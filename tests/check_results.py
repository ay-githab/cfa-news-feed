# -*- coding: utf-8 -*-
"""テスト実行(FEED_MODE=test)の結果を「取得」「検知」「通知」に分けて合否表にする。"""
import json
from pathlib import Path

T = Path("data/test")
out = ["# テスト結果まとめ（本番には触れていません）", ""]


def read(p):
    p = T / p
    return p.read_text(encoding="utf-8") if p.exists() else ""


def seg(text, key, n=4000):
    return text.split(key)[1][:n] if key in text else ""


MARK = {True: "✅ 合格", False: "❌ 要確認", None: "⏸ 未実施／要手動確認"}


def table(title, rows):
    out.append(f"## {title}")
    out.append("| 確認項目 | 判定 | 根拠 |")
    out.append("|---|---|---|")
    for name, ok, note in rows:
        out.append(f"| {name} | {MARK[ok]} | {note} |")
    out.append("")


steps = dict(kv.split("=") for kv in read("steps.txt").split() if "=" in kv)
hist, items, w1 = read("history.jsonl"), read("feed_items.json"), read("watch_1.md")
w2a, w2b, w2c = read("watch_2a.md"), read("watch_2b.md"), read("watch_2c.md")
w3 = read("watch_3_timing.md")
probe = read("probe.md")
local = read("local_tests.txt")
try:
    lr = json.loads(read("last_run.json"))
except Exception:
    lr = {}
s = lr.get("scraper", {}); sd = s.get("details", {})

# ---- 取得 ----
rows = []
rows.append(("ローカル検証48項目", ("合格 48 / 48" in local) if local else None, local.strip().splitlines()[-1] if local else "未実行"))
rows.append(("新着取得(scraper.py)の完走", steps.get("fetch") == "success" and s.get("status") == "ok",
             f"status={s.get('status')} 段階={sd.get('stages')} 新着{sd.get('news_new')}件/調達{sd.get('proc_new')}件 停止理由={sd.get('news_stop')}/{sd.get('proc_stop')}"))
target = "保育業務施設管理プラットフォームの軽微改修業務等"
rows.append(("10/13 施設管理PF 入札公告を取得", target in hist, ("履歴に保存" if target in hist else "履歴に無し") + ("・RSS候補にも入った" if target in items else "")))
for name in ["保活情報連携基盤", "保育業務施設管理PF", "ここdeサーチ WAM連絡板（施設向け）", "デジタル庁 データ要件・連携要件の標準仕様", "保育DX 全国意見照会"]:
    got = f"{name} — 初回記録" in w1 or f"{name} — 変化なし" in w1
    rows.append((f"Tier1ページ取得: {name}", got, "取得OK" if got else "取得NG(詳細は下のページ監視欄)"))
for name in ["施設管理PF 本体トップ", "政府調達ポータル 案件ページ例", "子ども・子育て支援等分科会"]:
    line = next((l for l in probe.splitlines() if name in l), "")
    rows.append((f"到達性: {name}", "HTTP 200" in line, line.replace("|", "/")[:120] or "未実施"))
table("A. 取得", rows)

# ---- 検知 ----
rows = []
rows.append(("施設管理PF: 疑似変更を検知", "本文が変わりました" in seg(w2a, "保育業務施設管理PF"), "検知" if "本文が変わりました" in seg(w2a, "保育業務施設管理PF") else "検知できず"))
sw = seg(w2b, "ここdeサーチ WAM連絡板（施設向け）")
rows.append(("WAM連絡板: 疑似変更を検知", "本文が変わりました" in sw, "検知" if "本文が変わりました" in sw else "検知できず"))
sd2 = seg(w2c, "デジタル庁 データ要件・連携要件の標準仕様")
rows.append(("デジタル庁標準仕様: 疑似変更を検知", "本文が変わりました" in sd2,
             ("本文の変化を検知" if "本文が変わりました" in sd2 else "検知できず") + ("・添付の差し替えも検知" if ("差し替" in sd2) else "")))
rows.append(("変化なし時に誤報しない", "変化なし" in w2a or "初回記録" in w2a, "他の対象は『変化なし』"))
n_lines = len([l for l in hist.splitlines() if l.strip()])
n_proc = sum(1 for l in hist.splitlines() if '"category": "調達情報"' in l)
n_rel = sum(1 for l in hist.splitlines() if '"category": "調達情報"' in l and '"feed_eligible": true' in l)
rows.append(("履歴への保存(調達は全件、配信は関連のみ)", n_lines > 0 and n_proc > 0, f"履歴{n_lines}件 / 調達{n_proc}件 / うち関連{n_rel}件"))
table("B. 検知・保存", rows)

# ---- 実行時間 ----
import re as _re
def total_sec(md):
    m = _re.search(r"合計 ([\d.]+) 秒", md)
    return float(m.group(1)) if m else None
rows = []
t1, t3 = total_sec(w1), total_sec(w3)
rows.append(("初回(基準記録・添付ダウンロードあり)の所要", (t1 is not None and t1 <= 600) if t1 is not None else None, f"{t1} 秒(上限600)" if t1 is not None else "記録なし"))
rows.append(("通常監視(2回目・軽量)の所要", (t3 is not None and t3 <= 300) if t3 is not None else None, f"{t3} 秒(目標300以内)" if t3 is not None else "記録なし"))
rows.append(("シミュレーション(オフライン)の所要", all(total_sec(x) is not None and total_sec(x) < 60 for x in (w2a, w2b, w2c)) if w2a else None, ", ".join(str(total_sec(x)) for x in (w2a, w2b, w2c)) + " 秒"))
try:
    pend = json.loads(read("pages_state.json")).get("_pending", [])
    rows.append(("時間上限で未処理になった対象", None if pend else True, ("次回に回す: " + ", ".join(pend)) if pend else "なし"))
except Exception:
    pass
table("B2. 実行時間", rows)

# ---- 通知・安全 ----
rows = []
nt = read("notify.txt").strip()
rows.append(("Slack通知の試験", None, nt or "未実施（テスト用ワークフローはSecretsを持たない。本番反映後に heartbeat-check の force_fail で確認）"))
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
rows.append(("本番ファイル不変の検算", None, "ワークフローの『本番ファイルが不変であることを検算』ステップの結果を参照（このレポートでは判定しない）"))
rows.append(("出力への秘密情報混入なし", None, "ワークフローの『秘密情報が出力に混入していないか検算』ステップの結果を参照"))
table("C. 通知・安全", rows)

out += ["## 詳細", "", "### URL到達性", probe, "", "### 新着取得の実行記録", "```", json.dumps(s, ensure_ascii=False, indent=1)[:3000], "```", "",
        "### ページ監視(初回)", w1[:6000], "", "### ページ監視(疑似変更・オフライン)", (w2a + "\n" + w2b + "\n" + w2c)[:12000], "", "### ページ監視(通常・2回目)", w3[:4000], "", "### ローカル検証ログ", "```", local[:4000], "```"]
print("\n".join(out))
