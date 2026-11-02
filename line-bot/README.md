# LINE からホームページを更新するボット

## しくみ

```
LINE ─→ 中継スクリプト（Google Apps Script）─→ このPC の poller.py（1分ごと）
                                               ├→ Claude が記事を作成
                                               ├→ WordPress に下書き保存・写真アップロード
                                               └→ LINE に「プレビュー / OK・公開する」ボタンを返信
```

- LINE からできるのは **新規投稿** と **LINE から投稿した記事の修正・公開** だけ（WordPress の「投稿者」権限で制限）。
- 新しい記事はまず下書き。公開中の記事を修正するときも、OK を押すまでは実際のページは変わらない。
- PC が起動していて、ログインしている間だけ動く。

## LINE での使い方

1. 写真・動画があれば **先に** 送る
2. 続けて、記事の内容を文章で送る（例: 「10/19 のGOBコンサート鑑賞の活動報告を書いて」）
3. 1〜2分でプレビューのボタンが届く → 直したいところがあれば文章で送る（例: 「タイトルをもっと短く」「2枚目の写真を消して」）
4. よければ **「OK・公開する」** を押す
   - 公開をやめるときは **「公開しない（下書き削除）」** を押す（または「やっぱりやめる」と送る）。下書きと、そのために送った写真・動画が削除される（記事は WordPress のゴミ箱に30日間残る）
   - 公開中の記事の修正案のときは **「修正を取り消す」**。ホームページの記事はそのまま
5. 「自分の記事の一覧」と送ると、自分が LINE から作成した記事の一覧が届く
6. 「○○の記事を削除して」と送ると確認ボタンが届き、「削除する」で削除（公開中の記事も可）。**削除できるのは作成した本人だけ**（作成者は `state.json` の `owners` に記録。この機能を入れる前の記事は作成者不明のため削除できない）

グループで使うときは、文章の先頭に `#HP` を付ける。

## 初回の設定

### 1. LINE 公式アカウント（Messaging API）
（2024年9月以降、Developers コンソールからは直接チャネルを作れないため、公式アカウント経由で作る）
1. https://entry.line.biz/form/entry/unverified で LINE 公式アカウントを作成（無料プランでよい）
2. https://manager.line.biz/ でそのアカウントを開き「設定 → Messaging API → Messaging APIを利用する」（プロバイダーを新規作成）
3. 同じく「設定 → 応答設定」で **応答メッセージをオフ**、**Webhook をオン** にする
4. https://developers.line.biz/console/ で自動作成されたチャネルを開き、「Messaging API設定」で **チャネルアクセストークン（長期）** を発行 → `config.json` の `line_channel_access_token` に貼る

### 2. 中継スクリプト（Google Apps Script）
1. Google スプレッドシートを新規作成（名前は「LINEホームページボット」など）
2. 「拡張機能 → Apps Script」を開き、`gas/Code.gs` の中身をすべて貼り付けて保存
3. 「プロジェクトの設定 → スクリプト プロパティ」に次を追加
   - `SECRET` … `config.json` の `gas_secret` と同じ値
   - `CHANNEL_ACCESS_TOKEN` … 1-4 のトークン
   - `ALLOWED_USER_IDS` … ひとまず空でよい（後で設定）
4. 「デプロイ → 新しいデプロイ → 種類: ウェブアプリ」、実行ユーザー「自分」、アクセス「全員」でデプロイ
5. 表示された URL（`https://script.google.com/macros/s/…/exec`）を `config.json` の `gas_url` に貼る
6. LINE Developers の「Webhook URL」に **`URL?key=SECRETの値`** を設定し、「Webhookの利用」をオン
   （「検証」ボタンがエラーになることがあるが、実際の受信には問題ない）
7. ボットを友だち追加してメッセージを送ると、**あなたのユーザーID** が返ってくる → `ALLOWED_USER_IDS` に設定（複数人はカンマ区切り）

※ `Code.gs` を書き換えたときは「デプロイを管理 → 編集 → 新バージョン」で更新する（URL は変わらない）。

### 3. WordPress
1. 管理画面「ユーザー → 新規追加」で、ユーザー名 `line-bot`、権限 **投稿者** のユーザーを作る
2. そのユーザーのプロフィール画面下部「アプリケーションパスワード」で新規作成 → `config.json` の `wp_app_password` に貼る
3. `config.json` の `wp_url` にホームページの URL（例: `https://example.jp`）を入れる

### 4. このPC
```powershell
python poller.py --check          # 接続確認（4項目とも表示されればOK）
powershell -ExecutionPolicy Bypass -File register_task.ps1   # 1分ごとの自動実行を登録
```

## クラウドで動かす（PC 不要）

```
LINE ─→ 中継スクリプト ─→ GitHub Actions を起動（.github/workflows/line-bot.yml）
                            └→ poller.py を実行（処理内容は PC 版と同じ）
```

- パスワード類は `config.json` ではなく GitHub の「Settings → Secrets and variables → Actions」に保存する
  （`GAS_URL` `GAS_SECRET` `LINE_CHANNEL_ACCESS_TOKEN` `WP_URL` `WP_USER` `WP_APP_PASSWORD` `CLAUDE_CODE_OAUTH_TOKEN`）
- `CLAUDE_CODE_OAUTH_TOKEN` は PC で `claude setup-token` を実行して発行する
- 処理状態は中継スクリプトのシート「state」に保存され、PC 版とクラウド版で共有される
- 中継スクリプトのスクリプト プロパティに `GITHUB_TOKEN`（このリポジトリの Contents: Read and write を許可した fine-grained トークン）と
  `GITHUB_REPO`（`ユーザー名/リポジトリ名`）を入れると、LINE 受信のたびにクラウド版が起動する。消せば起動しなくなる
- **PC 版とクラウド版を同時に動かさない**（PC 版はタスクトレイで OFF にする）
- 接続確認: GitHub の「Actions → LINE ボット → Run workflow」で「接続確認だけを行う」にチェックして実行

## タスクトレイのツール（tray.py）

タスクトレイの「HP」アイコン（緑 = ON、灰色 = OFF、赤 = 未登録）を右クリックして操作する。

- **ボットを動かす（ON）** … チェックを外すと停止。停止中に LINE で送った指示は、ON に戻したときに処理される
- **今すぐ LINE を確認** … 1分待たずにすぐ処理する
- **Windows ログイン時にこのツールを起動** … ツール自体の自動起動の ON/OFF
- **終了** … ツールを閉じる（ボットの ON/OFF はそのまま）

起動: `pythonw tray.py`（初回のみ `python -m pip install --user pystray` が必要）

## ファイル

| ファイル | 内容 |
|---|---|
| `poller.py` | 本体（LINE の指示 → Claude → WordPress） |
| `tray.py` | タスクトレイの ON/OFF ツール |
| `clients.py` | LINE・WordPress・中継スクリプトとの通信 |
| `prompt.md` | Claude への指示書（文体などを変えたいときはここを編集） |
| `config.json` | 設定とパスワード（**他人に渡さない**） |
| `gas/Code.gs` | 中継スクリプト |
| `poller.log` | 動作記録（うまく動かないときに見る） |
| `state.json` / `inbox/` | 処理状態と受け取った写真（7日で自動削除） |

## うまく動かないとき
- `poller.log` を見る
- WordPress で 401 エラー → サーバーが認証ヘッダーを止めている可能性。レンタルサーバーの「WAF」や `.htaccess` を確認
- 自動実行の停止: `Unregister-ScheduledTask LINE-HomepageBot`
