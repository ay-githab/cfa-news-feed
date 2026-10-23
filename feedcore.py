# -*- coding: utf-8 -*-
"""
feedcore.py — scraper.py / watch_pages.py / backfill.py が共用する部品。

役割:
  * ファイルの置き場所の定義(本番モード / テストモードの切替)
  * RSS(feed.xml)の生成  … 第1期と同じ形式を維持する
  * 長期履歴(data/history.jsonl)への「追記のみ」書き込み
  * 実行記録(data/last_run.json)の更新

ファイルの関係:
  通常取得(scraper.py)
     ├─ 新着を取得 → data/history.jsonl に「新規」イベントを追記(消さない・書き換えない)
     ├─ 配信対象だけを docs/feed_items.json(最新150件)に反映
     └─ docs/feed_items.json から docs/feed.xml(RSS)を生成 … Slackが読むのはこれだけ
  ページ監視(watch_pages.py)
     ├─ 変化を data/page_events.jsonl に追記
     └─ 変化があった時だけ docs/feed_items.json に1件追加 → feed.xml 再生成
  復元(backfill.py)
     └─ data/history.jsonl にだけ書く(source=backfill)。docs/ には一切触れない

テストモード(環境変数 FEED_MODE=test):
  すべての出力先が data/test/ 配下に変わり、docs/ と data/ 直下には書かない。
"""

import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

JST = timezone(timedelta(hours=9))
MODE = os.environ.get("FEED_MODE", "prod")
TEST = MODE == "test"

# ---- 置き場所 -------------------------------------------------------------
if TEST:
    _OUT = Path("data/test")
    FEED_PATH = _OUT / "feed.xml"
    STATE_PATH = _OUT / "feed_items.json"
    DATA_DIR = _OUT
else:
    FEED_PATH = Path("docs/feed.xml")          # ← URL・形式とも第1期のまま
    STATE_PATH = Path("docs/feed_items.json")  # ← RSS用(最新150件)
    DATA_DIR = Path("data")

HISTORY_PATH = DATA_DIR / "history.jsonl"       # 全件履歴(追記のみ)
PAGE_STATE_PATH = DATA_DIR / "pages_state.json" # ページ監視の前回状態
PAGE_EVENTS_PATH = DATA_DIR / "page_events.jsonl"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
CASES_PATH = DATA_DIR / "cases.json"            # 調達案件の追跡(案件番号キー)

MAX_ITEMS = 150

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cfa-news-feed/2.0; internal RSS converter)"
}


def ensure_dirs():
    for p in (FEED_PATH, STATE_PATH, HISTORY_PATH):
        p.parent.mkdir(parents=True, exist_ok=True)


# ---- RSS用アイテム(第1期と同じ形) ------------------------------------------
def make_item(source, category, title, link, pub_dt):
    prefix = f"【{source}"
    if category:
        prefix += f"/{category}"
    prefix += "】"
    guid = hashlib.md5((source + "|" + title + "|" + link).encode("utf-8")).hexdigest()
    return {
        "guid": guid,
        "title": prefix + title,
        "link": link,
        "category": category or source,
        "pub_iso": pub_dt.isoformat(),
    }


def load_state():
    """RSS用の最新150件。第1期にあった「調達を掃除する処理」は撤去した。"""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def merge_items(existing, new_items):
    known = {it["guid"] for it in existing}
    added = [it for it in new_items if it["guid"] not in known]
    merged = added + existing
    merged.sort(key=lambda it: it["pub_iso"], reverse=True)
    return merged[:MAX_ITEMS], len(added)


def rfc822(iso_str):
    return datetime.fromisoformat(iso_str).strftime("%a, %d %b %Y %H:%M:%S %z")


def build_feed(items):
    now = datetime.now(JST)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>政府サイト新着(こども政策関連・非公式変換フィード)</title>",
        "<link>https://www.cfa.go.jp/news</link>",
        "<description>こども家庭庁の新着全量と、4省庁RSSのこども政策関連記事をまとめた非公式フィードです(社内情報収集用)</description>",
        "<language>ja</language>",
        f"<lastBuildDate>{now.strftime('%a, %d %b %Y %H:%M:%S %z')}</lastBuildDate>",
    ]
    for it in items:
        parts += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(it['link'])}</link>",
            f'<guid isPermaLink="false">{it["guid"]}</guid>',
            f"<pubDate>{rfc822(it['pub_iso'])}</pubDate>",
        ]
        if it.get("category"):
            parts.append(f"<category>{escape(it['category'])}</category>")
        parts.append("</item>")
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)


def write_feed(items):
    ensure_dirs()
    STATE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    FEED_PATH.write_text(build_feed(items), encoding="utf-8")


# ---- 長期履歴(追記のみ) ------------------------------------------------------
def _history_ids():
    ids = set()
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line).get("item_id"))
                except Exception:
                    pass
    return ids


def append_history(records):
    """
    records: dict のリスト。各 dict は少なくとも item_id / event を持つ。
    同じ item_id の「新規」イベントは二重に書かない(重複登録の防止)。
    更新イベント(event != "new")は何度でも追記できる。
    既存行の書き換え・削除はこの関数からは行えない。
    """
    ensure_dirs()
    existing = _history_ids()
    written = 0
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        for r in records:
            if r.get("event") == "new" and r["item_id"] in existing:
                continue
            r.setdefault("recorded_at", datetime.now(JST).isoformat(timespec="seconds"))
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            existing.add(r["item_id"])
            written += 1
    return written


def history_item_id(source, link, title):
    return hashlib.md5((source + "|" + title + "|" + link).encode("utf-8")).hexdigest()


# ---- 実行記録 -----------------------------------------------------------------
def write_last_run(job, status, details):
    ensure_dirs()
    data = {}
    if LAST_RUN_PATH.exists():
        try:
            data = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[job] = {
        "status": status,
        "at": datetime.now(JST).isoformat(timespec="seconds"),
        "details": details,
    }
    LAST_RUN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
