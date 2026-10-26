# -*- coding: utf-8 -*-
"""
scraper.py — 政府サイトの新着を収集し RSS(docs/feed.xml) を更新する。
GitHub Actions から定期実行される。 (第2期: 2026-10 改修版)

第1期からの変更点:
  1. こ家庁の調達情報を除外しない。全件を履歴(data/history.jsonl)に保存し、
     保育ICT関連のものだけを RSS に流す(判定と配信を分離)
  2. 「直近2ページだけ」をやめ、既知の記事だけのページが2ページ続くまで読む
     (上限あり。上限に達したら警告)
  3. 調達一覧(/news?category=11)も同じ方式で読む
  4. こ家庁の取得失敗・0件・構造不一致を「失敗」として終了コード1で返す
     (これまでは警告だけ出して正常終了していた)
  5. 起動時に過去データを削除する処理を撤去
  6. 実行結果を data/last_run.json に記録(未起動検知に使う)
  7. 14日より古い記事は RSS に流さない(初回・復元時の大量配信防止)
  8. 公表日から1日以上遅れて見つけた記事は RSS の日時を検知時刻にする(Slackが古い日付を飛ばすため)

環境変数:
  FEED_MODE=test  … 出力を data/test/ に切り替える(本番に触れない)
  FEED_DEEP=1     … 週1回の深掘り(ページ上限を広げる)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import feedparser
from bs4 import BeautifulSoup

import feedcore as fc

# ==========================================================================
# ★ 設定(ここを編集すれば絞り込みを調整できます)
# ==========================================================================

# 4省庁RSS: タイトル/概要に含まれていれば通知
KEYWORDS = [
    "こども", "子ども", "子供", "児童", "保育", "幼児", "こども園",
    "幼稚園", "子育て", "少子化", "母子", "妊産婦", "出産", "産後",
    "虐待", "いじめ", "不登校", "放課後", "学童", "療育", "障害児",
    "ヤングケアラー", "貧困",
    "AI", "ＡＩ", "人工知能", "こどもDX", "PMH", "マイナポータル",
]

# こ家庁の調達情報: タイトルにこれらを含むものだけ RSS に流す(履歴には全件残る)
PROCUREMENT_KEYWORDS = [
    "システム", "プラットフォーム", "保育", "ICT", "ＩＣＴ", "データ", "連携",
    "改修", "標準", "公表", "給付", "監査", "通園", "性暴力防止", "DBS",
    "母子", "PMH", "情報", "デジタル", "DX", "ＤＸ", "AI", "ＡＩ",
    "就労証明", "保活", "こども園", "幼稚園", "調査研究", "クラウド",
]

# RSS に流さないこ家庁カテゴリ(履歴には残す)
CFA_FEED_EXCLUDE = {"採用"}

RSS_SOURCES = [
    ("内閣府",     "https://www.cao.go.jp/rss/news.rdf"),
    ("厚労省",     "https://www.mhlw.go.jp/stf/news.rdf"),
    ("デジタル庁", "https://www.digital.go.jp/rss/news.xml"),
    ("首相官邸",   "https://www.kantei.go.jp/index-jnews.rdf"),
]

# ページ取得の上限(通常 / 深掘り)
MAX_PAGES_NEWS = 10
MAX_PAGES_PROC = 5
DEEP_MAX_PAGES_NEWS = 30
DEEP_MAX_PAGES_PROC = 15
STOP_AFTER_KNOWN_PAGES = 2       # 全件既知のページがこの数だけ続いたら停止
FEED_MAX_AGE_DAYS = 14           # これより古い記事は RSS に入れない
REQUEST_INTERVAL_SEC = 1.0       # 政府サイトへの礼儀(1秒に1アクセス)

# ==========================================================================

BASE_URL = "https://www.cfa.go.jp"
NEWS_LIST = "https://www.cfa.go.jp/news"
PROC_LIST = "https://www.cfa.go.jp/news?category=11"
PROC_PORTAL_PAGES = [
    "https://www.cfa.go.jp/procurement/portal",
    "https://www.cfa.go.jp/procurement/service",
]

JST = fc.JST
DEEP = os.environ.get("FEED_DEEP") == "1"

KNOWN_CATEGORIES = [
    "報道発表", "会見", "会議等", "審議会", "お知らせ", "政策",
    "調達情報", "採用", "資料", "申請・届出", "広報・報道", "法令",
    "組織情報", "大臣等会見", "相談窓口",
]

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*$")

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print("WARN: " + msg, file=sys.stderr)


ALLOWED_HOSTS = {"www.cfa.go.jp", "www.cao.go.jp", "www.mhlw.go.jp", "www.digital.go.jp", "www.kantei.go.jp"}


def get(url):
    """固定URLしか呼ばないが、二重防御として https・許可ホスト以外を拒否し、リダイレクトは追わない。"""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"許可外のURL: {url}")
    try:
        fc.ensure_dirs()
        with (fc.DATA_DIR / "hosts_accessed.txt").open("a", encoding="utf-8") as f:
            f.write(u.hostname + "\n")
    except Exception:
        pass
    time.sleep(REQUEST_INTERVAL_SEC)
    resp = requests.get(url, headers=fc.HEADERS, timeout=30, allow_redirects=False)
    resp.raise_for_status()
    return resp


# ---- こ家庁 一覧ページの解析 -----------------------------------------------
def parse_cfa_list(html):
    """1ページ分のHTMLから記事を取り出す。戻り値: [(category, title, link, date)]"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        m = DATE_RE.search(text)
        if not m:
            continue
        year, month, day = map(int, m.groups())
        title_body = text[: m.start()].strip()
        if not title_body:
            continue
        category, rest = "", title_body
        for cat in KNOWN_CATEGORIES:
            if title_body.startswith(cat):
                category, rest = cat, title_body[len(cat):].strip()
                break
        href = a["href"]
        if href.startswith("/"):
            href = BASE_URL + href
        if not href.startswith("http"):
            continue
        out.append((category, rest, href, datetime(year, month, day, 9, 0, 0, tzinfo=JST)))
    return out


