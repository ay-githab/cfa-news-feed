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
