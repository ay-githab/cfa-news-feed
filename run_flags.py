# -*- coding: utf-8 -*-
"""
run_flags.py — build-feed ワークフローの「今回は何をするか」を決める。
  deep=1  : 日曜 03時台(JST) → 新着のページ上限を広げ、ページ監視は --deep(全添付を再ハッシュ)
  watch=1 : 07時台・17時台(JST)、手動実行、または deep=1 のとき → ページ監視を実行
テスト用: 環境変数 FLAGS_NOW="2026-10-25T03:10" で日時を差し替え、FLAGS_EVENT="workflow_dispatch" で手動扱い。
"""
import os
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))


def flags(now=None, event="schedule"):
    now = now or datetime.now(JST)
    deep = now.isoweekday() == 7 and now.hour == 3
    watch = deep or now.hour in (7, 17) or event == "workflow_dispatch"
    return {"hour": f"{now.hour:02d}", "deep": "1" if deep else "0", "watch": "1" if watch else "0"}


if __name__ == "__main__":
    n = os.environ.get("FLAGS_NOW")
    now = datetime.fromisoformat(n).replace(tzinfo=JST) if n else None
    for k, v in flags(now, os.environ.get("FLAGS_EVENT", "schedule")).items():
        print(f"{k}={v}")
