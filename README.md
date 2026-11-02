# LINE × AI × WordPress 自動投稿ボット

LINE で写真と一言を送るだけで、AI（Claude）が記事を書き、WordPress に下書きを作るボットです。
LINE に届くプレビューを確認して「OK・公開する」を押すと公開されます。
GitHub Actions で動くので、ボット用のサーバーは要りません。

- しくみ・設定手順: [line-bot/README.md](line-bot/README.md)
- AI への指示書（文体など）: [line-bot/prompt.md](line-bot/prompt.md)
- 中継スクリプト（Google Apps Script）: [line-bot/gas/Code.gs](line-bot/gas/Code.gs)
- GitHub Actions のワークフロー: [.github/workflows/line-bot.yml](.github/workflows/line-bot.yml)

パスワードやトークンは `config.json`（PC 版）または GitHub の Secrets（クラウド版）に置き、リポジトリには入れないでください。
