# cfa-news-feed

こども家庭庁ウェブサイトの「新着・更新」ページを定期的に読み取り、
RSSフィード（docs/feed.xml）に変換する仕組みです。社内の情報収集用。

- 実行: GitHub Actions が日本時間 7:00〜21:00 の間、2時間おきに自動実行
- 出力: GitHub Pages 経由で feed.xml を配信
- 購読: Slack の RSS アプリで `/feed subscribe <PagesのURL>/feed.xml`

## 構成
- `scraper.py` … 新着ページの読み取りとRSS生成
- `.github/workflows/build-feed.yml` … 定期実行の設定
- `docs/` … 生成されたフィードの置き場所（GitHub Pagesで公開）

---

## 第2期改修（2026年10月〜）

### 変わったこと
- こ家庁の**調達情報を除外しない**。全件を `data/history.jsonl` に保存し、保育ICT関連（`scraper.py` 冒頭の `PROCUREMENT_KEYWORDS`）だけをRSSに流す
- 新着・調達の一覧は「既知の記事だけのページが2ページ続くまで」読む（上限あり、上限到達で警告）
- こ家庁の取得失敗・0件・HTML構造不一致は**失敗**として終了し、GitHubのメール通知（＋任意でSlack）が飛ぶ
- `watch_pages.py` が `watch_targets.json` のページ（保活基盤・施設管理PF・ここdeサーチ・データ標準など）を1日2回巡回し、本文と添付（同じURLのまま中身が変わる場合も含む）の変化を検知してRSSに1件追加
- 実行間隔は60分。実行結果は `data/last_run.json` に記録し、別ワークフロー `heartbeat-check` が未起動を検知
- 復元は `backfill.py`（履歴にだけ書く。RSS・Slackには流れない）

### ファイル
| ファイル | 役割 |
|---|---|
| `scraper.py` | 新着・調達・4省庁RSSの取得、履歴追記、RSS更新 |
| `watch_pages.py` / `watch_targets.json` | 重要ページ・添付の変化検知 / その対象一覧 |
| `backfill.py` | 過去分の復元（独立実行） |
| `feedcore.py` | 共通部品（RSS生成・履歴追記・実行記録） |
| `docs/feed.xml` `docs/feed_items.json` | RSS（第1期と同じURL・形式） |
| `data/history.jsonl` | 全件履歴（追記のみ） |
| `data/pages_state.json` `data/page_events.jsonl` | ページ監視の状態と検知記録 |
| `data/cases.json` | 関連調達の案件追跡（調達案件番号キー） |
| `.github/workflows/test-run.yml` | 本番に触れないテスト（`FEED_MODE=test`、出力は `data/test/`） |

### 戻し方（ロールバック）
1. GitHubの Releases にある `v1-stable` が改修前の状態
2. 問題時は該当コミットの「Revert」で戻す。`docs/feed.xml` のURLは変わらないのでSlack側の作業は不要
3. `data/` 配下は追記専用で、`docs/` とは独立。履歴側の不具合はRSSに影響しない
