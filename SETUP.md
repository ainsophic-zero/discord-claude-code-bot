# 友人向けセットアップガイド：Discord Claude Code Bot + Mac↔Discord 同期

このリポジトリを使うと、**Mac の Claude Code セッションを Discord 経由でスマホからも操作できる**環境を構築できます。Mac で書いた応答が Discord にミラーされ、Discord から書いたメッセージが Claude に届く双方向同期です。

---

## 前提条件

- ✅ Mac で Claude Code を使ってる
- ✅ VPS を持ってる（Linux、Ubuntu 22.04+ 推奨。Oracle Cloud Always Free でもOK）
- ✅ Cloudflare アカウント（オプション、ttyd Web UI 公開する場合のみ）
- ✅ Discord アカウント

**所要時間**: 約30〜60分

---

## 全体の流れ

1. Discord アプリケーションと Bot を作る
2. VPS に bot.py と bumper.py を配置
3. systemd で常駐化
4. Mac ↔ VPS の Syncthing 設定
5. Discord に最初のフォーラム投稿が自動生成されることを確認

---

## ステップ1: Discord Bot 作成

### 1-1. Discord Developer Portal で New Application

https://discord.com/developers/applications にアクセスして「New Application」。

### 1-2. Bot Token 取得

`Bot` タブ → `Reset Token` → トークンをコピーして安全な場所に保存。**このトークンは絶対に公開しない**（GitHub に上げない）。

### 1-3. Privileged Gateway Intents を有効化

`Bot` タブの下の方：
- ✅ `MESSAGE CONTENT INTENT` を ON

### 1-4. Bot 招待 URL を生成

`OAuth2` → `URL Generator`:
- Scopes: `bot`, `applications.commands`
- Permissions: `Manage Channels`, `Manage Threads`, `Send Messages`, `Read Message History`, `Embed Links`, `Attach Files`, `Add Reactions`, `Use Slash Commands`

下に出てくる URL をコピーしてブラウザで開く → 自分のサーバーに招待。

### 1-5. 自分の Discord ユーザー ID を控える

Discord 設定 → 詳細設定 → 「開発者モード」を ON → 自分のアイコンを右クリック → 「ユーザー ID をコピー」。これを後で `.env` に書きます。

---

## ステップ2: 友人が Claude Code に貼り付けるプロンプト

ここから先は、**Claude Code に下記プロンプトを貼り付けて実行させる**ことを想定しています。VPS の SSH 設定とフォルダ構成は環境に合わせて柔軟に。

```
以下の構成を私のVPSと Mac にセットアップしてください。

【私の環境】
- VPS: <Ubuntu/Debian/CentOS 等>、IP <xxx.xxx.xxx.xxx>、SSH alias <例: my-vps>
- VPSユーザー: <例: ubuntu>
- VPS作業ディレクトリ: <例: /home/ubuntu/discord-bot> （好きな場所でOK）
- Mac作業ディレクトリ: <例: /Users/myname/dev>
- Mac の Claude Code セッション保存先: ~/.claude/projects/
- 私のDiscordユーザーID: <ステップ1-5でコピーした数字>
- DiscordサーバーID: <Discordで「サーバー設定 → 概要」または開発者モードでサーバー右クリック→「サーバーIDコピー」>
- DiscordボットToken: <ステップ1-2でコピーしたToken>

【ゴール】
GitHub の https://github.com/ainsophic-zero/discord-claude-code-bot をベースに、
1. VPS に bot.py + discord-thread-bumper.py を配置
2. systemd で常駐化（discord-claude-bot.service, discord-thread-bumper.service）
3. Mac↔VPS で ~/.claude/projects/ を Syncthing で双方向同期
4. Discord でメンション無しでも応答する設定（auto_respond）を有効化
5. セキュリティ: ALLOWED_USER_IDS で私のIDだけを許可

【手順】
1. VPS に SSH して、作業フォルダに git clone してください。
   git clone https://github.com/ainsophic-zero/discord-claude-code-bot.git
   cd discord-claude-code-bot

2. Python venv を作って依存をインストール:
   python3 -m venv venv
   source venv/bin/activate
   pip install -U pip
   pip install discord.py aiohttp claude-agent-sdk inotify-simple croniter aiofiles
   pip install Pillow openpyxl python-docx pypdf  # オプション: 添付処理用

3. .env を作成して、私の環境に合わせて埋めてください:
   cp .env.example .env
   # 編集: ALLOWED_USER_IDS, DISCORD_TOKEN, WORK_DIR を私の値に
   chmod 600 .env

4. sessions.db のパーミッションを 600 に:
   touch sessions.db && chmod 600 sessions.db

5. systemd unit ファイルを設置:
   sudo cp systemd/discord-claude-bot.service /etc/systemd/system/
   sudo cp systemd/discord-thread-bumper.service /etc/systemd/system/
   # WorkingDirectory と ExecStart のパスを私の環境に合わせて編集
   sudo sed -i 's|/home/ubuntu/discord-bot|<私のパス>|g' /etc/systemd/system/discord-*.service
   sudo systemctl daemon-reload

6. Claude Code が VPS で動くようにセットアップ:
   curl -fsSL https://claude.ai/install.sh | bash  # 公式インストーラ
   claude login  # OAuth ブラウザで認証

7. Syncthing を Mac と VPS にインストール:
   - Mac: brew install syncthing && brew services start syncthing
   - VPS: sudo apt install syncthing && systemctl --user enable --now syncthing
   - Mac の ~/.claude/projects/ を共有フォルダとして追加
   - VPS で受け入れ → 双方向同期
   - **重要**: Syncthing GUI (http://127.0.0.1:8384) にパスワードを設定すること

8. サービス起動:
   sudo systemctl enable --now discord-claude-bot discord-thread-bumper
   sudo systemctl status discord-claude-bot
   journalctl -u discord-claude-bot -f  # ログ確認

9. Discord で動作確認:
   - サーバーに bot を招待済みであること
   - 任意のテキストチャンネルで「hello」と書いて bot から応答が来るか確認
   - 来ない場合: ALLOWED_USER_IDS が正しいか確認

10. Mac↔Discord 同期テスト:
    - Mac で `claude` コマンドを起動して何か発言
    - 数秒以内に Discord に新しいフォーラム投稿（カテゴリ「<フォルダ名>」配下）が自動生成されることを確認
    - ⚡ アイコンが投稿に付くことを確認（bumper動作中の証）

【セキュリティチェック】
- [ ] .env ファイルが .gitignore に入ってる
- [ ] sessions.db が chmod 600
- [ ] ALLOWED_USER_IDS に私のID（およびDM相手）以外入ってない
- [ ] Syncthing GUI にパスワード設定済み
- [ ] (オプション) /etc/sudoers.d/90-cloud-init-users の "NOPASSWD: ALL" を必要分だけに制限
- [ ] (オプション) DEFAULT_PERMISSION_MODE を bypassPermissions から acceptEdits に変更

トラブルが出たら、journalctl のログを見せてください。フォルダ構成や VPS のディストリビューションが違っても、上記の手順を私の環境に合わせて調整してください。
```

