# -*- coding: utf-8 -*-
"""テスト実行(FEED_MODE=test)の結果を「取得」「検知」「実行時間」「通知・安全」に分けて合否表にする。"""
import json
import re
from pathlib import Path

T = Path("data/test")
out = ["# テスト結果まとめ（本番には触れていません）", ""]
MARK = {True: "✅ 合格", False: "❌ 要確認", None: "⏸ 未実施／要手動確認／情報"}


def read(p):
    p = T / p
    return p.read_text(encoding="utf-8") if p.exists() else ""


def sections(md):
    """watch_pages のレポートを {対象名: (結果, 本文)} に分解する（'## 名前 — 結果' 見出し単位）"""
    res = {}
    for m in re.finditer(r"^## (.+?) — (.+?)$\n(.*?)(?=^## |\Z)", md, re.M | re.S):
        res[m.group(1).strip()] = (m.group(2).strip(), m.group(3))
    return res


def find(secs, prefix):
    for k, v in secs.items():
        if k.startswith(prefix):
            return k, v
    return None, (None, "")


def table(title, rows):
    out.append(f"## {title}")
    out.append("| 確認項目 | 判定 | 根拠 |")
    out.append("|---|---|---|")
    for name, ok, note in rows:
        out.append(f"| {name} | {MARK[ok]} | {note} |")
    out.append("")


steps = dict(kv.split("=") for kv in read("steps.txt").split() if "=" in kv)
hist, items = read("history.jsonl"), read("feed_items.json")
w1, w3 = read("watch_1.md"), read("watch_3_timing.md")
w2 = {"shisetu_pf": read("watch_2a.md"), "kokodesearch_wam": read("watch_2b.md"), "digital_spec": read("watch_2c.md")}
sim = dict(re.findall(r"SIMULATION_RESULT id=(\S+) detected=(\S+)", read("sim.txt")))
probe, local = read("probe.md"), read("local_tests.txt")
try:
    lr = json.loads(read("last_run.json"))
except Exception:
    lr = {}
s = lr.get("scraper", {}); sd = s.get("details", {})
S1 = sections(w1)

# ---- A. 取得 ----
rows = []
last = local.strip().splitlines()[-1] if local.strip() else ""
rows.append(("ローカル検証48項目", ("合格 48 / 48" in local) if local else None, last or "記録なし(ファイル未出力)"))
rows.append(("新着取得(scraper.py)の完走", steps.get("fetch") == "success" and s.get("status") == "ok",
             f"status={s.get('status')} 段階={sd.get('stages')} 新着{sd.get('news_new')}件/調達{sd.get('proc_new')}件"))
target = "保育業務施設管理プラットフォームの軽微改修業務等"
rows.append(("10/13 施設管理PF 入札公告を取得", target in hist, ("履歴に保存" if target in hist else "履歴に無し") + ("・RSS候補にも入った" if target in items else "")))
for prefix in ["保活情報連携基盤", "保育業務施設管理PF", "ここdeサーチ WAM連絡板（施設向け）", "デジタル庁 データ要件・連携要件の標準仕様", "保育DX 全国意見照会", "デジタル庁 共通機能の標準仕様", "こ家庁 調達ポータル案内"]:
    k, (st, body) = find(S1, prefix)
    ok = st is not None and st.startswith(("初回記録", "変化なし", "0件", "1件", "2件", "3件", "4件", "5件", "6件", "7件", "8件", "9件"))
    note = f"{st}" if st else "レポートに該当なし"
    if st and not ok:
        note += " / " + " ".join(l.strip("- ").strip() for l in body.strip().splitlines()[:2])
    rows.append((f"Tier1ページ取得: {prefix}", ok if st else False, note[:200]))
for name in ["政府調達ポータル 案件ページ例", "子ども・子育て支援等分科会"]:
    line = next((l for l in probe.splitlines() if name in l), "")
    code = re.search(r"HTTP (\d+)", line)
    ok = True if (code and code.group(1) == "200") else (None if code and code.group(1) in ("401", "403") else False)
    rows.append((f"到達性(参考): {name}", ok, (line.replace("|", "/")[:120] or "未実施") + (" → アクセス制限のため対象外" if ok is None else "")))
table("A. 取得", rows)

# ---- B. 検知・保存 ----
rows = []
for tid, label in [("shisetu_pf", "施設管理PF"), ("kokodesearch_wam", "WAM連絡板"), ("digital_spec", "デジタル庁標準仕様")]:
    det = sim.get(tid)
    if det is None:   # 機械判定行が無ければレポート本文から判定
        secs = sections(w2[tid]); k, (st, body) = find(secs, {"shisetu_pf": "保育業務施設管理PF", "kokodesearch_wam": "ここdeサーチ WAM連絡板（施設向け）", "digital_spec": "デジタル庁 データ要件・連携要件の標準仕様"}[tid])
        det = "+".join([x for x, key in (("本文", "本文が変わりました"), ("添付", "差し替")) if key in body]) or ("none" if st else None)
    rows.append((f"{label}: 疑似変更を検知(オフライン)", (det not in (None, "none")) if det is not None else None,
                 f"検知: {det}" if det not in (None, "none") else ("検知できず" if det == "none" else "記録なし")))
