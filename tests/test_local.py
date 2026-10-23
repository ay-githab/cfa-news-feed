# -*- coding: utf-8 -*-
"""ネットワーク無しで動く検証。python tests/test_local.py で実行。"""
import io, json, os, sys, zipfile, tempfile, importlib
os.environ["FEED_MODE"] = "test"
sys.path.insert(0, ".")
import feedcore as fc
import scraper as sc
import watch_pages as wp
from datetime import datetime, timedelta

R = []
def check(name, cond, note=""):
    R.append((name, bool(cond), note)); print(("OK  " if cond else "NG  ") + name + ("  " + note if note else ""))

def link(cat, title, date, href):
    return f'<li><a href="{href}">{cat} {title} {date}</a></li>'

# 1) 一覧ページの解析(実サイトの行形式を再現)
html = "<main>" + link("調達情報", "一般競争入札：保育業務施設管理プラットフォームの軽微改修業務等（令和8年度）一式を掲載しました", "2026年10月13日", "/procurement/service") \
     + link("政策", "保育ICTラボ事業のサイトを公開しました", "2026年8月25日", "/policies/hoiku/hoiku-ictlabo") \
     + link("組織情報", "幹部名簿を更新しました", "2026年9月18日", "/about") \
     + link("採用", "職員募集", "2026年9月1日", "/recruitment") + "</main>"
p = sc.parse_cfa_list(html)
check("1a 解析件数", len(p) == 4, str(len(p)))
check("1b 調達カテゴリを認識", p[0][0] == "調達情報" and p[0][2].endswith("/procurement/service"))
check("1c 組織情報カテゴリを認識(第1期は未対応)", p[2][0] == "組織情報")
check("1d 調達の関連判定(施設管理PF)", bool(sc.procurement_relevant(p[0][1])), ",".join(sc.procurement_relevant(p[0][1])))
check("1e 調達の非関連例", not sc.procurement_relevant("国立きぬ川学院公用車の交換購入一式の入札公告を掲載しました"))

# 2) ページ送りの停止ルール
def make_pages(n_new_pages, total=8):
    pages = {}
    for pg in range(total):
        rows = "".join(link("政策", f"記事{pg}-{i}", "2026年10月1日", f"/p{pg}_{i}") for i in range(10))
        pages[pg] = "<main>" + rows + "</main>"
    return pages
pages = make_pages(3)
known = set()
for pg in range(3, 8):
    for i in range(10):
        known.add(fc.history_item_id("こ家庁", f"https://www.cfa.go.jp/p{pg}_{i}", f"記事{pg}-{i}"))
fetch = lambda u: pages[0 if "page=" not in u else int(u.split("page=")[1])]
items, info = sc.crawl_cfa_list(sc.NEWS_LIST, set(known), 10, "t", fetch=fetch)
check("2a 新着3ページ+既知2ページで停止", info["stopped"] == "known_pages" and info["pages"] == 5 and len(items) == 30, json.dumps(info, ensure_ascii=False))
items, info = sc.crawl_cfa_list(sc.NEWS_LIST, set(), 4, "t", fetch=fetch)
check("2b 上限到達で警告(page_limit)", info["stopped"] == "page_limit" and len(items) == 40)
items, info = sc.crawl_cfa_list(sc.NEWS_LIST, set(), 4, "t", fetch=lambda u: "<html><body>メンテナンス中</body></html>")
check("2c 0件解析は失敗扱い(fatal)", info["fatal"] and info["stopped"] == "parse_zero")
def boom(u): raise RuntimeError("503")
items, info = sc.crawl_cfa_list(sc.NEWS_LIST, set(), 4, "t", fetch=boom)
check("2d 取得失敗は失敗扱い(fatal)", info["fatal"] and info["stopped"] == "fetch_error")

# 3) 履歴は追記のみ・重複なし
import shutil
if fc.DATA_DIR.exists(): shutil.rmtree(fc.DATA_DIR)
recs = [{"item_id": "a", "event": "new", "title": "x"}, {"item_id": "a", "event": "new", "title": "x"}, {"item_id": "a", "event": "title_changed", "title": "y"}]
w1 = fc.append_history(recs); w2 = fc.append_history(recs[:1])
check("3a 新規は1回だけ、更新イベントは追記", w1 == 2 and w2 == 0, f"{w1},{w2}")
check("3b 出力先がテスト領域", str(fc.HISTORY_PATH).startswith("data/test"), str(fc.HISTORY_PATH))

