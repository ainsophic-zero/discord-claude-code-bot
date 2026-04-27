#!/usr/bin/env python3
"""
Discord Thread Activity Bumper v3
- Bumps existing Discord threads when CC sessions are updated (Phase 3)
- Auto-creates new Discord threads when new CC sessions appear (Phase 2)
  * VPS filesystem でフォルダ名を逆引き（日本語対応）
  * Discordにカテゴリ/チャンネルがなければ自動作成
- Phase 2B: Discord側作成スレッドをCC session に自動リンク

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

# VPS上の.vscode/フォルダ → Macのベースパス対応表
VSCODE_PATH_MAP = [
    (Path("/home/ubuntu/dev/vscode-mcp/.vscode"),    "/Users/nk/dev/vscode-mcp/.vscode/"),
    (Path("/home/ubuntu/dev/vscode-mcp/workspaces"), "/Users/nk/dev/vscode-mcp/workspaces/"),
]

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


def resolve_folder_from_proj_dir(proj_dir_name: str):
    """
    VPS上の.vscode/フォルダを実際に列挙し、project dir名と照合して
    実際のフォルダ名とMacのwork_dirを返す。
    日本語フォルダ名も正しく復元できる。
    Returns (folder_name, mac_work_dir) or ('', '')
    """
    for vps_base, mac_base in VSCODE_PATH_MAP:
        if not vps_base.exists():
            continue
        try:
            for folder in vps_base.iterdir():
                if not folder.is_dir():
                    continue
                mac_path = mac_base + folder.name
                if encode_path(mac_path) == proj_dir_name:
                    return norm(folder.name), mac_path
        except Exception as e:
            log.warning(f"resolve_folder scan error: {e}")
    return '', ''


def read_title_from_jsonl(jsonl_path: str, max_lines=150) -> str:
    """Read first user message from a JSONL file as session title."""
    try:
        with open(jsonl_path, 'r', errors='replace') as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                try:
                    d = json.loads(line)
                    if d.get('type') == 'last-prompt':
                        lp = d.get('lastPrompt', '')
                        if lp and lp.strip():
                            return lp[:80].strip()
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
    con = sqlite3.connect(DB_PATH)
    try:
        r = con.execute(
            "SELECT channel_id FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def build_proj_dir_map():
    """Build {encoded_path: (channel_id, work_dir)} from sessions.db."""
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT channel_id, work_dir FROM sessions WHERE work_dir IS NOT NULL AND work_dir != ''"
        ).fetchall()
    finally:
        con.close()
    mapping = {}
    for channel_id, work_dir in rows:
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
    """Phase 2B: work_dirに対応する事前登録スレッドを探す (session_id=NULL のもの)。"""
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
    gn = norm(group_name)
    if gn in chan_by_cat and chan_by_cat[gn]:
        return list(chan_by_cat[gn].values())[0]
    for cat, chans in chan_by_cat.items():
        if cat.lower() == gn.lower() and chans:
            return list(chans.values())[0]
    return None


def get_or_create_channel(folder_name: str, cats: dict, chan_by_cat: dict):
    """
    指定フォルダ名のDiscordチャンネルを探し、なければカテゴリ+チャンネルを自動作成する。
    Returns channel_id or None.
    """
    # まず既存チャンネルを探す
    ch = find_channel_for_group(folder_name, cats, chan_by_cat)
    if ch:
        return ch

    fn = norm(folder_name)
    log.info(f"auto-creating Discord category+channel for: {fn}")

    # カテゴリを探す / なければ作成
    cat_id = None
    for cat_name, cid in cats.items():
        if cat_name.lower() == fn.lower():
            cat_id = cid
            break

    if not cat_id:
        r = requests.post(
            f"{API}/guilds/{GUILD_ID}/channels",
            headers=H,
            json={"name": fn, "type": 4},
            timeout=10
        )
        if r.status_code in (200, 201):
            cat_id = r.json()['id']
            cats[fn] = cat_id
            log.info(f"created category: {fn} → {cat_id}")
            time.sleep(0.5)
        else:
            log.warning(f"category create fail {r.status_code}: {r.text[:100]}")
            return None

    # チャンネル作成
    r = requests.post(
        f"{API}/guilds/{GUILD_ID}/channels",
        headers=H,
        json={"name": fn, "type": 0, "parent_id": cat_id},
        timeout=10
    )
    if r.status_code in (200, 201):
        ch_id = r.json()['id']
        chan_by_cat.setdefault(fn, {})[fn] = ch_id
        log.info(f"created channel: {fn} → {ch_id}")
        time.sleep(0.5)
        return ch_id
    else:
        log.warning(f"channel create fail {r.status_code}: {r.text[:100]}")
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
    """新しいCC sessionに対してDiscordスレッドを作成しDBに登録する。"""
    now = time.time()
    if session_id in last_new_attempt and now - last_new_attempt[session_id] < NEW_SESSION_DEBOUNCE:
        return
    last_new_attempt[session_id] = now

    # 1. VPSファイルシステムからフォルダ名を正確に復元（日本語対応）
    folder_name, work_dir = resolve_folder_from_proj_dir(proj_dir_name)

    if folder_name:
        log.info(f"resolved: proj_dir → folder='{folder_name}' work_dir={work_dir[-40:]}")
    else:
        # フォールバック: DBのwork_dirから探す
        proj_map = build_proj_dir_map()
        if proj_dir_name in proj_map:
            _, work_dir = proj_map[proj_dir_name]
            folder_name = folder_name_from_work_dir(work_dir)
            log.info(f"DB fallback: folder='{folder_name}' work_dir={work_dir[-40:]}")
        else:
            # 最終フォールバック: proj_dir名から推定（ASCIIのみ）
            parts = proj_dir_name.split('-vscode-')
            folder_raw = parts[-1] if len(parts) > 1 else proj_dir_name[-20:]
            folder_name = folder_raw.rstrip('-') or proj_dir_name[-20:]
            work_dir = ''
            log.info(f"heuristic fallback folder='{folder_name}'")

    # 2. Discordのチャンネルを取得（なければ自動作成）
    cats, chan_by_cat = get_discord_channels()
    channel_id = get_or_create_channel(folder_name, cats, chan_by_cat)

    if not channel_id:
        log.warning(f"could not find/create channel for '{folder_name}', session {session_id[:8]}")
        return

    # 3. タイトルをJSONLから読む（初回書き込み直後は空の場合あり → 少し待ってリトライ）
    title = read_title_from_jsonl(jsonl_path)
    if not title:
        time.sleep(5)
        title = read_title_from_jsonl(jsonl_path)
    if not title:
        title = folder_name or f"Session {session_id[:8]}"

    # 4. Phase 2B: 同じwork_dirに事前登録スレッドがあればそちらを使う
    if work_dir:
        preregistered = find_preregistered_thread(work_dir)
        if preregistered:
            db_register(preregistered, session_id, work_dir, title)
            log.info(f"Phase 2B linked: {session_id[:8]} → pre-registered {preregistered}")
            return preregistered

    # 5. 新規スレッド作成
    thread_id = create_discord_thread(channel_id, title)
    if not thread_id:
        return

    # 6. DB登録
    db_register(thread_id, session_id, work_dir, title)
    log.info(f"registered: {session_id[:8]} → thread {thread_id} in '{folder_name}'")
    return thread_id


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
    log.info(f"watching {len(wds)-1} project dirs (Phase 2 auto-create + folder name resolution enabled)")

    while True:
        for ev in inotify.read():
            # 新しいプロジェクトディレクトリが出現
            if ev.wd == parent_wd:
                p = PROJECTS_DIR / ev.name
                if p.is_dir() and p not in wds.values():
                    wd = inotify.add_watch(str(p), wf)
                    wds[wd] = p
                    log.info(f"new project dir watched: {ev.name}")
                continue

            if not ev.name.endswith(".jsonl"):
                continue

            sid = ev.name[:-6]
            now = time.time()

            # デバウンス
            if sid in last_bump and now - last_bump[sid] < DEBOUNCE_SEC:
                continue

            proj_dir = wds.get(ev.wd)
            jsonl_path = str(proj_dir / ev.name) if proj_dir else ''

            ch = lookup_channel(sid)
            if ch:
                # 既知セッション → bump
                last_bump[sid] = now
                bump(ch, sid)
            else:
                # 未知セッション → Phase 2: Discordスレッド自動作成
                proj_dir_name = proj_dir.name if proj_dir else ''
                if proj_dir_name and jsonl_path:
                    log.info(f"new session detected: {sid[:8]} in {proj_dir_name}")
                    result = handle_new_session(sid, jsonl_path, proj_dir_name)
                    # 作成後にbump
                    ch2 = lookup_channel(sid)
                    if ch2:
                        last_bump[sid] = now
                        bump(ch2, sid)


if __name__ == "__main__":
    main()