def page_url(list_url, page):
    if page == 0:
        return list_url
    return list_url + ("&" if "?" in list_url else "?") + f"page={page}"


def crawl_cfa_list(list_url, known_ids, max_pages, label, fetch=None):
    """
    既知の記事だけのページが STOP_AFTER_KNOWN_PAGES ページ続くまで読む。
    戻り値: (items, info)  items は [(category,title,link,date)] の新規のみ
    fetch: テスト用に差し替え可能な取得関数(url -> html)
    """
    fetch = fetch or (lambda u: get(u).text)
    items, consecutive_known, pages = [], 0, 0
    info = {"label": label, "pages": 0, "stopped": "", "fatal": False, "error": ""}
    for page in range(max_pages):
        url = page_url(list_url, page)
        try:
            html = fetch(url)
        except Exception as e:
            msg = f"{label}: 取得失敗 {url}: {e}"
            if page == 0:
                info.update(stopped="fetch_error", fatal=True, error=msg)
                return items, info
            warn(msg)
            info["stopped"] = "fetch_error_later_page"
            break
        pages += 1
        parsed = parse_cfa_list(html)
        if not parsed:
            msg = f"{label}: {url} から記事を1件も読み取れませんでした(HTML構造変更の疑い)"
            if page == 0:
                info.update(stopped="parse_zero", fatal=True, error=msg, pages=pages)
                return items, info
            warn(msg)
            info["stopped"] = "parse_zero_later_page"
            break
        new_here = 0
        for cat, title, link, dt in parsed:
            iid = fc.history_item_id("こ家庁", link, title)
            if iid in known_ids:
                continue
            known_ids.add(iid)
            items.append((cat, title, link, dt))
            new_here += 1
        if new_here == 0:
            consecutive_known += 1
            if consecutive_known >= STOP_AFTER_KNOWN_PAGES:
                info["stopped"] = "known_pages"
                break
        else:
            consecutive_known = 0
    else:
        info["stopped"] = "page_limit"
        warn(f"{label}: ページ上限({max_pages})に達しても既知の記事に当たりませんでした。取り残しの可能性があります")
    info["pages"] = pages
    return items, info


def procurement_relevant(title):
    return [k for k in PROCUREMENT_KEYWORDS if k in title]


