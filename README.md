# Discord Claude Code Bot

Self-hosted Discord bot that wraps the **Claude Code CLI** so you can use Claude Code from any device (phone, browser, Discord client) — without owning a beefy laptop.

Includes a companion daemon **`discord-thread-bumper`** that mirrors your Mac's Claude Code sessions to Discord forum threads in real time (Mac → Discord) and bumps thread activity timestamps so they stay visible.

---

## ⚠️ SECURITY FIRST — Read this before you run

This bot can execute arbitrary shell commands and edit any file under `WORK_DIR` on the host machine. **Treat the bot token like a root password.**

### MUST DO before going live

1. **Set `ALLOWED_USER_IDS` in `.env`** — without this, the bot rejects all messages by design (fail-secure). Only the listed Discord user IDs can talk to the bot.
2. **Never commit `.env`** — it's in `.gitignore` already; double-check before `git push`.
3. **Set `sessions.db` to mode 600** — it stores all conversations including any leaked secrets you may have pasted.
   ```bash
   chmod 600 sessions.db
   ```
4. **Restrict sudoers** — if your VPS user has `(ALL) NOPASSWD: ALL`, bot compromise = root takeover. Replace with per-command grants (see `systemd/` directory).
5. **Lock Syncthing GUI** — set a password on `http://127.0.0.1:8384`. Even though it's localhost-only, internal SSRF can reach it.

### Default permission mode is `bypassPermissions` — KNOW THE RISK

`bypassPermissions` mode auto-approves every tool Claude wants to use (Bash, Write, Edit, fetch URLs, etc). This is **fast but dangerous**:

- Compromised bot token → attacker can `rm -rf`, exfiltrate `.env`, install backdoors via Claude
- Even legitimate users can accidentally tell Claude to do destructive things

**To make it safer**, set in your channel:
```
/permission_mode auto       # Claude judges what to ask
/permission_mode acceptEdits # Edits auto, Bash asked
/permission_mode default     # Every tool asked (slowest, safest)
```

Or change the global default in `bot.py` (search for `DEFAULT_PERMISSION_MODE`).

---

## Features

- **Mention or DM** the bot to chat with Claude (per-channel session continuity)
- **Mac session takeover**: import in-progress sessions from your Mac via Syncthing-mirrored `~/.claude/projects/`
- **Mac → Discord mirror** (`discord-thread-bumper`): assistant text from Mac sessions appears live in Discord forum threads
- **6 permission modes**: `bypassPermissions` / `auto` / `acceptEdits` / `default` / `plan` / `dontAsk`
- **Permission Buttons**: in non-bypass modes, pops a Discord embed with ✅/🔁/❌ for any tool
- **Personas & templates**: `/persona 執事`, `/template 議事録`
- **Tool-aware**: `/files`, `/file`, `/audio` (Whisper local), `/image` (Gemini Nano Banana / Pollinations / Cloudflare)
- **`/rewind turns:N`**: git-based rollback of work directory
- **`/thread name: prompt:`**: spin off a Discord thread for one task
- **Scheduling**: `/schedule_add`, `/schedule_list`, daily report
- **Reactions**: 🔄 retry, 📋 save to Obsidian, 👍 continue, 🗑️ delete

---

## Architecture

```
        ┌─────────────────────────────┐
        │  Mac: Claude Code (CLI)     │
        │  ~/.claude/projects/*.jsonl │──┐
        └─────────────────────────────┘  │ Syncthing
                                         ▼
        ┌─────────────────────────────────────────┐
        │  VPS: ~/.claude/projects/*.jsonl        │
        │     │                                    │
        │     ▼ inotify                            │
        │  bumper: detect new msg → bump + mirror │
        │     │                                    │
        │     ▼ Discord API                        │
        │  Forum thread (one per CC session)      │
        └────────▲──────────────────────┬─────────┘
                 │                      │ ⚡ bumps
                 │ on_message           │ assistant text
                 │                      ▼
        ┌─────────────────────────────────────────┐
        │  bot.py: handle_message → claude CLI    │
        │     │ (subprocess / claude-agent-sdk)    │
        │     ▼                                    │
        │  Discord reply                          │
        └─────────────────────────────────────────┘
```

---

## Requirements

- Python 3.12+
- Linux VPS (tested on Ubuntu 22.04, Oracle Cloud Ampere A1)
- `claude` (Claude Code CLI) installed and authenticated
- Discord application + bot token
- *(For Mac sync)* Syncthing on Mac and VPS
- *(Optional)* `claude-agent-sdk` for Permission Button support
- *(Optional)* Gemini/Cloudflare API keys for image generation

---

## Quick Start

See **[SETUP.md](./SETUP.md)** for full step-by-step instructions including Discord application creation, Syncthing setup, and systemd service installation.

TL;DR:

```bash
git clone https://github.com/ainsophic-zero/discord-claude-code-bot.git
cd discord-claude-code-bot
python3 -m venv venv && source venv/bin/activate
pip install discord.py aiohttp claude-agent-sdk inotify-simple croniter

cp .env.example .env
# Edit .env: set ALLOWED_USER_IDS, DISCORD_TOKEN, WORK_DIR

python -u bot.py
```

For the bumper (Mac → Discord mirror):

```bash
python -u scripts/discord-thread-bumper.py
```

---

## Files

| Path | Purpose |
|------|---------|
| `bot.py` | Main Discord bot (4385 lines, slash commands, claude exec, permissions) |
| `scripts/discord-thread-bumper.py` | inotify daemon: Mac → Discord mirror, auto thread creation |
| `permission_handler.py` | PreToolUse hook for tool approval UI |
| `systemd/discord-claude-bot.service` | systemd unit for bot |
| `systemd/discord-thread-bumper.service` | systemd unit for bumper |
| `.env.example` | Configuration template |

---

## License

MIT