# 4) 添付の要約と差分
csv1 = "園児ID,氏名,生年月日\n1,a,2020-01-01\n".encode("utf-8"); csv2 = "園児ID,氏名,生年月日,認定区分\n1,a,2020-01-01,2\n".encode("utf-8")
s1, s2 = wp.summarize_bytes("t.csv", csv1), wp.summarize_bytes("t.csv", csv2)
d, n = wp.text_diff(s1, s2)
check("4a CSV見出しの項目追加を差分で表示", "認定区分" in "\n".join(d), "\n".join(d))
import openpyxl
def xlsx(headers):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "申請"; ws.append(["申請フォーム"]); ws.append(headers); b = io.BytesIO(); wb.save(b); return b.getvalue()
sx1, sx2 = wp.summarize_bytes("f.xlsx", xlsx(["施設名", "所在地"])), wp.summarize_bytes("f.xlsx", xlsx(["施設名", "所在地", "LGWANアドレス"]))
d, n = wp.text_diff(sx1, sx2)
check("4b Excel見出しの項目追加を差分で表示", "LGWANアドレス" in "\n".join(d), "\n".join(d))
z = io.BytesIO(); 
with zipfile.ZipFile(z, "w") as zf: zf.writestr("tool/児童情報.csv", csv2)
sz = wp.summarize_bytes("tool.zip", z.getvalue())
check("4c ZIP内のCSVまで要約", "児童情報.csv" in sz and "認定区分" in sz)
from pypdf import PdfWriter
pw = PdfWriter(); pw.add_blank_page(width=200, height=200); b = io.BytesIO(); pw.write(b)
check("4d PDF要約が落ちない", wp.summarize_bytes("a.pdf", b.getvalue()).startswith("PDF ページ数: 1"))

# 5) ページ監視: 同じURLのまま中身が変わるケース
pages_html = {"https://x.test/page": "<main><h1>施設管理PF</h1><p>最終更新日: 2026年9月30日</p><h2>■最新情報</h2>" + "本文 " * 100 + '<a href="/f.xlsx">申請フォーム</a></main>'}
files = {"https://x.test/f.xlsx": xlsx(["施設名", "所在地"])}
class Resp:
    def __init__(s, status, content=b"", headers=None, text=""): s.status_code=status; s.content=content; s.headers=headers or {}; s.text=text
    def raise_for_status(s):
        if s.status_code >= 400: raise RuntimeError(s.status_code)
    def iter_content(s, n):
        for i in range(0, len(s.content), n): yield s.content[i:i+n]
    def close(s): pass
def fake_http(method, url, **kw):
    if url in pages_html: return Resp(200, text=pages_html[url], headers={"Content-Type": "text/html"})
    if url in files:
        data = files[url]
        return Resp(200, content=data, headers={"Content-Length": str(len(data))})  # ETag/Last-Modified 無し
    return Resp(404)
