#!/usr/bin/env python3
"""
Discord Thread Activity Bumper v7 (Forum Edition + Mac→Discord Mirror + Title Sync)
- v5機能継承
- v6: Mac側で書かれた会話 (cwd=/Users/...) をDiscordフォーラム投稿にミラー
  - VPS bot経由で書かれた応答 (cwd=/home/ubuntu/...) はスキップ（Discordに既出）
  - sessions.db.last_mirrored_uuid で増分管理
  - 起動時：未初期化セッションは現JSONL末尾を「処理済み」とする → 過去分は送られない
- v7: Mac→Discord タイトル同期（一方向）
  - .jsonl の metadata.title を監視
  - last_known_title と比較して変更時のみ Discord API で PATCH
  - タイトル長100文字制限で自動 truncate

Loop prevention: posts as bot; on_message ignores bot messages.
Debounce: 30s per session for bumping, 10s for new-session creation.
"""
import os, sys, json, sqlite3, time, logging, unicodedata, re
from pathlib import Path
from inotify_simple import INotify, flags
import requests

DB_PATH = "/home/ubuntu/discord-bot/sessions.db"
PROJECTS_DIR = Path("/home/ubuntu/.claude/projects")
DEBOUNCE_SEC = 30
NEW_SESSION_DEBOUNCE = 10
PERIODIC_SCAN_SEC = 300          # 5分ごとに全セッションスキャン
PERIODIC_RECENT_CUTOFF = 86400   # 24時間以内に更新されたJSONLだけ対象
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


def clean_title(t):
    """スレッドタイトルを1-100字に整形"""
    if not t:
        return 'Session'
    t = norm(t)
    t = re.sub(r'\s+', ' ', t)
    if t.startswith('<'):
        t = re.sub(r'<[^>]+>', '', t).strip() or t[:50]
    return t[:100].strip() or 'Session'


def encode_path(path: str) -> str:
    """Encode a filesystem path to Claude project dir name format."""
    result = []
    for c in path:
        if c.isascii() and (c.isalnum() or c == '-'):
            result.append(c)
        else:
            result.append('-')
    return ''.join(result)


def folder_name_from_work_dir(work_dir: str) -> str:
    if not work_dir:
        return ''
    return Path(work_dir).name


def resolve_folder_from_proj_dir(proj_dir_name: str):
    """VPSのファイルシステムからフォルダ名とMac work_dirを返す。"""
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


def is_valid_session_jsonl(name: str) -> bool:
    """Syncthing衝突ファイル等を弾く。正規のCC sessionファイルかどうか。"""
    if not name or not name.endswith('.jsonl'):
        return False
    stem = name[:-6]
    # Syncthing sync-conflict
    if '.sync-conflict-' in stem:
        return False
    # macOS dotfile / hidden
    if stem.startswith('.'):
        return False
    # tmp / 部分書き込み
    if stem.endswith('.tmp') or stem.endswith('.partial'):
        return False
    # CC session_id は UUID v4 形式 (8-4-4-4-12)。雑だが ハイフンの数で判定可
    if stem.count('-') != 4:
        return False
    return True


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
        # bumper管理のフォーラム投稿は auto_respond を自動ON（メンション無しでClaude応答）
        con.execute(
            '''INSERT INTO auto_respond(channel_id, enabled) VALUES(?, 1)
               ON CONFLICT(channel_id) DO UPDATE SET enabled=1, updated_at=CURRENT_TIMESTAMP''',
            (channel_id,)
        )
        con.commit()
    finally:
        con.close()