others = [k for k, (st, _) in sections(w2["shisetu_pf"]).items() if st.startswith("変化なし")]
rows.append(("変化なしの対象で誤報しない", len(others) > 0, f"『変化なし』{len(others)}件"))
n_lines = len([l for l in hist.splitlines() if l.strip()])
n_proc = sum(1 for l in hist.splitlines() if '"category": "調達情報"' in l)
n_rel = sum(1 for l in hist.splitlines() if '"category": "調達情報"' in l and '"feed_eligible": true' in l)
rows.append(("履歴への保存(調達は全件、配信は関連のみ)", n_lines > 0 and n_proc > 0, f"履歴{n_lines}件 / 調達{n_proc}件 / うち関連{n_rel}件"))
# ページ上限の扱い
ns, ps = sd.get("news_stop"), sd.get("proc_stop")
def stop_note(x):
    return {"known_pages": "既知の記事に到達して停止(正常)", "page_limit": "上限到達(初回・既知なしでは想定どおり。通常運用で出れば警告通知)", "": "未実行"}.get(x, str(x))
rows.append(("新着一覧の停止理由", ns == "known_pages" if ns else None, f"{ns}: {stop_note(ns)} ({sd.get('news_pages')}ページ)"))
rows.append(("調達一覧の停止理由", True if ps in ("known_pages", "page_limit") else None, f"{ps}: {stop_note(ps)} ({sd.get('proc_pages')}ページ)。初回は履歴が空のため上限到達が正常"))
ft = read("failtest.txt").strip()
wf = read("watch_fail.md")
rows.append(("取得失敗時のエラー判定(重要対象が取れない→失敗終了コード2＋警告)", ft == "exit=2" and "【重要】" in wf if ft else None,
             f"{ft or '記録なし'} / " + ("警告文あり" if "【重要】" in wf else "警告文なし")))
table("B. 検知・保存", rows)

# ---- B2. 実行時間 ----
def total_sec(md):
    m = re.search(r"合計 ([\d.]+) 秒", md)
    return float(m.group(1)) if m else None
rows = []
t1, t3 = total_sec(w1), total_sec(w3)
rows.append(("初回(基準記録・添付ダウンロードあり)の所要", (t1 <= 600) if t1 is not None else None, f"{t1} 秒(上限600)" if t1 is not None else "記録なし"))
rows.append(("通常監視(2回目・軽量)の所要", (t3 <= 300) if t3 is not None else None, f"{t3} 秒(目標300以内)" if t3 is not None else "記録なし"))
ts = [total_sec(x) for x in w2.values()]
rows.append(("シミュレーション(オフライン)の所要", all(x is not None and x < 60 for x in ts) if any(x is not None for x in ts) else None, ", ".join(str(x) for x in ts) + " 秒"))
try:
    pend = json.loads(read("pages_state.json")).get("_pending", [])
    rows.append(("時間上限で未処理になった対象", None if pend else True, ("次回に回す: " + ", ".join(pend)) if pend else "なし"))
except Exception:
    pass
table("B2. 実行時間", rows)

# ---- C. 通知・安全 ----
rows = []
rows.append(("Slack通知の試験", None, "未実施（テスト用ワークフローはSecretsを持たない。本番反映後に heartbeat-check の force_fail で確認）"))
rows.append(("GitHubメール通知", None, "未実施。本番反映後に heartbeat-check を force_fail=true で手動実行し、メール到着を目視確認"))
allowed = set()
try:
    allowed = set(json.loads(Path("watch_targets.json").read_text(encoding="utf-8")).get("allowed_hosts", []))
    import importlib, sys
    sys.path.insert(0, "."); allowed |= set(importlib.import_module("scraper").ALLOWED_HOSTS)
except Exception:
    pass
hosts = sorted(set(h.strip() for h in read("hosts_accessed.txt").splitlines() if h.strip()))
bad = [h for h in hosts if h not in allowed]
rows.append(("実際の接続先がすべて許可ホスト内", (not bad) if hosts else None,
             (f"接続先 {len(hosts)} ホスト: " + ", ".join(hosts)) + (f" / 許可外: {bad}" if bad else "") if hosts else "接続記録なし"))
rows.append(("本番ファイル不変の検算", None, "ワークフローの『本番ファイルが不変であることを検算』ステップの結果を参照"))
rows.append(("出力への秘密情報混入なし", None, "ワークフローの『秘密情報が出力に混入していないか検算』ステップの結果を参照"))
table("C. 通知・安全", rows)

out += ["## 詳細", "", "### URL到達性", probe, "", "### 新着取得の実行記録", "```", json.dumps(s, ensure_ascii=False, indent=1)[:3000], "```", "",
        "### ページ監視(初回)", w1[:6000], "", "### ページ監視(疑似変更・オフライン)", "\n".join(w2.values())[:12000], "", "### ページ監視(通常・2回目)", w3[:4000],
        "", "### ローカル検証ログ", "```", local[:4000], "```"]
print("\n".join(out))