# ---- 調達案件番号の対応付け(できる範囲で) -----------------------------------
ANKEN_RE = re.compile(r"anken=(\d{10,})")


def fetch_portal_case_map():
    """調達ポータル案内ページから 案件名→案件番号 の対応を取る。失敗しても空で返す。"""
    cases = {}
    for url in PROC_PORTAL_PAGES:
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except Exception as e:
            warn(f"調達案内ページ取得失敗 {url}: {e}")
            continue
        for a in soup.find_all("a", href=True):
            m = ANKEN_RE.search(a["href"])
            if not m:
                continue
            name = " ".join(a.get_text(" ", strip=True).split())
            if name:
                cases[name] = {"anken": m.group(1), "pportal_url": a["href"], "found_on": url}
    return cases


def update_cases(proc_items, case_map):
    """関連調達を data/cases.json に登録。案件番号が取れたものは番号をキーに。"""
    if not proc_items:
        return 0
    fc.ensure_dirs()
    data = {}
    if fc.CASES_PATH.exists():
        try:
            data = json.loads(fc.CASES_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    added = 0
    for cat, title, link, dt in proc_items:
        anken, purl = None, None
        for name, c in case_map.items():
            if name and name in title:
                anken, purl = c["anken"], c["pportal_url"]
                break
        key = anken or ("title:" + fc.history_item_id("こ家庁", link, title))
        entry = data.setdefault(key, {"anken": anken, "pportal_url": purl, "events": []})
        ev = {"date": dt.date().isoformat(), "title": title, "cfa_link": link}
        if ev not in entry["events"]:
            entry["events"].append(ev)
            added += 1
    fc.CASES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return added


# ---- 4省庁RSS(第1期と同じ判定) ------------------------------------------------
def matches_keywords(text):
    return any(kw in text for kw in KEYWORDS)


def fetch_rss_items():
    items, status = [], {}
    for source, url in RSS_SOURCES:
        try:
            resp = get(url)
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            warn(f"RSS取得失敗 {source} {url}: {e}")
            status[source] = "fetch_error"
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
            t = e.get("published_parsed") or e.get("updated_parsed")
            if t:
                pub = datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min,
                               tzinfo=timezone.utc).astimezone(JST)
            else:
                pub = datetime.now(JST)
            items.append((source, title, link, pub))
            count += 1
        status[source] = f"{len(parsed.entries)} entries / {count} matched"
        if len(parsed.entries) == 0:
            warn(f"{source}: RSSの記事数が0件でした(配信停止またはURL変更の疑い)")
    return items, status


# ---- メイン -------------------------------------------------------------------
STAGES = {"fetch": "pending", "history": "pending", "feed": "pending"}


