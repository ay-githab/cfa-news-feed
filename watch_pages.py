# -*- coding: utf-8 -*-
"""
watch_pages.py — 「新着一覧」に載らない重要ページ(保活基盤・施設管理PF・ここdeサーチ・
データ標準など)の本文と添付資料の変化を検知する。

監視対象は watch_targets.json に書く(URL・監視の深さ)。

検知の3層:
  1. 本文   … ナビ・フッターを除いた本文のハッシュと、見出し単位の指紋。
               本文そのものも差分の行も保存しない(「どの見出しの下が変わったか」だけ出す)
  2. 添付一覧 … ページ内の PDF/Excel/Word/CSV/ZIP リンクの追加・削除
  3. 添付内容 … 同じURLのまま中身が変わっても気づけるよう、
               HEAD で ETag/Last-Modified/サイズを比較し、変化・欠落時は
               ダウンロードして SHA-256 を比較。メタデータが不変でも
               7日ごと(REHASH_DAYS)と週次深掘り(--deep)では必ず再ダウンロードして確認。Excel/CSV は見出し行、
               PDF は本文テキスト、ZIP は中身一覧を保存して差分を出す。

書き込み先:
  data/pages_state.json  … 前回の状態(ハッシュ等)
  data/page_events.jsonl … 検知した変化(追記のみ)
  data/attachment_text/  … 添付の「見出し行」「ページ指紋」「ZIP一覧」(本文は含まない。伏字処理済み)
  docs/feed_items.json / feed.xml … 変化があったページにつき1件だけ追加

使い方:
  python watch_pages.py                  通常実行
  python watch_pages.py --deep           全添付をダウンロードして再ハッシュ
  python watch_pages.py --probe          到達性チェックだけ(状態を更新しない)
  python watch_pages.py --simulate-change <id>   テスト用: 前回状態をわざとずらして検知を確認
  python watch_pages.py --report out.md  結果を Markdown で書き出す
"""

import argparse
import csv
import difflib
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import feedcore as fc

TARGETS_PATH = Path("watch_targets.json")
ATTACH_TEXT_DIR = fc.DATA_DIR / "attachment_text"
ATTACH_RE = re.compile(r"\.(pdf|xlsx|xlsm|xls|docx|doc|csv|zip|pptx)(\?.*)?$", re.I)
REQUEST_INTERVAL_SEC = float(__import__("os").environ.get("FEED_INTERVAL", "1.0"))
DEFAULT_SIZE_CAP_MB = 30
REHASH_DAYS = 7          # ETag/更新日時/サイズが不変でも、この日数を過ぎたら中身を再確認する
JST = fc.JST
NOW = datetime.now(JST)

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print("WARN: " + msg, file=sys.stderr)


ALLOWED_HOSTS = set()   # watch_targets.json の allowed_hosts で上書き
MAX_REDIRECTS = 3


class BlockedURL(Exception):
    pass


def check_allowed(url):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme != "https":
        raise BlockedURL(f"https以外は禁止: {url}")
    if u.hostname not in ALLOWED_HOSTS:
        raise BlockedURL(f"許可外ホスト: {u.hostname}")
    return url


def _log_host(url):
    """実際に接続したホストを記録(テスト時に許可リスト内だけだったかを検算する)"""
    from urllib.parse import urlsplit
    try:
        fc.ensure_dirs()
        with (fc.DATA_DIR / "hosts_accessed.txt").open("a", encoding="utf-8") as f:
            f.write(urlsplit(url).hostname + "\n")
    except Exception:
        pass


def http(method, url, **kw):
    """許可ホスト・https限定。リダイレクトは1段ずつ検査しながら最大3回まで追う。"""
    check_allowed(url)
    for _ in range(MAX_REDIRECTS + 1):
        _log_host(url)
        time.sleep(REQUEST_INTERVAL_SEC)
        r = requests.request(method, url, headers=fc.HEADERS, timeout=60, allow_redirects=False, **kw)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = check_allowed(urljoin(url, r.headers["Location"]))
            continue
        return r
    raise BlockedURL(f"リダイレクトが多すぎます: {url}")