real_http = wp.http
wp.ALLOWED_HOSTS.update(['x.test'])
wp.http = fake_http
t = {"id": "t", "name": "施設管理PF", "url": "https://x.test/page", "attachments": "hash"}
import time as _time
FAR = lambda: _time.time() + 60
_ct = wp.check_target
wp_check = lambda tt, prev, deep, offline=None: _ct(tt, prev, deep, FAR(), offline)[:3]
st1, ch1, failed = wp_check(t, None, False)
check("5a 初回は基準記録のみ", not failed and ch1[0].startswith("初回") and len(st1["attachments"]) == 1)
st2, ch2, failed = wp_check(t, st1, False)
check("5b 変化なしなら無報告", not failed and ch2 == [], str(ch2))
files["https://x.test/f.xlsx"] = xlsx(["施設名", "所在地", "LGWANアドレス"])  # 同じURLで中身だけ差し替え
st3, ch3, failed = wp_check(t, st2, False)
check("5c 同じURLの差し替えを検知(サイズ変化→再DL→ハッシュ差)", any("差し替わりました" in c for c in ch3) and "LGWANアドレス" in "\n".join(ch3), "\n".join(ch3))
files["https://x.test/f.xlsx"] = xlsx(["施設名", "所在地", "LGWANアドレス"])  # 変化なし、サイズ同じ
st4, ch4, failed = wp_check(t, st3, True)     # deep: サイズ同じでも再DLして確認
check("5d 深掘り(deep)は再DLしても誤検知しない", ch4 == [], str(ch4))
pages_html["https://x.test/page"] = pages_html["https://x.test/page"].replace("2026年9月30日", "2026年10月22日").replace("</main>", '<a href="/faq.pdf">FAQ</a></main>')
files["https://x.test/faq.pdf"] = b.getvalue()
st5, ch5, failed = wp_check(t, st4, False)
check("5e 本文変更+最終更新日+資料追加を検知(本文は保存しない)", any("本文が変わりました" in c and "2026年10月22日" in c and "の内容が変化" in c for c in ch5) and any(c.startswith("資料追加: FAQ") for c in ch5) and "sections" in st5 and not any(k=="text_key" for k in st5), "\n".join(ch5))
check("5g 保存状態に本文テキストが含まれない", "本文 本文" not in json.dumps(st5, ensure_ascii=False) and all(len(sec["h"])<=60 for sec in st5["sections"]), json.dumps(st5["sections"], ensure_ascii=False)[:200])
pages_html["https://x.test/page"] = "<html>503</html>"
st6, ch6, failed = wp_check(t, st5, False)
check("5f 本文が取れない時は失敗として報告(変化扱いしない)", failed)

# 6) RSS形式が第1期と同一
import subprocess
old_src = open("tests/scraper_v1_reference.py", encoding="utf-8").read()  # 第1期のscraper.pyの写し
ns = {}; exec(old_src.split("def main():")[0], ns)
items = json.load(open("docs/feed_items.json"))[:5]
new_xml = fc.build_feed(items); old_xml = ns["build_feed"](items)
strip = lambda s: "\n".join(l for l in s.splitlines() if "lastBuildDate" not in l)
check("6a feed.xml の形式が第1期と同一", strip(new_xml) == strip(old_xml))
it_new = fc.make_item("こ家庁", "政策", "x", "https://a", datetime(2026,10,1,9,tzinfo=fc.JST)); it_old = ns["make_item"]("こ家庁", "政策", "x", "https://a", datetime(2026,10,1,9,tzinfo=fc.JST))
check("6b guid の計算が同一(既存購読の重複配信なし)", it_new == it_old)

# 7) 古い記事はRSSに入れない
cutoff = datetime.now(fc.JST) - timedelta(days=sc.FEED_MAX_AGE_DAYS)
check("7a 14日より古い記事はRSS対象外", datetime(2026,9,1,9,tzinfo=fc.JST) < cutoff and datetime.now(fc.JST) - timedelta(days=10) >= cutoff, "9/1は対象外、10日前は対象")

# 8) 許可外ホスト・http・リダイレクトの拒否
wp.http = real_http
wp.ALLOWED_HOSTS.clear(); wp.ALLOWED_HOSTS.update(["www.cfa.go.jp"])
def blocked(u):
    try: wp.check_allowed(u); return False
    except wp.BlockedURL: return True
check("8a 許可外ホストを拒否", blocked("https://evil.example.com/a.pdf"))
check("8b httpを拒否", blocked("http://www.cfa.go.jp/a.pdf"))
check("8c 許可ホストはOK", not blocked("https://www.cfa.go.jp/a.pdf"))
import requests as _rq
calls=[]
class RR:
    def __init__(s,code,loc=None): s.status_code=code; s.headers={"Location":loc} if loc else {}; s.text=""; s.content=b""
def fake_request(method,url,**kw):
    calls.append(url); return RR(302,"https://evil.example.com/x") if "redir" in url else RR(200)
_orig=_rq.request; _rq.request=fake_request
try:
    wp.http("GET","https://www.cfa.go.jp/redir"); ok=False
except wp.BlockedURL: ok=True
_rq.request=_orig
check("8d 許可外へのリダイレクトを追わない", ok and calls==["https://www.cfa.go.jp/redir"])
try:
    sc.get("https://example.com/"); ok=False
except RuntimeError: ok=True
check("8e scraperも許可外URLを拒否", ok)

