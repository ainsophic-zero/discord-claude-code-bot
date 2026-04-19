# Discord Claude Code Bot

Self-hosted Discord bot that wraps the **Claude Code CLI** so you can use Claude Code from any device (phone, browser, Discord client) — without owning a beefy laptop.

## Features

- **Mention or DM** the bot to chat with Claude (per-channel session continuity)
- **Mac session takeover**: import an in-progress session from your Mac's Claude Code via `/threads` (uses Syncthing for `~/.claude/projects/` mirror)
- **Project-aware**: shows folder grouping in the picker, auto-resolves `cwd`
- **6 permission modes**: `bypassPermissions` (fast/dangerous), `auto` (Claude judges), `acceptEdits`, `default` (every tool asks), `plan` (read-only), `dontAsk`
- **Permission Buttons**: when in non-bypass mode, pops a Discord embed with `✅ 許可` / `🔁 常に許可` / `❌ 拒否` for any tool Claude wants to use (Bash, Write, Edit, etc). Implemented via `claude-agent-sdk` `PreToolUse` hooks (because the upstream `can_use_tool` callback has [issue #469](https://github.com/anthropics/claude-agent-sdk-python/issues/469))
- **Session control**: `/clear`, `/recent messages:N` (truncate context), `/threads_recent hours:N`
- **Personas & templates**: `/persona 執事`, `/template 議事録`, ...
- **Tool-aware**: `/files`, `/file`, `/audio` (Whisper local), `/image` (Pollinations / Nano Banana / Cloudflare)
- **`/rewind turns:N`**: git-based rollback of the work directory, with auto backup tag
- **`/thread name: prompt:`**: spin off a Discord thread for a single task; thread becomes its own session
- **Scheduling**: `/schedule_add`, `/schedule_list`, daily report
- **Reactions** for quick ops: 🔄 retry, 📋 save to Obsidian, 👍 continue, 🗑️ delete

## Requirements

- Python 3.12+
- `claude` (Claude Code CLI) installed and authenticated
- A Discord application + bot token
- *(Optional)* `claude-agent-sdk` for Permission Button support
- *(Optional)* Syncthing if you want Mac ↔ VPS session sync
- *(Optional)* Anthropic API key in `ANTHROPIC_API_KEY` if not using OAuth

## Quick start

```bash
git clone <repo>
cd discord-bot
python3 -m venv venv && source venv/bin/activate
pip install discord.py aiohttp claude-agent-sdk

cp .env.example .env
# edit .env: DISCORD_TOKEN, WORK_DIR, etc.

python -u bot.py
```

Recommended: install as systemd service. See `discord-claude-bot.service` example.

## .env

```env
DISCORD_TOKEN=...
ANTHROPIC_API_KEY=sk-ant-...   # optional
WORK_DIR=/path/to/your/projects
DB_PATH=./sessions.db
CLAUDE_BIN=claude
MAX_CONCURRENT=3
```

## Permission mode behavior

| Mode | Behavior |
|---|---|
| `bypassPermissions` | All tools auto-approved. Fastest, dangerous. Uses CLI subprocess directly. |
| `auto` | Claude decides which tools to ask about. Uses SDK + hook. |
| `acceptEdits` | File edits auto, Bash etc. asked via Discord buttons. |
| `default` | Every tool ask. Discord buttons. Read/Glob/Grep auto-allowed for sanity. |
| `plan` | Read-only. No tool can write. |
| `dontAsk` | Only pre-approved tools allowed. |

In `default` and similar modes, the bot pops `🔐 ツール実行の承認: <tool>` embed and waits up to 10 minutes. `🔁 常に許可` whitelist is per-channel-session.

## Architecture

```
[Discord] ── on_message ──▶ handle_message ──▶ run_claude ──┬─▶ subprocess `claude` (bypass mode)
                                                            └─▶ ClaudeSDKClient (other modes)
                                                                         │
                                                                         └─PreToolUse hook─▶ DiscordPermissionUI ──▶ embed+buttons
```

## License

MIT (or your choice)
