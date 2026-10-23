# アップロード配置表（v2-reliability ブランチ用）

| ファイル／フォルダ | GitHub上の置き場所 | 区分 | 本番(main)への影響 |
|---|---|---|---|
| scraper.py | リポジトリ直下 | 上書き | なし（ブランチのみ） |
| run_flags.py | リポジトリ直下 | 新規 | なし |
| README.md | リポジトリ直下 | 上書き | なし |
| feedcore.py / watch_pages.py / watch_targets.json / backfill.py / requirements.txt / SECURITY.md / UPLOAD_MAP.md | リポジトリ直下 | 新規 | なし |
| tests/（test_local.py, check_results.py, scraper_v1_reference.py, README.md, last_result.txt） | リポジトリ直下に tests フォルダごと | 新規 | なし |
| .github/workflows/test-run.yml | .github/workflows/ | 新規 | なし。mainに無いので本番では存在しない |
| .github/workflows/heartbeat.yml | .github/workflows/ | 新規 | なし。mainに無いため定期実行も手動実行もされない |
| .github/workflows/build-feed.yml | .github/workflows/ | 上書き（ブランチのみ） | なし。定期実行はmainのファイルだけが対象 |
| .github/dependabot.yml | .github/ | 新規 | なし |
| docs/feed.xml, docs/feed_items.json | （アップロードしない） | 変更なし | RSS公開はmainの docs/ から。ブランチには触れない |

## ブランチに置いた時点で動くもの・動かないもの
- 動く: test-run（ブランチへのpushで自動実行。FEED_MODE=test、読み取り専用、Secretsなし、出力は成果物のみ）
- 動かない: build-feed の定期実行（mainのファイルのみ）、heartbeat-check（mainに無い）、GitHub Pagesの更新（mainの docs/ のみ）、Slack通知（Webhook未設定・Environment未作成のためステップが素通り）
- 注意: GitHubの「Run workflow」ボタンで build-feed を選び、ブランチに v2-reliability を指定して手動実行すると、ブランチ上で新版が動き、ブランチの docs/ を更新する（mainには影響しないが、不要なので押さないこと）
