# アニメコラボ・イベント情報BOT

GitHub Actions + Discord Webhook によるRSSイベント記事の通知BOT。

## 初期設定
1. 新しいGitHubリポジトリ `anime-event-discord-bot` を作る。
2. このZIP内のファイルを、フォルダ構造ごとリポジトリに追加する。
3. **初回は `sent_articles.json` を作らない**。初回実行で過去記事を既読登録する。
4. DiscordチャンネルでWebhookを作成。
5. GitHubの Settings → Secrets and variables → Actions に `DISCORD_WEBHOOK_URL` を登録。
6. Settings → Actions → General → Workflow permissions で Read and write permissions を有効化。
7. Actions → Anime Event Notification → Run workflow を押す。
8. 初回実行ではDiscord投稿なし。`sent_articles.json` がコミットされれば初期化成功。
9. 次回からRSS内の新着該当記事を最大12件通知。

## 動作仕様
- 毎日12:07と20:07（JST）に自動チェック。GitHub側の都合で遅延することがある。
- 記事タイトルのキーワードで判定。全アニメを対象にするが、情報源にない記事は検出できない。
- 取得元: https://animeanime.jp/rss/index.rdf
- 記事公開を通知するBOTで、イベント開催日の通知ではない。
- RSS取得エラーはActions失敗として通知履歴を変更しない。
- 投稿成功分は履歴へ追加。失敗時もワークフロー末尾で履歴をコミットする。
- 手動テスト: `DRY_RUN=true python event_bot.py` （RSS取得のみ、Discord投稿なし）。
- `sent_articles.json` は自動生成。**最初から `[]` を置かない**。

## 注意
- RSSが無効化された場合は取得元の変更が必要。
- RSSには直近の一部記事しか含まれないため、停止中にフィードから消えた記事は取得できない。
- 記事タイトルの判定には誤検出・見逃しがある。
- GitHub Actionsの無料枠・利用条件はGitHubの規約に従う。
- Webhook URLをコードや公開ログに貼らない。
