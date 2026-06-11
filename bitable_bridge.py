#!/usr/bin/env python3
"""
雷达→飞书多维表格桥(焕新美居_by选题雷达)
在 GitHub Actions 里 crawler 跑完后执行:
读当天 output/news/<date>.db → 按 config/frequency_words.txt 筛命中话题 → 写入 Bitable。
缺凭据时静默跳过(exit 0),不影响群推送主流程。
环境变量: FEISHU_APP_ID / FEISHU_APP_SECRET / BITABLE_APP_TOKEN / BITABLE_TABLE_ID
"""
import os
import re
import sqlite3
import sys
import json
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://open.feishu.cn/open-apis"


def log(msg):
    print(f"[bitable_bridge] {msg}")


def api(path, method="GET", token=None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as r:
        out = json.loads(r.read().decode())
    if out.get("code") != 0:
        raise RuntimeError(f"{path} -> {out.get('code')}: {out.get('msg')}")
    return out


def parse_rules(path):
    """解析 frequency_words.txt: [组名] 下每行一条规则(+必含 !排除 普通词=任一命中)"""
    rules = []
    group = "默认"
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            group = m.group(1)
            continue
        req, exc, any_ = [], [], []
        for tok in line.split():
            if tok.startswith("+"):
                req.append(tok[1:])
            elif tok.startswith("!"):
                exc.append(tok[1:])
            else:
                any_.append(tok)
        if req or any_:
            rules.append((group, req, exc, any_))
    return rules


def match(title, rules):
    for group, req, exc, any_ in rules:
        if any(w in title for w in exc):
            continue
        if req and not all(w in title for w in req):
            continue
        if any_ and not any(w in title for w in any_):
            continue
        return group
    return None


def main():
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    app_token = os.environ.get("BITABLE_APP_TOKEN", "")
    table_id = os.environ.get("BITABLE_TABLE_ID", "")
    if not all([app_id, app_secret, app_token, table_id]):
        log("凭据未配齐,跳过存表(不影响群推送)")
        return

    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    db_path = f"output/news/{today}.db"
    if not os.path.exists(db_path):
        log(f"未找到 {db_path},跳过")
        return

    rules = parse_rules("config/frequency_words.txt")
    db = sqlite3.connect(db_path)
    rows = db.execute(
        "SELECT n.title, p.name, n.rank, COALESCE(NULLIF(n.url,''), n.mobile_url) "
        "FROM news_items n JOIN platforms p ON n.platform_id = p.id"
    ).fetchall()

    hits = []
    for title, platform, rank, url in rows:
        g = match(title, rules)
        if g:
            hits.append({"话题": title, "平台": platform, "热度排名": rank or 0,
                         "链接": {"link": url or "", "text": "原文"},
                         "命中关键词组": g, "日期": today, "来源": "选题雷达"})
    log(f"全量{len(rows)}条,命中{len(hits)}条")
    if not hits:
        return

    tok = api("/auth/v3/tenant_access_token/internal", "POST",
              body={"app_id": app_id, "app_secret": app_secret})["tenant_access_token"]

    # 当天已存的话题去重(翻页拉全)
    existing = set()
    page = ""
    while True:
        q = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=500"
        if page:
            q += f"&page_token={page}"
        data = api(q, token=tok)["data"]
        for it in data.get("items") or []:
            f = it.get("fields", {})
            d = f.get("日期", "")
            if isinstance(d, list):
                d = "".join(x.get("text", "") for x in d if isinstance(x, dict))
            t = f.get("话题", "")
            if isinstance(t, list):
                t = "".join(x.get("text", "") for x in t if isinstance(x, dict))
            if d == today:
                existing.add(t)
        if not data.get("has_more"):
            break
        page = data.get("page_token", "")

    fresh = [h for h in hits if h["话题"] not in existing]
    log(f"去重后新增{len(fresh)}条(当天已有{len(existing)}条)")
    for i in range(0, len(fresh), 100):
        api(f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            "POST", token=tok,
            body={"records": [{"fields": r} for r in fresh[i:i + 100]]})
    log("写入完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"出错但不阻塞主流程: {e}")
        sys.exit(0)