def find_preregistered_thread(work_dir: str):
    """Phase 2B: work_dirに対応する既存スレッドを探す（session_id有無問わず最新を返す）。
    重複スレッド作成を防ぐため、事前登録だけでなく既存スレッドも再利用する。"""
    if not work_dir:
        return None
    candidates = [work_dir]
    if work_dir.startswith('/Users/nk/'):
        candidates.append(work_dir.replace('/Users/nk/', '/home/ubuntu/', 1))
    elif work_dir.startswith('/home/ubuntu/'):
        candidates.append(work_dir.replace('/home/ubuntu/', '/Users/nk/', 1))
    con = sqlite3.connect(DB_PATH)
    try:
        for wd in candidates:
            # 事前登録（session_id未設定）を優先
            r = con.execute(
                "SELECT channel_id FROM sessions WHERE work_dir=? AND (session_id IS NULL OR session_id='') ORDER BY updated_at DESC LIMIT 1",
                (wd,)
            ).fetchone()
            if r:
                return r[0]
        # 見つからなければ既存スレッド（session_id設定済み）も含めて検索
        for wd in candidates:
            r = con.execute(
                "SELECT channel_id FROM sessions WHERE work_dir=? ORDER BY updated_at DESC LIMIT 1",
                (wd,)
            ).fetchone()
            if r:
                return r[0]
        return None
    finally:
        con.close()


# ─────────────────────────── Discord ops ─────────────────────────