def sha256(b):
    return hashlib.sha256(b).hexdigest()


# 保存・表示する文字列から、メールアドレス・電話番号・個人名らしき表記を伏せる
PII_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[メール]"),
    (re.compile(r"(?<!\d)(0\d{1,4}[-ー－(（]?\d{1,4}[-ー－)）]?\d{3,4})(?!\d)"), "[電話]"),
    (re.compile(r"(担当|係|氏名|連絡先)[:：]\s*[^\s、,]{2,12}"), r"\1:[伏字]"),
]


def mask_pii(text):
    for pat, rep in PII_PATTERNS:
        text = pat.sub(rep, text)
    return text


# ---- ダウンロード・展開の上限 ----
DL_CHUNK = 1 << 16
ZIP_MAX_MEMBERS = 500
ZIP_MAX_TOTAL = 200 * 1024 * 1024
ZIP_MAX_MEMBER = 20 * 1024 * 1024
ZIP_MAX_RATIO = 100


def download_capped(url, cap_bytes):
    """途中でサイズ上限を超えたら中断する。戻り値 (bytes or None, reason)"""
    r = http("GET", url, stream=True)
    r.raise_for_status()
    buf, total = io.BytesIO(), 0
    for chunk in r.iter_content(DL_CHUNK):
        total += len(chunk)
        if total > cap_bytes:
            r.close()
            return None, f"サイズ上限({cap_bytes // 1024 // 1024}MB)超のため中断"
        buf.write(chunk)
    return buf.getvalue(), ""


def zip_members_safe(zf):
    """ZIP爆弾対策: 件数・総容量・個別容量・圧縮率を確認してから中身一覧を返す"""
    infos = zf.infolist()
    if len(infos) > ZIP_MAX_MEMBERS:
        raise ValueError(f"ZIP内ファイル数が上限超({len(infos)})")
    for i in infos:
        if i.file_size > ZIP_MAX_MEMBER:
            raise ValueError(f"ZIP内の1ファイルが上限超({i.filename}: {i.file_size} bytes)")
    total = sum(i.file_size for i in infos)
    if total > ZIP_MAX_TOTAL:
        raise ValueError(f"ZIP展開後の総容量が上限超({total})")
    comp = sum(i.compress_size for i in infos) or 1
    if total / comp > ZIP_MAX_RATIO:
        raise ValueError(f"ZIP圧縮率が異常({total / comp:.0f}倍)")
    return infos


# ---- 本文抽出 -----------------------------------------------------------------
def extract_main_text(html):
    """戻り値: (main要素, 本文テキスト, 最終更新日)。本文テキストはメモリ上で比較にだけ使い、保存しない。"""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "iframe"]):
        t.decompose()
    main = soup.find("main") or soup.find(id="mainContainer") or soup.find(id="main") or soup.body or soup
    lines = []
    for raw in main.get_text("\n").splitlines():
        s = " ".join(raw.split())
        if s:
            lines.append(s)
    text = "\n".join(lines)
    m = re.search(r"最終更新日[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日)", text)
    last_updated = m.group(1) if m else ""
    return main, text, last_updated


def section_fingerprints(main):
    """
    見出し(h1〜h4)ごとに本文の指紋を作る。公開リポジトリに残るのは
    「見出し名(伏字処理済み・60字まで)」「その区間のハッシュ」「行数」だけ。
    """
    sections, cur, buf = [], "(冒頭)", []

    def flush():
        txt = "\n".join(buf)
        sections.append({"h": mask_pii(cur)[:60], "hash": sha256(txt.encode("utf-8"))[:16], "n": len(buf)})

    for el in main.descendants:
        if getattr(el, "name", None) in ("h1", "h2", "h3", "h4"):
            flush()
            cur, buf = " ".join(el.get_text(" ", strip=True).split()) or "(無題)", []
        elif isinstance(el, str):
            t = " ".join(el.split())
            if t:
                buf.append(t)
    flush()
    return sections


