# -*- coding: utf-8 -*-
"""
backfill.py — こ家庁「新着・更新」と「調達情報」一覧を過去にさかのぼって
長期履歴(data/history.jsonl)に取り込む。通常の新着取得とは完全に独立。

安全装置:
  * 書き込み先は data/history.jsonl と data/backfill_report.json だけ。
    docs/feed.xml・docs/feed_items.json(RSS)には一切触れない → Slackには流れない
  * origin="backfill" を付けて通常取得と区別する
  * 既に履歴にある記事は書かない(重複登録なし)
  * --dry-run なら何も書かず件数だけ表示

使い方(段階実施):
  第1段 python backfill.py --list news --pages 3 --dry-run
       python backfill.py --list news --pages 3
       python backfill.py --list proc --pages 3
  第2段 python backfill.py --list news --start 3 --pages 30
  第3段 python backfill.py --list news --start 33 --pages 300
       python backfill.py --list proc --start 3 --pages 60
"""

import argparse
import json
import sys
import time
from datetime import datetime

import feedcore as fc
from scraper import parse_cfa_list, page_url, NEWS_LIST, PROC_LIST, procurement_relevant, get

INTERVAL = 1.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", choices=["news", "proc"], required=True)
    ap.add_argument("--start", type=int, default=0, help="開始ページ(0始まり)")
    ap.add_argument("--pages", type=int, default=3, help="読むページ数")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = NEWS_LIST if args.list == "news" else PROC_LIST
    known = fc._history_ids()
    records, report = [], {"list": args.list, "start": args.start, "pages_requested": args.pages,
                           "pages_fetched": 0, "items_seen": 0, "items_new": 0, "empty_pages": 0,
                           "errors": [], "date_min": "", "date_max": "", "by_category": {},
                           "started_at": datetime.now(fc.JST).isoformat(timespec="seconds")}
    for page in range(args.start, args.start + args.pages):
        url = page_url(base, page)
        try:
            time.sleep(INTERVAL - 1.0)
            r = get(url)   # scraper と同じ許可ホスト限定の取得
        except Exception as e:
            report["errors"].append(f"{url}: {e}")
            if len(report["errors"]) >= 3:
                print("ERROR: 連続失敗のため中断", file=sys.stderr)
                break
            continue
        report["pages_fetched"] += 1
        parsed = parse_cfa_list(r.text)
        if not parsed:
            report["empty_pages"] += 1
            if report["empty_pages"] >= 2:
                print(f"終端(記事なしページが2連続): page={page}")
                break
            continue
        for cat, title, link, dt in parsed:
            report["items_seen"] += 1
            d = dt.date().isoformat()
            report["date_min"] = min(report["date_min"] or d, d)
            report["date_max"] = max(report["date_max"] or d, d)
            report["by_category"][cat or "(なし)"] = report["by_category"].get(cat or "(なし)", 0) + 1
            iid = fc.history_item_id("こ家庁", link, title)
            if iid in known:
                continue
            known.add(iid)
            records.append({
                "item_id": iid, "event": "new", "source": "こ家庁", "category": cat,
                "title": title, "link": link, "pub_date": d, "origin": "backfill",
                "feed_eligible": False,
                "relevance_keywords": procurement_relevant(title) if cat == "調達情報" else [],
            })
        print(f"page {page}: {len(parsed)}件 (累計新規 {len(records)})")
    report["items_new"] = len(records)
    report["finished_at"] = datetime.now(fc.JST).isoformat(timespec="seconds")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.dry_run:
        print("dry-run: 何も書き込んでいません")
        return
    written = fc.append_history(records)
    print(f"history.jsonl に {written} 件追記しました")
    fc.ensure_dirs()
    rp = fc.DATA_DIR / "backfill_report.json"
    prev = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else []
    prev.append(report)
    rp.write_text(json.dumps(prev, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