---

## ステップ3: 動作確認

### 確認①: Bot が応答する

Discord で適当なチャンネルに `@<bot名> hello` と打って応答が返るか。

### 確認②: Mac → Discord 同期

Mac で：
```bash
cd ~/dev/<任意のフォルダ>
claude  # セッション開始
```
何か質問する → 数秒以内に Discord の Forum チャンネル「<任意のフォルダ>」配下に新しい投稿が生成されることを確認。

### 確認③: Discord → Mac 同期

Discord で生成された投稿に「Hello from Discord」と書く → bot が claude を呼び出して応答 → Mac 側の同じセッションにもメッセージが流れる。

---

## トラブルシューティング

### Bot が無反応

```bash
# bot.py のログ確認
journalctl -u discord-claude-bot -n 50

# .env の ALLOWED_USER_IDS を確認
grep ALLOWED_USER_IDS /home/<user>/discord-bot/.env

# 自分のDiscordユーザーIDが入ってるか確認
```

### Mac → Discord ミラーが動かない

```bash
# bumper のログ確認
journalctl -u discord-thread-bumper -n 50

# Syncthing が動いてる？
systemctl --user status syncthing  # VPS
brew services list | grep syncthing  # Mac

# JSONL ファイルが VPS に同期されてる？
ls -la /home/<user>/.claude/projects/
```

### Syncthing で sync-conflict が出る

bumper には `*.sync-conflict-*.jsonl` を無視するフィルタが入ってます。万一すり抜けたら：
```bash
find /home/<user>/.claude/projects -name '*.sync-conflict-*' -delete
```

### "⛔ このBotは管理者専用です"

`.env` の `ALLOWED_USER_IDS` に自分の Discord ユーザーIDが入ってない。再度確認。

---

## メンテナンス

```bash
# 更新を取り込む
cd ~/discord-bot
git pull
sudo systemctl restart discord-claude-bot discord-thread-bumper

# DB バックアップ
sqlite3 sessions.db ".backup sessions.backup.$(date +%Y%m%d).db"

# ログを直近1時間
journalctl -u discord-claude-bot --since '1 hour ago'
```

---

## 既知の制約

1. **bypassPermissions モードは強力かつ危険** — README.md のセキュリティ警告を必ず読む
2. **DM はサーバー所属に関係なく届く** — `ALLOWED_USER_IDS` で必ず自分のIDを設定
3. **Mac → Discord ミラーは「assistant text」のみ** — ツール実行結果は省略される（ミラー対象を絞ってる）
4. **JSONL ファイルが大きくなる** — 1セッションが数MB〜数十MBになる、定期的に古いセッションをアーカイブ推奨

---

## 質問・改善案

このリポジトリは https://github.com/ainsophic-zero/discord-claude-code-bot にあります。Issue や PR 歓迎。