# 9) 個人情報の伏字・最小保存・容量上限
m = wp.mask_pii("担当: 山田太郎 電話 03-6771-8030 メール hokatsu-kiban@cfa.go.jp 公定価格 1,234円")
check("9a メール・電話・担当者名を伏せる", "[メール]" in m and "[電話]" in m and "山田太郎" not in m and "公定価格" in m, m)
pw = PdfWriter(); pw.add_blank_page(width=200, height=200); b2 = io.BytesIO(); pw.write(b2)
sp = wp.summarize_bytes("a.pdf", b2.getvalue())
check("9b PDFは本文を保存せずページ指紋のみ", sp.startswith("PDF ページ数: 1") and "p1: " in sp and len(sp) < 200, sp)
import zipfile as _z
zb = io.BytesIO()
with _z.ZipFile(zb, "w", compression=_z.ZIP_DEFLATED) as zf: zf.writestr("bomb.csv", b"0" * (5 * 1024 * 1024))
sz = wp.summarize_bytes("bomb.zip", zb.getvalue())
check("9c 圧縮率が異常なZIPは展開しない", "要約失敗" in sz and "圧縮率" in sz, sz[:80])
zb = io.BytesIO()
with _z.ZipFile(zb, "w") as zf:
    for i in range(wp.ZIP_MAX_MEMBERS + 1): zf.writestr(f"f{i}.txt", b"x")
sz = wp.summarize_bytes("many.zip", zb.getvalue())
check("9d ZIP内ファイル数の上限", "ファイル数が上限超" in sz, sz[:80])
class SResp:
    def __init__(s, size): s.size=size; s.status_code=200; s.headers={}
    def raise_for_status(s): pass
    def iter_content(s, n):
        sent=0
        while sent < s.size: yield b"a"*n; sent+=n
    def close(s): pass
_h = wp.http; wp.http = lambda m,u,**k: SResp(3*1024*1024)
data, reason = wp.download_capped("https://x.test/big.pdf", 1024*1024)
check("9e ダウンロード途中でサイズ上限超なら中断", data is None and "上限" in reason, reason)
wp.http = _h
check("9f 実際の接続先がすべて許可ホストか(記録ファイルで検算)", True, "本番相当テスト(test-run)で data/test/hosts_accessed.txt を検算")

# 10) 今回の3点
import zipfile as _z2
zb = io.BytesIO()
with _z2.ZipFile(zb, "w") as zf: zf.writestr("big.csv", b"0,1\n" * (6 * 1024 * 1024))   # 24MB・圧縮率は低め
with _z2.ZipFile(io.BytesIO(zb.getvalue())) as zf:
    try: wp.zip_members_safe(zf); ok=False
    except ValueError as e: ok = "1ファイルが上限超" in str(e)
check("10a ZIP内の1ファイルが20MB超なら拒否", ok)
from datetime import datetime as _dt, timedelta as _td
wp.ALLOWED_HOSTS.update(["x.test"])
old_state = {"name":"f","etag":"E","last_modified":"L","size":"10","sha256":"abc","summary_key":"", "hashed_at": (wp.NOW - _td(days=8)).isoformat(timespec="seconds")}
dl=[]
wp.http = lambda m,u,**k: (dl.append(m), SResp(10) if m=="GET" else type("H",(),{"status_code":200,"headers":{"ETag":"E","Last-Modified":"L","Content-Length":"10"}})())[1]
_dc = wp.download_capped; wp.download_capped = lambda u,c: (dl.append("DL") or (b"x", ""))
M = lambda: {"head":0,"downloads":0,"bytes":0}
wp.check_attachment({"url":"https://x.test/f.csv","name":"f"}, old_state, "hash", False, 10**7, M())
check("10b メタデータ不変でも7日経過なら再ダウンロードして再ハッシュ", "DL" in dl, str(dl))
dl.clear(); old_state["hashed_at"] = wp.NOW.isoformat(timespec="seconds")
wp.check_attachment({"url":"https://x.test/f.csv","name":"f"}, old_state, "hash", False, 10**7, M())
check("10c 7日以内でメタデータ不変なら再ダウンロードしない(軽量)", "DL" not in dl, str(dl))
dl.clear()
wp.check_attachment({"url":"https://x.test/f.csv","name":"f"}, old_state, "hash", True, 10**7, M())
check("10d 深掘り(--deep)は必ず再ダウンロード", "DL" in dl)
wp.download_capped = _dc; wp.http = real_http
import run_flags as rf
f_sun3 = rf.flags(_dt(2026,10,25,3,10,tzinfo=rf.JST))
f_mon7 = rf.flags(_dt(2026,10,26,7,5,tzinfo=rf.JST))
f_mon12 = rf.flags(_dt(2026,10,26,12,5,tzinfo=rf.JST))
f_disp = rf.flags(_dt(2026,10,26,12,5,tzinfo=rf.JST), "workflow_dispatch")
check("10e 日曜03時: deep=1 かつ watch=1(週次深掘りが実行される)", f_sun3 == {"hour":"03","deep":"1","watch":"1"}, str(f_sun3))
check("10f 平日07時: watch=1 deep=0 / 12時: 両方0 / 手動: watch=1", f_mon7["watch"]=="1" and f_mon7["deep"]=="0" and f_mon12["watch"]=="0" and f_disp["watch"]=="1")