def section_diff(old, new):
    """見出し単位の差分を文章で返す(本文は含まない)"""
    o = {s_["h"]: s_ for s_ in old}
    n = {s_["h"]: s_ for s_ in new}
    msgs = []
    for h, s_ in n.items():
        if h not in o:
            msgs.append(f"見出し追加「{h}」({s_['n']}行)")
        elif s_["hash"] != o[h]["hash"]:
            msgs.append(f"「{h}」の内容が変化({o[h]['n']}行→{s_['n']}行)")
    for h in o:
        if h not in n:
            msgs.append(f"見出し削除「{h}」")
    return msgs


def extract_attachments(main, base_url, flt, limit):
    seen, out = set(), []
    for a in main.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not ATTACH_RE.search(href):
            continue
        if flt and not re.search(flt, href + " " + a.get_text(" ", strip=True)):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({"url": href, "name": mask_pii(" ".join(a.get_text(" ", strip=True).split()))[:120]})
        if len(out) >= limit:
            break
    return out


# ---- 添付内容の要約(差分用) ---------------------------------------------------
def summarize_bytes(name, data, depth=0):
    """ファイル種別ごとに「差分を取る対象」を文字列で返す。失敗しても空文字。"""
    low = name.lower()
    try:
        if low.endswith(".csv"):
            txt = data.decode("utf-8-sig", errors="replace")
            rows = list(csv.reader(io.StringIO(txt)))
            head = rows[0] if rows else []
            return "CSV 見出し: " + " | ".join(head) + f"\n行数: {len(rows)}"
        if low.endswith((".xlsx", ".xlsm")):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                header = []
                for row in ws.iter_rows(min_row=1, max_row=5, values_only=True):
                    vals = [str(v) for v in row if v not in (None, "")]
                    if len(vals) >= 2:
                        header = vals
                        break
                parts.append(f"シート: {ws.title}\n  見出し: " + " | ".join(header)[:2000])
            return "\n".join(parts)
        if low.endswith(".pdf"):
            # 本文そのものは保存しない。ページごとの指紋だけを残し、「どのページが変わったか」を出す
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            lines = [f"PDF ページ数: {len(reader.pages)}"]
            for i, p in enumerate(reader.pages[:300], 1):
                try:
                    t = " ".join((p.extract_text() or "").split())
                except Exception:
                    t = ""
                lines.append(f"p{i}: {sha256(t.encode('utf-8'))[:12]} ({len(t)}字)")
            return "\n".join(lines)
        if low.endswith(".zip") and depth < 1:
            zf = zipfile.ZipFile(io.BytesIO(data))
            infos = zip_members_safe(zf)
            parts = ["ZIP 内容:"]
            for info in infos:
                parts.append(f"  {info.filename} ({info.file_size} bytes)")
            for info in infos:
                if info.file_size <= ZIP_MAX_MEMBER and info.filename.lower().endswith((".csv", ".xlsx", ".pdf")):
                    sub = summarize_bytes(info.filename, zf.read(info), depth + 1)
                    if sub:
                        parts.append(f"--- {info.filename} ---\n{sub[:20000]}")
            return "\n".join(parts)
    except Exception as e:
        return f"(要約失敗: {e})"
    return ""


def _summarize_masked(name, data):
    return mask_pii(summarize_bytes(name, data))