def bump(channel_id: str, session_id: str):
    """フォーラム投稿（スレッド）にbumpメッセージを送信。"""
    try:
        r = requests.post(
            f"{API}/channels/{channel_id}/messages",
            headers=H,
            json={"content": "-# ⚡", "flags": 4096},
            timeout=10
        )
        if r.status_code in (200, 201):
            log.info(f"bump ok  ch={channel_id} sid={session_id[:8]}")
        elif r.status_code == 429:
            wait = r.json().get('retry_after', 5)
            time.sleep(wait + 0.5)
            requests.post(
                f"{API}/channels/{channel_id}/messages",
                headers=H,
                json={"content": "-# ⚡", "flags": 4096},
                timeout=10
            )
        else:
            log.warning(f"bump fail {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"bump exc: {e}")


def get_discord_structure():
    """
    Discordのカテゴリ・フォーラム構造を取得。
    Returns (cats, forums_by_cat)
    cats: {cat_name: cat_id}
    forums_by_cat: {cat_name: {forum_name: forum_id}}
    """
    try:
        r = requests.get(f"{API}/guilds/{GUILD_ID}/channels", headers=H, timeout=10)
        if r.status_code == 429:
            wait = r.json().get('retry_after', 5)
            time.sleep(wait + 1)
            r = requests.get(f"{API}/guilds/{GUILD_ID}/channels", headers=H, timeout=10)
        chans = r.json()
        if not isinstance(chans, list):
            log.error(f"get_structure unexpected: {str(chans)[:200]}")
            return {}, {}
        cats = {norm(c['name']): c['id'] for c in chans if c['type'] == 4}
        forums_by_cat = {}
        for c in chans:
            if c.get('type') == 15 and c.get('parent_id'):
                cat = next((n for n, i in cats.items() if i == c['parent_id']), None)
                if cat:
                    forums_by_cat.setdefault(cat, {})[norm(c['name'])] = c['id']
        return cats, forums_by_cat
    except Exception as e:
        log.error(f"get_structure: {e}")
        return {}, {}


def find_or_create_forum(folder_name: str, cats: dict, forums_by_cat: dict):
    """
    プロジェクトフォルダ名に対応するフォーラムチャンネルを取得。
    なければカテゴリ（とフォーラム）を自動作成。
    Returns forum_id or None.
    """
    fn = norm(folder_name)

    # 既存フォーラムを探す
    if fn in forums_by_cat and forums_by_cat[fn]:
        return list(forums_by_cat[fn].values())[0]
    for cat, forums in forums_by_cat.items():
        if cat.lower() == fn.lower() and forums:
            return list(forums.values())[0]

    log.info(f"auto-creating forum for: {fn}")

    # カテゴリを探す / なければ作成
    cat_id = cats.get(fn) or next(
        (cid for cn, cid in cats.items() if cn.lower() == fn.lower()), None
    )
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

    # フォーラムチャンネル作成
    r = requests.post(
        f"{API}/guilds/{GUILD_ID}/channels",
        headers=H,
        json={
            "name": fn,
            "type": 15,           # GUILD_FORUM
            "parent_id": cat_id,
            "default_auto_archive_duration": 10080,
        },
        timeout=10
    )
    if r.status_code in (200, 201):
        forum_id = r.json()['id']
        forums_by_cat.setdefault(fn, {})[fn] = forum_id
        log.info(f"created forum: {fn} → {forum_id}")
        time.sleep(0.5)
        return forum_id
    else:
        log.warning(f"forum create fail {r.status_code}: {r.text[:100]}")
        return None


def create_forum_post(forum_id: str, title: str):
    """
    フォーラムチャンネルに新規投稿（スレッド）を作成。
    フォーラム投稿は必ず初期messageが必要。
    Returns thread_id or None.
    """
    title_clean = clean_title(title)
    try:
        r = requests.post(
            f"{API}/channels/{forum_id}/threads",
            headers=H,
            json={
                "name": title_clean,
                "auto_archive_duration": 10080,
                "message": {"content": "-# ⚡"},
            },
            timeout=10
        )
        if r.status_code in (200, 201):
            tid = r.json()['id']
            log.info(f"forum post created: {tid} '{title_clean[:40]}'")
            return tid
        elif r.status_code == 429:
            wait = r.json().get('retry_after', 5)
            time.sleep(wait + 0.5)
            r = requests.post(
                f"{API}/channels/{forum_id}/threads",
                headers=H,
                json={
                    "name": title_clean,
                    "auto_archive_duration": 10080,
                    "message": {"content": "-# ⚡"},
                },
                timeout=10
            )
            if r.status_code in (200, 201):
                return r.json()['id']
        else:
            log.warning(f"forum post fail {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"create_forum_post: {e}")
    return None


# ─────────────────────────── Mirror (v6) ─────────────────────────

def split_for_discord(text: str, prefix: str = "") -> list:
    """Discord 2000字制限。1900で安全側に分割。最初のチャンクだけprefix付き。"""
    MAX = 1900
    if len(prefix) + len(text) <= MAX:
        return [prefix + text]
    chunks = []
    first_room = MAX - len(prefix)
    chunks.append(prefix + text[:first_room])
    rest = text[first_room:]
    while rest:
        chunks.append(rest[:MAX])
        rest = rest[MAX:]
    return chunks


def post_message(channel_id: str, content: str) -> bool:
    """Discordチャンネル/スレッドにメッセージ投稿。"""
    try:
        r = requests.post(
            f"{API}/channels/{channel_id}/messages",
            headers=H,
            json={"content": content},
            timeout=15
        )
        if r.status_code == 429:
            wait = r.json().get('retry_after', 5)
            time.sleep(wait + 0.5)
            r = requests.post(
                f"{API}/channels/{channel_id}/messages",
                headers=H,
                json={"content": content},
                timeout=15
            )
        if r.status_code in (200, 201):
            return True
        log.warning(f"post_message fail {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"post_message exc: {e}")
    return False



# ─── タイトル更新レート制限（30分に3回まで、Mac優先） ───
title_update_history: dict[str, list[float]] = {}

def can_update_title(channel_id: str, max_per_30min: int = 3) -> bool:
    now = time.time()
    hist = title_update_history.get(channel_id, [])
    hist = [ts for ts in hist if ts > now - 1800]
    title_update_history[channel_id] = hist
    return len(hist) < max_per_30min

def record_title_update(channel_id: str):
    title_update_history.setdefault(channel_id, []).append(time.time())


def update_thread_title(channel_id: str, new_title: str) -> bool:
    """Discordスレッド/フォーラム投稿のタイトル更新。100文字制限。"""
    title = new_title[:100] if len(new_title) > 100 else new_title
    try:
        r = requests.patch(
            f"{API}/channels/{channel_id}",
            headers=H,
            json={"name": title},
            timeout=15
        )
        if r.status_code == 429:
            wait = r.json().get('retry_after', 5)
            time.sleep(wait + 0.5)
            r = requests.patch(
                f"{API}/channels/{channel_id}",
                headers=H,
                json={"name": title},
                timeout=15
            )
        if r.status_code == 200:
            return True
        log.warning(f"update_thread_title fail {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"update_thread_title exc: {e}")
    return False


def _extract_text_from_content(content) -> str:
    """str か list[dict] の content から発話テキストだけ抽出。
    tool_use, tool_result, image, thinking は無視。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "".join(parts).strip()


def _is_system_noise(s: str) -> bool:
    """システムリマインダーやコマンド出力等のノイズを判定。"""
    if not s:
        return True
    if s.startswith("<system-reminder>"):
        return True
    if s.startswith("Caveat:"):
        return True
    if s.startswith("<command-name>"):
        return True
    if s.startswith("<local-command"):
        return True
    # 単一の<...>タグだけのもの
    if s.startswith("<") and s.endswith(">") and "\n" not in s and len(s) < 200:
        return True
    return False


def read_title_from_jsonl(jsonl_path: str) -> str | None:
    """JSONLから現在のタイトルを取得。優先: custom-title (最新) > metadata.title。
    custom-title はユーザーが Claude Code 上で付けたカスタム名。"""
    custom_title = None
    metadata_title = None
    try:
        with open(jsonl_path, 'r', errors='replace') as f:
            for line in f:
                try:
                    d = json.loads(line)
                    t = d.get("type")
                    if t == "custom-title":
                        ct = d.get("customTitle")
                        if ct and ct.strip():
                            custom_title = ct.strip()  # 最後の出現が最新
                    elif t == "metadata":
                        mt = d.get("title")
                        if mt and mt.strip():
                            metadata_title = mt.strip()
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return custom_title or metadata_title


def extract_messages_after(jsonl_path: str, last_uuid):
    """Mac起源(/Users/...)のミラー対象メッセージを last_uuid 以降で抽出。
    Returns: list of (role, text, uuid)
    """
    messages = []
    started = (last_uuid is None)
    try:
        with open(jsonl_path, 'r', errors='replace') as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                uuid = d.get("uuid")
                if not started:
                    if uuid == last_uuid:
                        started = True
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                if d.get("isSidechain"):
                    continue
                cwd = d.get("cwd", "")
                # Mac起源のみミラー（VPS botの応答はDiscordに既出）
                if not cwd.startswith("/Users/"):
                    continue
                content = d.get("message", {}).get("content")
                text = _extract_text_from_content(content)
                if _is_system_noise(text):
                    continue
                messages.append((t, text, uuid))
    except Exception as e:
        log.warning(f"extract_messages: {e}")
    return messages


def mirror_to_discord(channel_id: str, session_id: str, jsonl_path: str):
    """Mac起源の新規メッセージをDiscordフォーラム投稿にミラー。
    v7: タイトル同期（Mac→Discord一方向）追加。"""

    # タイトル同期チェック
    current_title = read_title_from_jsonl(jsonl_path)
    if current_title:
        con = sqlite3.connect(DB_PATH)
        try:
            r = con.execute(
                "SELECT last_known_title FROM sessions WHERE channel_id=?",
                (channel_id,)
            ).fetchone()
            last_title = r[0] if r and r[0] else None
        finally:
            con.close()

        if current_title != last_title:
            if not can_update_title(channel_id):
                log.warning(f"title rate-limit ch={channel_id[:18]} (>3/30min) skip")
                con = sqlite3.connect(DB_PATH)
                try:
                    con.execute(
                        "UPDATE sessions SET last_known_title=? WHERE channel_id=?",
                        (current_title, channel_id)
                    )
                    con.commit()
                finally:
                    con.close()
            elif update_thread_title(channel_id, current_title):
                record_title_update(channel_id)
                con = sqlite3.connect(DB_PATH)
                try:
                    con.execute(
                        "UPDATE sessions SET last_known_title=? WHERE channel_id=?",
                        (current_title, channel_id)
                    )
                    con.commit()
                    log.info(f"title synced: ch={channel_id} '{current_title[:50]}'")
                finally:
                    con.close()

    # メッセージミラー
    con = sqlite3.connect(DB_PATH)
    try:
        r = con.execute(
            "SELECT last_mirrored_uuid FROM sessions WHERE channel_id=?",
            (channel_id,)
        ).fetchone()
        last_uuid = r[0] if r and r[0] else None
    finally:
        con.close()

    new_msgs = extract_messages_after(jsonl_path, last_uuid)
    if not new_msgs:
        return

    last_processed = None
    sent = 0
    for role, text, uuid in new_msgs:
        prefix = "👤 " if role == "user" else "🤖 "
        chunks = split_for_discord(text, prefix=prefix)
        all_ok = True
        for chunk in chunks:
            if not post_message(channel_id, chunk):
                all_ok = False
                break
            time.sleep(0.5)
        if all_ok:
            last_processed = uuid
            sent += 1
        else:
            break

    if last_processed:
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute(
                "UPDATE sessions SET last_mirrored_uuid=? WHERE channel_id=?",
                (last_processed, channel_id)
            )
            con.commit()
        finally:
            con.close()
        log.info(f"mirrored {sent} msgs to ch={channel_id} sid={session_id[:8]}")


def find_jsonl_for_session(session_id: str):
    """同名JSONLが複数dir（.bak含む）にある場合は最も新しいものを返す。"""
    candidates = []
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        # .bak など派生dirは除外
        if proj_dir.name.endswith(".bak") or "_bak_" in proj_dir.name:
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.exists():
            try:
                candidates.append((candidate.stat().st_mtime, str(candidate)))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def read_last_uuid(jsonl_path: str):
    last = None
    try:
        with open(jsonl_path, 'r', errors='replace') as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("uuid"):
                        last = d["uuid"]
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return last


def init_mirror_state():
    """起動時：未初期化セッションは現在のJSONL末尾を処理済みとマーク。
    過去ログを再送信しないため。"""
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT channel_id, session_id FROM sessions "
            "WHERE session_id IS NOT NULL AND session_id != '' "
            "AND (last_mirrored_uuid IS NULL OR last_mirrored_uuid = '')"
        ).fetchall()
    finally:
        con.close()

    initialized = 0
    for ch_id, sid in rows:
        jsonl_path = find_jsonl_for_session(sid)
        if not jsonl_path:
            continue
        last_uuid = read_last_uuid(jsonl_path)
        if last_uuid:
            con = sqlite3.connect(DB_PATH)
            try:
                con.execute(
                    "UPDATE sessions SET last_mirrored_uuid=? WHERE channel_id=?",
                    (last_uuid, ch_id)
                )
                con.commit()
            finally:
                con.close()
            initialized += 1
    log.info(f"init_mirror_state: initialized {initialized} sessions to current tail (past logs will not be sent)")


# ─────────────────────────── Phase 2 logic ───────────────────────

def handle_new_session(session_id: str, jsonl_path: str, proj_dir_name: str):
    """新しいCC sessionに対してDiscordフォーラム投稿を作成しDBに登録。"""
    now = time.time()
    if session_id in last_new_attempt and now - last_new_attempt[session_id] < NEW_SESSION_DEBOUNCE:
        return None
    last_new_attempt[session_id] = now

    # 重複防止: 既にこのsession_idがDBに登録済みなら新規フォーラム作成しない
    # （bot.pyの/clearでレコード削除→bumperが新規誤判定して連投する競合を防ぐ）
    existing_ch = lookup_channel(session_id)
    if existing_ch:
        log.info(f'already registered: {session_id[:8]} -> {existing_ch}, skip new session creation')
        return existing_ch

    # 1. VPSファイルシステムからフォルダ名を復元
    folder_name, work_dir = resolve_folder_from_proj_dir(proj_dir_name)

    if folder_name:
        log.info(f"resolved: '{folder_name}' work_dir={work_dir[-40:]}")
    else:
        proj_map = build_proj_dir_map()
        if proj_dir_name in proj_map:
            _, work_dir = proj_map[proj_dir_name]
            folder_name = folder_name_from_work_dir(work_dir)
            log.info(f"DB fallback: folder='{folder_name}'")
        else:
            parts = proj_dir_name.split('-vscode-')
            folder_raw = parts[-1] if len(parts) > 1 else proj_dir_name[-20:]
            folder_name = folder_raw.rstrip('-') or proj_dir_name[-20:]
            work_dir = ''
            log.info(f"heuristic fallback: '{folder_name}'")

    # 2. フォーラムチャンネルを取得（なければ作成）
    cats, forums_by_cat = get_discord_structure()
    forum_id = find_or_create_forum(folder_name, cats, forums_by_cat)

    if not forum_id:
        log.warning(f"could not find/create forum for '{folder_name}', sid={session_id[:8]}")
        return None

    # 3. タイトルを読む
    title = read_title_from_jsonl(jsonl_path)
    if not title:
        time.sleep(5)
        title = read_title_from_jsonl(jsonl_path)
    if not title:
        title = folder_name or f"Session {session_id[:8]}"

    # 4. Phase 2B: 同じwork_dirの事前登録投稿があればリンク
    if work_dir:
        preregistered = find_preregistered_thread(work_dir)
        if preregistered:
            db_register(preregistered, session_id, work_dir, title)
            log.info(f"Phase 2B linked: {session_id[:8]} → {preregistered}")
            return preregistered

    # 5. 新規フォーラム投稿作成
    post_id = create_forum_post(forum_id, title)
    if not post_id:
        return None

    # 6. DB登録
    db_register(post_id, session_id, work_dir, title)
    log.info(f"registered: {session_id[:8]} → post {post_id} in forum '{folder_name}'")
    return post_id


# ─────────────────────────── v4継承: スキャン機能 ────────────────────────────

def startup_scan():
    """起動時スキャン: 未登録セッションのフォーラム投稿を自動作成。"""
    log.info("startup_scan: scanning all existing sessions...")
    created = 0; already = 0; skipped = 0

    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        folder_name, work_dir = resolve_folder_from_proj_dir(proj_dir.name)
        if not folder_name:
            skipped += 1
            continue
        jsonl_files = [f for f in sorted(proj_dir.glob("*.jsonl"),
                             key=lambda f: f.stat().st_mtime if f.exists() else 0,
                             reverse=True) if is_valid_session_jsonl(f.name)][:3]
        for jsonl_file in jsonl_files:
            sid = jsonl_file.stem
            ch = lookup_channel(sid)
            if ch:
                already += 1
            else:
                result = handle_new_session(sid, str(jsonl_file), proj_dir.name)
                if result:
                    created += 1
                    time.sleep(1.0)
                else:
                    skipped += 1

    log.info(f"startup_scan done: created={created} already={already} skipped={skipped}")


def periodic_scan():
    """5分ごとスキャン: 24時間以内に更新されたJSONLで未登録セッションを検出。"""
    cutoff = time.time() - PERIODIC_RECENT_CUTOFF
    new_found = 0

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        folder_name, _ = resolve_folder_from_proj_dir(proj_dir.name)
        if not folder_name:
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if not is_valid_session_jsonl(jsonl_file.name):
                continue
            try:
                if jsonl_file.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            sid = jsonl_file.stem
            if not lookup_channel(sid):
                result = handle_new_session(sid, str(jsonl_file), proj_dir.name)
                if result:
                    new_found += 1
                    time.sleep(1.0)

    if new_found:
        log.info(f"periodic_scan: {new_found} new sessions registered")


# ─────────────────────────── DB migration ───────────────────────────

def ensure_schema():
    """v7: last_known_title カラム追加（Mac→Discord タイトル同期用）"""
    con = sqlite3.connect(DB_PATH)
    try:
        # カラム存在チェック
        cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)").fetchall()]
        if "last_known_title" not in cols:
            con.execute("ALTER TABLE sessions ADD COLUMN last_known_title TEXT")
            con.commit()
            log.info("schema migration: added last_known_title column")
    finally:
        con.close()


# ─────────────────────────── main loop ───────────────────────────

def main():
    # v7: スキーママイグレーション
    try:
        ensure_schema()
    except Exception as e:
        log.error(f"ensure_schema error: {e}")

    inotify = INotify()
    # v6: MODIFY追加（appendを拾う）+ MOVE_SELF/DELETE_SELF（Syncthing rename対応）
    wf = (flags.MODIFY | flags.CLOSE_WRITE | flags.MOVED_TO |
          flags.CREATE | flags.MOVE_SELF | flags.DELETE_SELF)
    pf = flags.CREATE | flags.MOVED_TO

    wds = {}

    def add_dir_watch(d: Path):
        try:
            wd = inotify.add_watch(str(d), wf)
            wds[wd] = d
            return wd
        except OSError as e:
            log.warning(f"add_watch failed {d}: {e}")
            return None

    for d in PROJECTS_DIR.iterdir():
        if d.is_dir():
            add_dir_watch(d)
    parent_wd = inotify.add_watch(str(PROJECTS_DIR), pf)
    wds[parent_wd] = PROJECTS_DIR
    log.info(f"watching {len(wds)-1} project dirs (v7: Title Sync Edition)")

    try:
        startup_scan()
    except Exception as e:
        log.error(f"startup_scan error: {e}")

    # v6: ミラー初期化（既存セッションは現状のJSONL末尾を「処理済み」にする）
    try:
        init_mirror_state()
    except Exception as e:
        log.error(f"init_mirror_state error: {e}")

    last_periodic = time.time()

    while True:
        events = inotify.read(timeout=60000)

        now = time.time()
        if now - last_periodic >= PERIODIC_SCAN_SEC:
            last_periodic = now
            try:
                periodic_scan()
            except Exception as e:
                log.error(f"periodic_scan error: {e}")

        for ev in events:
            # 自己修復: watch が死んだ（rename/delete等）→ 親dirを再watch
            if ev.mask & (flags.IGNORED | flags.MOVE_SELF | flags.DELETE_SELF):
                d = wds.pop(ev.wd, None)
                if d and d != PROJECTS_DIR and d.exists():
                    new_wd = add_dir_watch(d)
                    if new_wd:
                        log.info(f"re-watched after rename/ignored: {d.name}")
                continue

            if ev.wd == parent_wd:
                p = PROJECTS_DIR / ev.name
                if p.is_dir() and p not in wds.values():
                    add_dir_watch(p)
                    log.info(f"new project dir watched: {ev.name}")
                continue

            if not is_valid_session_jsonl(ev.name):
                continue

            sid = ev.name[:-6]
            now2 = time.time()

            if sid in last_bump and now2 - last_bump[sid] < DEBOUNCE_SEC:
                continue

            proj_dir = wds.get(ev.wd)
            jsonl_path = str(proj_dir / ev.name) if proj_dir else ''

            ch = lookup_channel(sid)
            if ch:
                last_bump[sid] = now2
                bump(ch, sid)
                # v6: Mac→Discord ミラー
                if jsonl_path:
                    try:
                        mirror_to_discord(ch, sid, jsonl_path)
                    except Exception as e:
                        log.error(f"mirror error sid={sid[:8]}: {e}")
            else:
                proj_dir_name = proj_dir.name if proj_dir else ''
                if proj_dir_name and jsonl_path:
                    log.info(f"new session: {sid[:8]} in {proj_dir_name}")
                    handle_new_session(sid, jsonl_path, proj_dir_name)
                    ch2 = lookup_channel(sid)
                    if ch2:
                        last_bump[sid] = now2
                        bump(ch2, sid)
                        # 新規セッションは即座に末尾uuidを記録（過去メッセージはミラーしない）
                        try:
                            last_uuid = read_last_uuid(jsonl_path)
                            if last_uuid:
                                con = sqlite3.connect(DB_PATH)
                                try:
                                    con.execute(
                                        "UPDATE sessions SET last_mirrored_uuid=? WHERE channel_id=?",
                                        (last_uuid, ch2)
                                    )
                                    con.commit()
                                finally:
                                    con.close()
                        except Exception as e:
                            log.error(f"new session mirror init error: {e}")


if __name__ == "__main__":
    main()
