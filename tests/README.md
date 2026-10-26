# テストの再現手順

## ローカル検証（ネットワーク不要・24項目）
- Python 3.12（3.10以上で動作見込み）
- `pip install -r requirements.txt`（requests / beautifulsoup4 / feedparser / openpyxl / pypdf）
- リポジトリ直下で `python tests/test_local.py`
- 最終行に `合格 51 / 51` と出れば成功。出力先は `data/test/` のみ
- 同じ内容は GitHub Actions の `test-run` でも自動実行され、`data/test/local_tests.txt` に残る

## 本番相当テスト（GitHub Actions `test-run`）
- `v2-reliability` ブランチへの push で自動実行。実行画面の「Summary」に合否表が出る
- 判定は A.取得 / B.検知・保存 / C.通知・安全 に分けて表示
- `FEED_MODE=test` のため `docs/feed.xml` は変更されない（最終ステップで指紋を検算）