def text_diff(old, new, limit=40):
    diff = [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---"))]
    return diff[:limit], len(diff)


def save_text(key, text):
    ATTACH_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    p = ATTACH_TEXT_DIR / (key + ".txt")
    p.write_text(text, encoding="utf-8")
    return str(p)


def load_text(key):
    p = ATTACH_TEXT_DIR / (key + ".txt")
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---- 添付1件の確認 -------------------------------------------------------------
def check_attachment(att, prev, mode, deep, cap_bytes):
    """
    戻り値: (新しい状態dict, 変化の説明 or None)
    mode: "none"(一覧のみ) / "headers"(HEAD比較) / "hash"(ダウンロードしてハッシュ+内容)
    """
    url = att["url"]
    state = {"name": att["name"], "etag": "", "last_modified": "", "size": "", "sha256": "",
             "summary_key": "", "checked_at": NOW.isoformat(timespec="seconds")}
    if mode == "none":
        return state, None
    try:
        check_allowed(url)
    except BlockedURL as e:
        state["sha256"] = "skipped:blocked_host"
        return state, None   # 許可外ホストの資料はリンクの有無だけ追跡し、中身は取りに行かない
    try:
        h = http("HEAD", url)
        state["etag"] = h.headers.get("ETag", "")
        state["last_modified"] = h.headers.get("Last-Modified", "")
        state["size"] = h.headers.get("Content-Length", "")
        if h.status_code >= 400:
            return state, f"添付にアクセスできません(HTTP {h.status_code}): {att['name']}"
    except Exception as e:
        return state, f"添付のHEAD失敗: {att['name']}: {e}"

    headers_known = bool(state["etag"] or state["last_modified"] or state["size"])
    stale = False
    if prev and prev.get("hashed_at"):
        try:
            stale = (NOW - datetime.fromisoformat(prev["hashed_at"])).days >= REHASH_DAYS
        except Exception:
            stale = True
    elif prev and mode == "hash":
        stale = True
    headers_changed = prev and (
        (state["etag"] and state["etag"] != prev.get("etag")) or
        (state["last_modified"] and state["last_modified"] != prev.get("last_modified")) or
        (state["size"] and state["size"] != prev.get("size")))
    need_download = (mode == "hash" and (prev is None or deep or headers_changed or not headers_known or stale))
    if mode == "headers":
        if prev and headers_changed:
            return state, f"差し替えの疑い(更新日時/サイズ変化): {att['name']}"
        if prev:
            state["sha256"], state["summary_key"] = prev.get("sha256", ""), prev.get("summary_key", "")
        return state, None

    if not need_download:
        state["sha256"], state["summary_key"] = prev.get("sha256", ""), prev.get("summary_key", "")
        state["hashed_at"] = prev.get("hashed_at", "")
        return state, None
    try:
        size = int(state["size"] or 0)
        if size > cap_bytes:
            state["sha256"] = "skipped:too_large"
            return state, (f"差し替えの疑い(サイズ上限超のため内容未確認): {att['name']}" if headers_changed else None)
        data, reason = download_capped(url, cap_bytes)
        if data is None:
            state["sha256"] = "skipped:too_large"
            return state, (f"差し替えの疑い({reason}): {att['name']}" if headers_changed else None)
    except Exception as e:
        return state, f"添付のダウンロード失敗: {att['name']}: {e}"
    state["sha256"] = sha256(data)
    state["hashed_at"] = NOW.isoformat(timespec="seconds")
    state["size"] = state["size"] or str(len(data))
    summary = _summarize_masked(url.split("?")[0], data)
    key = state["sha256"][:16]
    if summary:
        save_text(key, summary)
        state["summary_key"] = key
    if prev is None:
        return state, None
    if prev.get("sha256") and prev["sha256"] != state["sha256"]:
        old = load_text(prev.get("summary_key", ""))
        lines, total = text_diff(old, summary) if (old and summary) else ([], 0)
        detail = f"内容が差し替わりました(同じURLのまま中身が変化): {att['name']}"
        if total:
            detail += f"\n   差分 {total} 行(抜粋):\n   " + "\n   ".join(lines[:15])
        return state, detail
    return state, None


# ---- ページ1件の確認 -----------------------------------------------------------
def check_target(t, prev, deep):
    url = t["url"]
    try:
        r = http("GET", url)
        r.raise_for_status()
    except Exception as e:
        return None, [f"ページ取得失敗: {e}"], True
    main, text, last_updated = extract_main_text(r.text)
    text = mask_pii(text)
    if len(text) < 200:
        return None, ["本文が極端に短く取得されました(ブロック/構造変更の疑い)"], True
    mode = t.get("attachments", "headers")
    atts = extract_attachments(main, url, t.get("attachment_filter"), t.get("max_attachments", 80))
    cap = int(t.get("size_cap_mb", DEFAULT_SIZE_CAP_MB)) * 1024 * 1024
    thash = sha256(text.encode("utf-8"))
    sections = section_fingerprints(main)
    new_state = {"name": t["name"], "text_hash": thash, "last_updated": last_updated,
                 "sections": sections, "attachments": {}, "checked_at": NOW.isoformat(timespec="seconds")}
    changes = []
    if prev is None:
        for a in atts:
            st, _ = check_attachment(a, None, mode, deep, cap)
            new_state["attachments"][a["url"]] = st
        return new_state, ["初回: 現在の状態を基準として記録しました"], False

    # 本文(保存するのは見出し単位の指紋だけ。本文も差分行も保存しない)
    if thash != prev.get("text_hash"):
        msgs = section_diff(prev.get("sections", []), sections)
        d = f"本文が変わりました({len(msgs)}か所)"
        if last_updated and last_updated != prev.get("last_updated"):
            d += f" 最終更新日: {prev.get('last_updated') or '不明'} → {last_updated}"
        if msgs:
            d += "\n   " + "\n   ".join(msgs[:15])
        changes.append(d)

    # 添付の追加・削除
    prev_atts = prev.get("attachments", {})
    cur_urls = [a["url"] for a in atts]
    for a in atts:
        if a["url"] not in prev_atts:
            changes.append(f"資料追加: {a['name']}\n   {a['url']}")
    for u, st in prev_atts.items():
        if u not in cur_urls:
            changes.append(f"資料削除(リンクが消えました): {st.get('name', u)}")
    # 添付の内容
    for a in atts:
        st, ch = check_attachment(a, prev_atts.get(a["url"]), mode, deep, cap)
        new_state["attachments"][a["url"]] = st
        if ch and a["url"] in prev_atts:
            changes.append(ch)
    return new_state, changes, False


def load_targets():
    cfg = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    ALLOWED_HOSTS.clear()
    ALLOWED_HOSTS.update(cfg.get("allowed_hosts", []))
    return cfg


def load_page_state():
    if fc.PAGE_STATE_PATH.exists():
        try:
            return json.loads(fc.PAGE_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def probe(targets_cfg):
    rows = []
    for p in targets_cfg.get("probes", []) + [{"id": t["id"], "name": t["name"], "url": t["url"]} for t in targets_cfg["targets"]]:
        try:
            r = http("GET", p["url"])
            ok = r.status_code < 400
            _, text, lu = extract_main_text(r.text) if ok and "html" in r.headers.get("Content-Type", "") else (None, "", "")
            rows.append((p["name"], p["url"], f"HTTP {r.status_code}", f"本文{len(text)}字", lu))
        except Exception as e:
            rows.append((p["name"], p["url"], f"失敗: {e}", "", ""))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--simulate-change", default="")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    cfg = load_targets()
    if args.probe:
        rows = probe(cfg)
        md = ["| 対象 | URL | 結果 | 本文 | 最終更新日 |", "|---|---|---|---|---|"]
        for r in rows:
            md.append("| " + " | ".join(r) + " |")
        out = "\n".join(md)
        print(out)
        if args.report:
            Path(args.report).write_text(out, encoding="utf-8")
        return

    state = load_page_state()
    if args.simulate_change:
        for t in cfg["targets"]:
            if t["id"] == args.simulate_change and t["url"] in state:
                s = state[t["url"]]
                s["text_hash"] = "simulated"
                if s.get("sections"):
                    s["sections"][0]["hash"] = "simulated"
                for u in list(s["attachments"])[:1]:
                    s["attachments"][u]["sha256"] = "simulated"
                    s["attachments"][u]["size"] = "0"
                print(f"simulate-change: {t['id']} の前回状態をずらしました")

    events, feed_items, report = [], [], []
    failures, critical_failures = 0, []
    for t in cfg["targets"]:
        new_state, changes, failed = check_target(t, state.get(t["url"]), args.deep)
        if failed:
            failures += 1
            tag = "【重要】" if t.get("critical") else ""
            warn(f"{tag}{t['name']}: " + "; ".join(changes))
            if t.get("critical"):
                critical_failures.append(t["name"])
            report.append((t["name"], "取得失敗" + ("（重要対象）" if t.get("critical") else ""), changes))
            continue
        state[t["url"]] = new_state
        real = [c for c in changes if not c.startswith("初回")]
        report.append((t["name"], f"{len(real)}件の変化" if real else ("初回記録" if changes else "変化なし"), changes))
        for c in changes:
            events.append({"item_id": hashlib.md5((t["url"] + c + NOW.isoformat()).encode()).hexdigest(),
                           "event": "baseline" if c.startswith("初回") else "page_change",
                           "target": t["id"], "name": t["name"], "url": t["url"], "detail": c,
                           "tier": t.get("tier", 2)})
        if real:
            n_add = sum(1 for c in real if c.startswith("資料追加"))
            n_rep = sum(1 for c in real if "差し替" in c)
            n_txt = sum(1 for c in real if c.startswith("本文"))
            parts = []
            if n_add: parts.append(f"資料追加{n_add}")
            if n_rep: parts.append(f"差し替え{n_rep}")
            if n_txt: parts.append("本文変更")
            title = f"{t['name']}：" + "・".join(parts or ["更新"]) + f"（{NOW.strftime('%m/%d %H:%M')}）"
            feed_items.append(fc.make_item(t.get("source", "こ家庁"), "ページ更新", title, t["url"], NOW))

    fc.ensure_dirs()
    fc.PAGE_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if events:
        with fc.PAGE_EVENTS_PATH.open("a", encoding="utf-8") as f:
            for e in events:
                e["recorded_at"] = NOW.isoformat(timespec="seconds")
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    if feed_items:
        merged, added = fc.merge_items(fc.load_state(), feed_items)
        fc.write_feed(merged)
        print(f"feed: {added} item(s) added for page changes")

    md = [f"# ページ監視 結果 {NOW.strftime('%Y-%m-%d %H:%M')} (mode={fc.MODE})", ""]
    for name, status, changes in report:
        md.append(f"## {name} — {status}")
        for c in changes:
            md.append(f"- {c}")
        md.append("")
    if WARNINGS:
        md.append("## 警告")
        md += [f"- {w}" for w in WARNINGS]
    out = "\n".join(md)
    print(out)
    if args.report:
        Path(args.report).write_text(out, encoding="utf-8")
    status = "ok" if failures == 0 else ("critical_failure" if critical_failures else "partial")
    fc.write_last_run("watch_pages", status,
                      {"targets": len(cfg["targets"]), "failures": failures, "critical_failures": critical_failures,
                       "changes": len(feed_items), "warnings": WARNINGS,
                       "stages": {"fetch": status, "state_saved": "ok", "feed": "ok"}})
    if WARNINGS:
        p = fc.DATA_DIR / "run_warnings.txt"
        prev = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text((prev + "\n" if prev else "") + "\n".join(WARNINGS), encoding="utf-8")
    # Tier1(critical)の取得失敗は失敗終了(終了コード2) → ワークフローが通知を送る
    if critical_failures:
        print("ERROR: 重要対象の取得に失敗: " + ", ".join(critical_failures), file=sys.stderr)
        sys.exit(2)
    if cfg["targets"] and failures == len(cfg["targets"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
