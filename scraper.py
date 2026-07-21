# -*- coding: utf-8 -*-
"""
こども家庭庁「新着・更新」ページ (https://www.cfa.go.jp/news) を読み取り、
RSS 2.0 フィード (docs/feed.xml) を生成するスクリプト。
GitHub Actions から定期実行される想定。
"""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.cfa.go.jp"
NEWS_URLS = [
    "https://www.cfa.go.jp/news",          # 1ページ目
    "https://www.cfa.go.jp/news?page=1",   # 2ページ目（取りこぼし防止）
]
FEED_PATH = Path("docs/feed.xml")
STATE_PATH = Path("docs/feed_items.json")
MAX_ITEMS = 100  # フィードに保持する最大件数

JST = timezone(timedelta(hours=9))

# 新着一覧に現れるカテゴリ名（先頭語の判定用）
KNOWN_CATEGORIES = [
    "報道発表", "会見", "会議等", "審議会", "お知らせ", "政策",
    "調達情報", "採用", "資料", "申請・届出", "広報・報道", "法令",
]

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*$")


def fetch_items():
    """新着ページから記事一覧を取得する"""
    items = []
    seen_guids = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; cfa-news-feed/1.0; internal RSS converter)"
    }
    for url in NEWS_URLS:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"WARN: fetch failed for {url}: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            text = " ".join(a.get_text(" ", strip=True).split())
            m = DATE_RE.search(text)
            if not m:
                continue  # 日付で終わらないリンクは新着記事ではない

            year, month, day = map(int, m.groups())
            title_body = text[: m.start()].strip()
            if not title_body:
                continue

            # 先頭語がカテゴリ名ならカテゴリとして切り出す
            category = ""
            rest = title_body
            for cat in KNOWN_CATEGORIES:
                if title_body.startswith(cat):
                    category = cat
                    rest = title_body[len(cat):].strip()
                    break

            href = a["href"]
            if href.startswith("/"):
                href = BASE_URL + href
            if not href.startswith("http"):
                continue

            # 同一リンクでもタイトルが違えば別記事として扱う
            guid = hashlib.md5((title_body + "|" + href).encode("utf-8")).hexdigest()
            if guid in seen_guids:
                continue
            seen_guids.add(guid)

            pub = datetime(year, month, day, 9, 0, 0, tzinfo=JST)
            items.append({
                "guid": guid,
                "title": (f"【{category}】" if category else "") + rest,
                "link": href,
                "category": category,
                "pub_iso": pub.isoformat(),
            })
    return items


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def merge_items(existing, new_items):
    """新規記事を先頭に追加し、既知の記事は保持する"""
    known = {it["guid"] for it in existing}
    added = [it for it in new_items if it["guid"] not in known]
    merged = added + existing
    merged.sort(key=lambda it: it["pub_iso"], reverse=True)
    return merged[:MAX_ITEMS], len(added)


def rfc822(iso_str):
    dt = datetime.fromisoformat(iso_str)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def build_feed(items):
    now = datetime.now(JST)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>こども家庭庁 新着・更新（非公式変換フィード）</title>",
        "<link>https://www.cfa.go.jp/news</link>",
        "<description>こども家庭庁ウェブサイトの新着・更新情報をRSSに変換した非公式フィードです（社内情報収集用）</description>",
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


def main():
    new_items = fetch_items()
    print(f"fetched {len(new_items)} items from news pages")
    if not new_items and not STATE_PATH.exists():
        print("ERROR: no items fetched and no previous state; aborting", file=sys.stderr)
        sys.exit(1)

    existing = load_state()
    merged, added = merge_items(existing, new_items)
    print(f"{added} new item(s); feed now has {len(merged)} item(s)")

    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    FEED_PATH.write_text(build_feed(merged), encoding="utf-8")
    print("feed.xml written")


if __name__ == "__main__":
    main()