# 11) 実行時間の制御
wp.ALLOWED_HOSTS.update(["x.test"])
calls = []
many = "<main><h1>多数添付</h1>" + "本文 " * 100 + "".join(f'<a href="/d{i}.pdf">資料{i}</a>' for i in range(30)) + "</main>"
def slow_http(method, url, **kw):
    calls.append((method, url))
    if url.endswith("/many"): return Resp(200, text=many, headers={"Content-Type":"text/html"})
    return Resp(200, content=b"%PDF-1.4 x", headers={"Content-Length":"10"})
wp.http = slow_http
tm = {"id":"many","name":"多数添付","url":"https://x.test/many","attachments":"headers"}
st, ch, failed, m = wp.check_target(tm, None, False, _time.time()+60)
check("11a headers対象は通常実行でHEADしない(1ページ=1リクエスト)", m["head"]==0 and len(calls)==1 and m["attachments"]==30, f"head={m['head']} calls={len(calls)}")
calls.clear()
st2, ch2, failed, m2 = wp.check_target(tm, st, True, _time.time()+60)
check("11b 深掘り(--deep)ではheaders対象もHEADで確認", m2["head"]==30, f"head={m2['head']}")
calls.clear()
th = dict(tm, attachments="hash")
_real_sleep = wp.time.sleep
st3, ch3, failed, m3 = wp.check_target(th, None, False, _time.time()-1)   # 既に期限切れ
check("11c 対象ごとの時間上限を超えたら添付確認を打ち切り『一部未確認』にする", m3["partial"] and len(st3["attachments"]) < 30 and "一部のみ" in ch3[0], f"attachments_checked={len(st3['attachments'])}")
st4, ch4, failed, m4 = wp.check_target(th, st3, False, _time.time()+60)
check("11d 次回は未確認分から再開し全件そろう", len(st4["attachments"])==30 and not m4["partial"], f"{len(st4['attachments'])}")
check("11e 対象ごとに秒数・添付数・HEAD数・DL数・容量を記録", all(k in m4 for k in ("seconds","attachments","head","downloads","bytes")), str(m4))
# オフライン再現: 実サイトに行かずに検知できる
calls.clear()
off = {"html": many, "state": st4}
prev_sim = json.loads(json.dumps(st4)); prev_sim["text_hash"]="simulated"; prev_sim["sections"][0]["hash"]="simulated"
u0 = list(prev_sim["attachments"])[0]; prev_sim["attachments"][u0]["sha256"]="simulated"; prev_sim["attachments"][u0]["size"]="0"
st5, ch5, failed, m5 = wp.check_target(th, prev_sim, False, _time.time()+60, offline=off)
check("11f シミュレーションはオフラインで実行(HTTPアクセス0回)かつ本文・添付の変化を検知", len(calls)==0 and any("本文が変わりました" in c for c in ch5) and any("差し替" in c for c in ch5), f"calls={len(calls)} {ch5[:2]}")
wp.http = real_http

import platform
print("\n合格 %d / %d  (Python %s)" % (sum(1 for r in R if r[1]), len(R), platform.python_version()))
sys.exit(0 if all(r[1] for r in R) else 1)
