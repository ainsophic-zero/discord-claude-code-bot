#!/usr/bin/env python3
"""
Discord Thread Activity Bumper v2
- Bumps existing Discord threads when CC sessions are updated (Phase 3)
- Auto-creates new Discord threads when new CC sessions appear (Phase 2)
- Registers new sessions in sessions.db

Loop prevention: posts as bot; on_message ignores bot messages.
Debounce: 30s per session for bumping, 10s for new-session creation.
"""
import os, sys, json, sqlite3, time, logging, unicodedata
from pathlib import Path
from inotify_simple import INotify, flags
import requests

DB_PATH = "/home/ubuntu/discord-bot/sessions.db"
PROJECTS_DIR = Path("/home/ubuntu/.claude/projects")
DEBOUNCE_SEC = 30
NEW_SESSION_DEBOUNCE = 10
GUILD_ID = '1495139872418042020'
API = "https://discord.com/api/v10"

TOKEN = None
with open("/home/ubuntu/discord-bot/.env") as f:
    for ln in f:
        if ln.startswith("DISCORD_TOKEN="):
            TOKEN = ln.strip().split("=", 1)[1].strip("'\"")
            break
if not TOKEN:
    sys.exit("DISCORD_TOKEN not found")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("bumper")

last_bump = {}          # session_id → last bump time
last_new_attempt = {}   # session_id → last create attempt time

H = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}


# ─────────────────────────── helpers ─────────────────────────────

def norm(s):
    return unicodedata.normalize('NFC', s).strip() if s else s


def encode_path(path: str) -> str:
    """Encode a filesystem path to Claude project dir name format.
    Only ASCII alphanumeric and '-' are kept; everything else → '-'."""
    result = []
    for c in path:
        if c.isascii() and (c.isalnum() or c == '-'):
            result.append(c)
        else:
            result.append('-')
    return ''.join(result)


def folder_name_from_work_dir(work_dir: str) -> str:
    """Extract the last path segment (project folder name)."""
    if not work_dir:
        return ''
    return Path(work_dir).name


def read_title_from_jsonl(jsonl_path: str, max_lines=100) -> str:
    """Read first user message from a JSONL file as session title."""
    try:
        with open(jsonl_path, 'r', errors='replace') as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                try:
                    d = json.loads(line)
                    # last-prompt entry
                    if d.get('type') == 'last-prompt':
                        lp = d.get('lastPrompt', '')
                        if lp:
                            return lp[:80].strip()
                    # user message
                    if d.get('type') == 'user':
                        msg = d.get('message', {})
                        content = msg.get('content', '')
                        if isinstance(content, str) and content.strip():
                            return content[:80].strip()
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get('type') == 'text':
                                    t = c.get('text', '').strip()
                                    if t:
                                        return t[:80]
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception as e:
        log.warning(f"read_title: {e}")
    return ''


# ─────────────────────────── DB ops ──────────────────────────────

def lookup_channel(session_id: str):
    """Return channel_id for a known session, or None."""
    con = sqlite3.connect(DB_PATH)
    try:
        r = con.execute(
            "SELECT channel_id FROM sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def build_proj_dir_map():
    """Build {encoded_path: channel_id} from sessions.db work_dirs."""
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT channel_id, work_dir FROM sessions WHERE work_dir != ''"
        ).fetchall()
    finally:
        con.close()
    mapping = {}
    for channel_id, work_dir in rows:
        if work_dir:
            encoded = encode_path(work_dir)
            if encoded not in mapping:
                mapping[encoded] = (channel_id, work_dir)
    return mapping


def db_register(channel_id: str, session_id: str, work_dir: str, title: str):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            '''INSERT OR REPLACE INTO sessions
               (channel_id, session_id, work_dir, thread_title)
               VALUES (?,?,?,?)''',
            (channel_id, session_id, work_dir, title)
        )
        con.commit()
    finally:
        con.close()


def find_preregistered_thread(work_dir: str):
    """Phase 2B: work_dirに対応する事前登録スレッドを探す (session_id=NULL のもの)。
    bot が Discord側スレッド作成を検知してDB登録した場合に利用。"""
    if not work_dir:
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        r = con.execute(
            "SELECT channel_id FROM sessions WHERE work_dir=? AND (session_id IS NULL OR session_id='')",
            (work_dir,)
        ).fetchone()
        return r[0] if r else None
    finally:
        con.close()


# ─────────────────────────── Discord ops ─────────────────────────