def main():
    fc.ensure_dirs()
    origin = "deep" if DEEP else "scheduled"
    existing = fc.load_state()
    known_ids = {it["guid"] for it in existing} | fc._history_ids()
    if fc.TEST and not existing:
        # テストでは本番のRSS用150件(docs/feed_items.json)を読み取り専用で既知扱いにし、本番初回と同じ条件にする
        try:
            known_ids |= {it["guid"] for it in json.loads(Path("docs/feed_items.json").read_text(encoding="utf-8"))}
        except Exception:
            pass

    # 1) こ家庁 新着(全カテゴリ)
    news_items, news_info = crawl_cfa_list(
        NEWS_LIST, known_ids, DEEP_MAX_PAGES_NEWS if DEEP else MAX_PAGES_NEWS, "こ家庁新着")
    if news_info["fatal"]:
        STAGES["fetch"] = "failed"
        fc.write_last_run("scraper", "failed", {"error": news_info["error"], "warnings": WARNINGS, "stages": STAGES})
        print("ERROR: " + news_info["error"], file=sys.stderr)
        sys.exit(1)

    # 2) こ家庁 調達一覧
    proc_items, proc_info = crawl_cfa_list(
        PROC_LIST, known_ids, DEEP_MAX_PAGES_PROC if DEEP else MAX_PAGES_PROC, "こ家庁調達")
    if proc_info["fatal"]:
        warn(proc_info["error"])   # 調達一覧だけの失敗は警告扱い(新着の配信は止めない)

    cfa_items = news_items + proc_items

    # 3) 4省庁RSS
    rss_items, rss_status = fetch_rss_items()
    STAGES["fetch"] = "ok"

    # 4) 履歴に追記(全件)、RSS候補の選別
    now = datetime.now(JST)
    cutoff = now - timedelta(days=FEED_MAX_AGE_DAYS)
    records, feed_candidates, relevant_proc = [], [], []
    for cat, title, link, dt in cfa_items:
        kws = procurement_relevant(title) if cat == "調達情報" else []
        eligible = (cat not in CFA_FEED_EXCLUDE) and (cat != "調達情報" or bool(kws))
        records.append({
            "item_id": fc.history_item_id("こ家庁", link, title), "event": "new",
            "source": "こ家庁", "category": cat, "title": title, "link": link,
            "pub_date": dt.date().isoformat(), "origin": origin,
            "feed_eligible": eligible, "relevance_keywords": kws,
        })
        if cat == "調達情報" and kws:
            relevant_proc.append((cat, title, link, dt))
        if eligible and dt >= cutoff:
            # 公表日から1日以上遅れて見つけた記事は、RSS上の日時を「見つけた時刻」にする。
            # SlackのRSSアプリは日時の古い記事を新着扱いしないため(2026/10/23 の調達12件が未配信になった)。
            # 元の公表日はタイトル末尾に残し、履歴(pub_date)にも元の日付を保存する。
            if now - dt > timedelta(days=1):
                feed_candidates.append(fc.make_item("こ家庁", cat, f"{title}（公表{dt.month}/{dt.day}）", link, now))
            else:
                feed_candidates.append(fc.make_item("こ家庁", cat, title, link, dt))
    for source, title, link, pub in rss_items:
        records.append({
            "item_id": fc.history_item_id(source, link, title), "event": "new",
            "source": source, "category": "", "title": title, "link": link,
            "pub_date": pub.date().isoformat(), "origin": origin, "feed_eligible": True,
        })
        if pub >= cutoff:
            feed_candidates.append(fc.make_item(source, "", title, link, pub))
    try:
        hist_written = fc.append_history(records)
        STAGES["history"] = "ok"
    except Exception as e:
        STAGES["history"] = f"failed: {e}"
        fc.write_last_run("scraper", "failed", {"error": f"履歴保存失敗: {e}", "stages": STAGES, "warnings": WARNINGS})
        print(f"ERROR: 履歴保存失敗: {e}", file=sys.stderr)
        sys.exit(1)

    # 5) 関連調達の案件追跡
    cases_added = 0
    if relevant_proc:
        cases_added = update_cases(relevant_proc, fetch_portal_case_map())

    # 6) RSS 更新
    merged, added = fc.merge_items(existing, feed_candidates)
    try:
        fc.write_feed(merged)
        # 書いた直後に読み戻して件数とXMLの体裁を確認
        chk = json.loads(fc.STATE_PATH.read_text(encoding="utf-8"))
        xml = fc.FEED_PATH.read_text(encoding="utf-8")
        assert len(chk) == len(merged) and xml.startswith("<?xml") and xml.rstrip().endswith("</rss>")
        STAGES["feed"] = "ok"
    except Exception as e:
        STAGES["feed"] = f"failed: {e}"
        fc.write_last_run("scraper", "failed", {"error": f"RSS生成失敗: {e}", "stages": STAGES, "warnings": WARNINGS})
        print(f"ERROR: RSS生成失敗: {e}", file=sys.stderr)
        sys.exit(1)

    summary = {
        "news_pages": news_info["pages"], "news_stop": news_info["stopped"], "news_new": len(news_items),
        "proc_pages": proc_info["pages"], "proc_stop": proc_info["stopped"], "proc_new": len(proc_items),
        "proc_relevant": len(relevant_proc), "rss": rss_status,
        "history_written": hist_written, "feed_added": added, "feed_total": len(merged),
        "cases_added": cases_added, "warnings": WARNINGS, "mode": fc.MODE, "origin": origin,
        "stages": STAGES,
    }
    fc.write_last_run("scraper", "ok", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if WARNINGS:
        Path(fc.DATA_DIR / "run_warnings.txt").write_text("\n".join(WARNINGS), encoding="utf-8")


if __name__ == "__main__":
    main()
