# -*- coding: utf-8 -*-
"""
政府サイトの新着情報を収集し、こども政策関連に絞ってRSSフィード
(docs/feed.xml) を生成するスクリプト。GitHub Actions から定期実行される。

収集対象:
 1. こども家庭庁「新着・更新」ページ … 全量(除外カテゴリを除く)
 2. 4省庁の公式RSS(内閣府/厚労省/デジタル庁/首相官邸) … キーワード合致のみ
"""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import requests
import feedparser
from bs4 import BeautifulSoup

# ==========================================================================
# ★ キーワード設定(ここを編集すれば絞り込みを調整できます)
#    4省庁のRSS記事は、タイトルに以下のいずれかを含む場合のみ通知されます。
# ==========================================================================
KEYWORDS = [
    # こども軸
    "こども", "子ども", "子供", "児童", "保育", "幼児", "こども園",
    "幼稚園", "子育て", "少子化", "母子", "妊産婦", "出産", "産後",
    # 政策テーマ軸
    "虐待", "いじめ", "不登校", "放課後", "学童", "療育", "障害児",
    "ヤングケアラー", "貧困",
    # DX/AI軸
    "AI", "ＡＩ", "人工知能", "こどもDX", "PMH", "マイナポータル",
]

# こども家庭庁の新着のうち、通知しないカテゴリ
CFA_EXCLUDE_CATEGORIES = {"調達情報", "採用"}

# 4省庁の公式RSS
RSS_SOURCES = [
    ("内閣府",     "https://www.cao.go.jp/rss/news.rdf"),
    ("厚労省",     "https://www.mhlw.go.jp/stf/news.rdf"),
    ("デジタル庁", "https://www.digital.go.jp/rss/news.xml"),
    ("首相官邸",   "https://www.kantei.go.jp/index-jnews.rdf"),
]

# ==========================================================================

BASE_URL = "https://www.cfa.go.jp"
NEWS_URLS = [
    "https://www.cfa.go.jp/news",
    "https://www.cfa.go.jp/news?page=1",
]
FEED_PATH = Path("docs/feed.xml")
STATE_PATH = Path("docs/feed_items.json")
MAX_ITEMS = 150

JST = timezone(timedelta(hours=9))

KNOWN_CATEGORIES = [
    "報道発表", "会見", "会議等", "審議会", "お知らせ", "政策",
    "調達情報", "採用", "資料", "申請・届出", "広報・報道", "法令",
]

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*$")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cfa-news-feed/2.0; internal RSS converter)"
}


def matches_keywords(text):
    return any(kw in text for kw in KEYWORDS)


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


def fetch_cfa_items():
    """こども家庭庁 新着・更新ページ(全量、除外カテゴリを除く)"""
    items = []
    for url in NEWS_URLS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"WARN: CFA fetch failed for {url}: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            text = " ".join(a.get_text(" ", strip=True).split())
            m = DATE_RE.search(text)
            if not m:
                continue
            year, month, day = map(int, m.groups())
            title_body = text[: m.start()].strip()
            if not title_body:
                continue

            category = ""
            rest = title_body
            for cat in KNOWN_CATEGORIES:
                if title_body.startswith(cat):
                    category = cat
                    rest = title_body[len(cat):].strip()
                    break
            if category in CFA_EXCLUDE_CATEGORIES:
                continue

            href = a["href"]
            if href.startswith("/"):
                href = BASE_URL + href
            if not href.startswith("http"):
                continue

            pub = datetime(year, month, day, 9, 0, 0, tzinfo=JST)
            items.append(make_item("こ家庁", category, rest, href, pub))
    return items


def fetch_rss_items():
    """4省庁の公式RSSからキーワード合致記事のみ抽出"""
    items = []
    for source, url in RSS_SOURCES:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"WARN: RSS fetch failed for {source} {url}: {e}", file=sys.stderr)
            continue

        count = 0
        for e in parsed.entries:
            title = " ".join((e.get("title") or "").split())
            link = e.get("link") or ""
            if not title or not link:
                continue
            summary = e.get("summary") or ""
            if not (matches_keywords(title) or matches_keywords(summary)):
                continue

            if e.get("published_parsed"):
                t = e.published_parsed
                pub = datetime(t.tm_year, t.tm_mon, t.tm_mday,
                               t.tm_hour, t.tm_min, tzinfo=timezone.utc).astimezone(JST)
            elif e.get("updated_parsed"):
                t = e.updated_parsed
                pub = datetime(t.tm_year, t.tm_mon, t.tm_mday,
                               t.tm_hour, t.tm_min, tzinfo=timezone.utc).astimezone(JST)
            else:
                pub = datetime.now(JST)

            items.append(make_item(source, "", title, link, pub))
            count += 1
        print(f"{source}: {len(parsed.entries)} entries, {count} matched")
    return items


def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            # 旧版由来の除外カテゴリ記事を掃除
            return [it for it in data
                    if not any(f"【{c}】" in it.get("title", "") or f"/{c}】" in it.get("title", "")
                               for c in CFA_EXCLUDE_CATEGORIES)]
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


def main():
    cfa = fetch_cfa_items()
    print(f"CFA: {len(cfa)} items")
    rss = fetch_rss_items()
    print(f"RSS filtered: {len(rss)} items")
    new_items = cfa + rss

    if not new_items and not STATE_PATH.exists():
        print("ERROR: no items fetched and no previous state; aborting", file=sys.stderr)
        sys.exit(1)

    existing = load_state()
    merged, added = merge_items(existing, new_items)
    print(f"{added} new item(s); feed now has {len(merged)} item(s)")

    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    FEED_PATH.write_text(build_feed(merged), encoding="utf-8")
    print("feed.xml written")


if __name__ == "__main__":
    main()