def bump(channel_id: str, session_id: str):
    try:
        r = requests.post(
            f"{API}/channels/{channel_id}/messages",
            headers=H,
            json={"content": "-# ⚡", "flags": 4096},
            timeout=10
        )
        if r.status_code in (200, 201):
            log.info(f"bump ok  ch={channel_id} sid={session_id[:8]}")
        else:
            log.warning(f"bump fail {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"bump exc: {e}")


def get_discord_channels():
    """Fetch all guild channels. Returns (cats, chan_by_cat)."""
    try:
        r = requests.get(f"{API}/guilds/{GUILD_ID}/channels", headers=H, timeout=10)
        chans = r.json()
        cats = {norm(c['name']): c['id'] for c in chans if c['type'] == 4}
        chan_by_cat = {}
        for c in chans:
            if c['type'] == 0 and c.get('parent_id'):
                cat = next((n for n, i in cats.items() if i == c['parent_id']), None)
                if cat:
                    chan_by_cat.setdefault(cat, {})[norm(c['name'])] = c['id']
        return cats, chan_by_cat
    except Exception as e:
        log.error(f"get_channels: {e}")
        return {}, {}


def find_channel_for_group(group_name: str, cats: dict, chan_by_cat: dict):
    """Return first channel_id for the given group/category, or None."""
    group_norm = norm(group_name)
    # exact match
    if group_norm in chan_by_cat and chan_by_cat[group_norm]:
        return list(chan_by_cat[group_norm].values())[0]
    # case-insensitive
    for cat, chans in chan_by_cat.items():
        if cat.lower() == group_norm.lower() and chans:
            return list(chans.values())[0]
    return None


def create_discord_thread(channel_id: str, title: str):
    """Create a public thread in the given channel. Return thread_id or None."""
    try:
        r = requests.post(
            f"{API}/channels/{channel_id}/threads",
            headers=H,
            json={"name": title[:100], "type": 11, "auto_archive_duration": 10080},
            timeout=10
        )
        if r.status_code in (200, 201):
            tid = r.json()['id']
            log.info(f"thread created: {tid} '{title[:40]}'")
            return tid
        else:
            log.warning(f"thread create fail {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"create_thread: {e}")
    return None


# ─────────────────────────── Phase 2 logic ───────────────────────

def handle_new_session(session_id: str, jsonl_path: str, proj_dir_name: str):
    """Try to create a Discord thread for a newly-seen CC session."""
    now = time.time()
    if session_id in last_new_attempt and now - last_new_attempt[session_id] < NEW_SESSION_DEBOUNCE:
        return
    last_new_attempt[session_id] = now

    # 1. Resolve work_dir from project dir mapping (DB)
    proj_map = build_proj_dir_map()
    work_dir = ''
    folder_name = ''

    if proj_dir_name in proj_map:
        _, work_dir = proj_map[proj_dir_name]
        folder_name = folder_name_from_work_dir(work_dir)
        log.info(f"new session matched proj_dir → wd={work_dir[-40:]} folder={folder_name}")
    else:
        # 2. Derive folder name from proj_dir_name suffix heuristically
        #    e.g. "-Users-nk-dev-vscode-mcp--vscode-Oracle----" → "Oracle----" → "Oracle"
        parts = proj_dir_name.split('-vscode-')
        folder_raw = parts[-1] if len(parts) > 1 else proj_dir_name.split('-')[-1]
        folder_name = folder_raw.rstrip('-') or proj_dir_name[-20:]
        log.info(f"new session fallback folder='{folder_name}' from proj_dir={proj_dir_name[-40:]}")

    # 3. Look up Discord TEXT channel for the folder/category
    #    Always fetch from guild to get actual text channel IDs (not thread IDs)
    cats, chan_by_cat = get_discord_channels()
    channel_id = find_channel_for_group(folder_name, cats, chan_by_cat)
    log.info(f"Discord channel for '{folder_name}': {channel_id}")

    if not channel_id and not work_dir:
        log.warning(f"no channel found for new session {session_id[:8]}, proj={proj_dir_name}")
        return

    # 3. Read title from JSONL
    title = read_title_from_jsonl(jsonl_path)
    if not title:
        # wait a bit and retry once (session may still be initializing)
        time.sleep(3)
        title = read_title_from_jsonl(jsonl_path)
    if not title:
        title = folder_name or f"Session {session_id[:8]}"

    # 4. Phase 2B: check for pre-registered thread (created from Discord side)
    if work_dir:
        preregistered = find_preregistered_thread(work_dir)
        if preregistered:
            db_register(preregistered, session_id, work_dir, title)
            log.info(f"Phase 2B linked: {session_id[:8]} → pre-registered {preregistered}")
            return preregistered

    if not channel_id:
        log.warning(f"no channel found for new session {session_id[:8]}, proj={proj_dir_name}")
        return

    # 5. Create Discord thread
    thread_id = create_discord_thread(channel_id, title)
    if not thread_id:
        return

    # 6. Register in DB
    db_register(thread_id, session_id, work_dir, title)
    log.info(f"registered new session {session_id[:8]} → thread {thread_id}")


# ─────────────────────────── main loop ───────────────────────────

def main():
    inotify = INotify()
    wf = flags.CLOSE_WRITE | flags.MOVED_TO
    pf = flags.CREATE | flags.MOVED_TO

    wds = {}
    for d in PROJECTS_DIR.iterdir():
        if d.is_dir():
            wd = inotify.add_watch(str(d), wf)
            wds[wd] = d
    parent_wd = inotify.add_watch(str(PROJECTS_DIR), pf)
    wds[parent_wd] = PROJECTS_DIR
    log.info(f"watching {len(wds)-1} project dirs (Phase 2 auto-create enabled)")

    while True:
        for ev in inotify.read():
            # New project directory appeared
            if ev.wd == parent_wd:
                p = PROJECTS_DIR / ev.name
                if p.is_dir() and p not in wds.values():
                    wd = inotify.add_watch(str(p), wf)
                    wds[wd] = p
                    log.info(f"new project dir: {ev.name}")
                continue

            if not ev.name.endswith(".jsonl"):
                continue

            sid = ev.name[:-6]
            now = time.time()

            # Debounce
            if sid in last_bump and now - last_bump[sid] < DEBOUNCE_SEC:
                continue

            proj_dir = wds.get(ev.wd)
            jsonl_path = str(proj_dir / ev.name) if proj_dir else ''

            ch = lookup_channel(sid)
            if ch:
                # Known session → bump
                last_bump[sid] = now
                bump(ch, sid)
            else:
                # Unknown session → Phase 2: auto-create thread
                proj_dir_name = proj_dir.name if proj_dir else ''
                if proj_dir_name and jsonl_path:
                    log.info(f"new session detected: {sid[:8]} in {proj_dir_name}")
                    handle_new_session(sid, jsonl_path, proj_dir_name)
                    # After creation, bump once
                    ch2 = lookup_channel(sid)
                    if ch2:
                        last_bump[sid] = now
                        bump(ch2, sid)


if __name__ == "__main__":
    main()
