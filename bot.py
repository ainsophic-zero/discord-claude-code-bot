#!/usr/bin/env python3
"""
Discord Bot for Claude Code
- @mention or DM でClaudeと会話
- スレッドごとにセッション継続
- ファイル添付対応
- 長文自動分割
- /model コマンド＋自然言語でモデル切り替え対応
"""

import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from permission_handler import DiscordPermissionUI, make_pretool_hook
try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

# permission_mode != bypassPermissions のときに使う、
# channel ごとの「常に許可」した tool name セット
_SESSION_ALLOWED_TOOLS: dict[str, set[str]] = {}

# ── 設定 ────────────────────────────────────────────
TOKEN          = os.environ["DISCORD_TOKEN"]
API_KEY        = os.environ.get("ANTHROPIC_API_KEY", "")
WORK_DIR       = Path(os.environ.get("WORK_DIR", str(Path.home() / "workspace")))
DB_PATH        = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "sessions.db")))
CLAUDE_BIN     = os.environ.get("CLAUDE_BIN", "claude")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))

# モデル指定（CLIが自動で最新バージョンに解決してくれる）
# --model sonnet → claude-sonnet-4-6, --model opus → claude-opus-4-7, etc.
MODEL_ALIASES = {
    "opus":    "opus",
    "sonnet":  "sonnet",
    "haiku":   "haiku",
    # カタカナ・ひらがなも同じショートネームに正規化
    "オーパス": "opus", "オパス": "opus", "おーぱす": "opus", "おぱす": "opus",
    "ソネット": "sonnet", "そねっと": "sonnet",
    "ハイク": "haiku", "はいく": "haiku",
}
DEFAULT_MODEL = "sonnet"  # CLIがその時点の最新sonnetを使う
MODEL_EMOJI = {
    "opus":   "🟣",
    "sonnet": "🔵",
    "haiku":  "🟢",
}

# モデル名（ラテン文字・カタカナ・ひらがな）マッチ用
_MODEL_KEYWORD = (
    r'(opus|sonnet|haiku'
    r'|オーパス|オパス|おーぱす|おぱす'       # opus カタカナ/ひらがな
    r'|ソネット|そねっと'                       # sonnet カタカナ/ひらがな
    r'|ハイク|はいく'                           # haiku カタカナ/ひらがな
    r')'
)

# 切り替えアクション動詞（漢字・ひらがな混在を吸収）
_SWITCH_VERB = (
    r'(?:に|へ)?'
    r'(?:切り替え|きりかえ|切りかえ|変え|かえ|して|にして|変更|変更して|してください|にしてください|切り替えてください|きりかえてください)'
)

# 自然言語モデル切り替えパターン（日本語・英語）
_NL_PERMISSION_PATTERN = re.compile(
    r'(?:'
    # 「権限モードをautoに」「モードをデンジャラスに」「plan モードに切り替え」等
    + r'(?:権限モード|モード|permission[ _-]?mode)\s*を?\s*'
    + r'(auto|オート|bypass|デンジャラス|dangerous|accept|編集OK|default|デフォルト|毎回|plan|計画|読み取り|dontAsk|don[\' ]?t\s*ask|全拒否)'
    + r'(?:\s*に)?(?:\s*(?:切り替え|きりかえ|変え|かえ|して|変更))?'
    + r'|'
    # 「autoに切り替え」のような逆パターン (モード言及なし)
    + r'(auto|オート|デンジャラス|dangerous|plan|計画)\s*モード\s*(?:に)?(?:切り替え|きりかえ|変え|かえ|して)'
    + r')',
    re.IGNORECASE
)

_PERMISSION_NL_ALIASES = {
    "auto": "auto", "オート": "auto", "おーと": "auto",
    "bypass": "bypassPermissions", "デンジャラス": "bypassPermissions",
    "dangerous": "bypassPermissions",
    "accept": "acceptEdits", "編集ok": "acceptEdits",
    "default": "default", "デフォルト": "default", "毎回": "default",
    "plan": "plan", "計画": "plan", "読み取り": "plan",
    "dontask": "dontAsk", "don't ask": "dontAsk", "全拒否": "dontAsk",
}

_NL_MODEL_PATTERN = re.compile(
    r'(?:'
    # 日本語: 「opusに切り替えて」「オーパスにきりかえて」「sonnetにして」等
    + r'(?:モデルを?)?' + _MODEL_KEYWORD + r'(?:モデル|に)?' + _SWITCH_VERB
    + r'|'
    # 日本語逆順: 「モデルをopusに変えて」
    + r'(?:モデル|もでる)を?' + _MODEL_KEYWORD + r'に'
    + r'|'
    # 英語: 「switch to opus」「use opus」「change model to sonnet」
    + r'(?:switch|change|use)\s+(?:to\s+)?(?:the\s+)?(?:model\s+)?' + _MODEL_KEYWORD + r'(?:\s+model)?'
    + r')',
    re.IGNORECASE
)

# モデル切り替えヒントキーワード
_MODEL_HINT_RE = re.compile(
    r'(クロード|claude|モデル|もでる|切り替|きりかえ|変え|かえ|にして|重い|軽い|賢い|速い|安い|遅い'
    r'|高性能|シンプル|コスト|賢いの|賢いやつ|重いの|軽いの|速いの|遅いの'
    r'|opus|sonnet|haiku|オーパス|ソネット|ハイク)',
    re.IGNORECASE
)
# プロジェクト切り替えヒントキーワード
_PROJECT_HINT_RE = re.compile(
    r'(に移|にうつ|フォルダ|ぷろじぇくと|プロジェクト|で作業|に切り替|にきりか|開いて|ひらいて'
    r'|\d+番|番目|のやつ|のとこ|のところ)',
    re.IGNORECASE
)
GEMINI_KEY       = os.environ.get("GEMINI_API_KEY", "")  # 既存キー（テキスト・画像とも対応）
GEMINI_KEY_TEXT  = GEMINI_KEY
GEMINI_KEY_IMAGE = GEMINI_KEY

WORK_DIR.mkdir(parents=True, exist_ok=True)

# ── キャラクター（ペルソナ） ──────────────────────────
PERSONAS: dict[str, dict] = {
    "default": {
        "emoji": "🤖",
        "label": "デフォルト",
        "prompt": "",
    },
    "butler": {
        "emoji": "🎩",
        "label": "執事",
        "prompt": (
            "あなたは丁寧で品格のある執事として応答してください。"
            "「〜でございます」「〜いたします」などの丁寧な口調を使い、"
            "主人への最大限の敬意を払ってください。ただし技術的内容の正確性は損なわないこと。"
        ),
    },
    "gal": {
        "emoji": "💅",
        "label": "ギャル",
        "prompt": (
            "あなたはノリの良いギャル口調で応答してください。"
            "「まじ」「〜じゃん」「〜だわ」「てか」などの軽快な言葉を使い、"
            "絵文字も多めに。ただし技術的内容の正確性は損なわないこと。"
        ),
    },
    "linus": {
        "emoji": "😤",
        "label": "容赦なし",
        "prompt": (
            "あなたは Linus Torvalds 風の容赦ないレビュアーです。"
            "指摘は率直で辛辣に、甘やかしは一切しません。"
            "ただし技術的に正確・建設的であること。単なる罵倒ではなく、何がダメで何を直すべきかを明確に。"
        ),
    },
    "kansai": {
        "emoji": "🐯",
        "label": "関西弁",
        "prompt": (
            "あなたは関西弁で応答してください。"
            "「〜やで」「〜やな」「ほんま」「せやな」などを使ってください。"
            "ツッコミも適度に入れて、親しみやすく。ただし技術的内容の正確性は損なわないこと。"
        ),
    },
    "zen": {
        "emoji": "🧘",
        "label": "禅僧",
        "prompt": (
            "あなたは禅僧のように静かで深い応答をしてください。"
            "余計な言葉は削ぎ落とし、本質を簡潔に伝えてください。"
            "時に問いを返すことで思考を促してください。"
        ),
    },
}

# ── Permission Mode 定義 ──────────────────
PERMISSION_MODES = {
    "bypassPermissions": {
        "emoji": "⚠️",
        "label": "デンジャラス（全許可）",
        "desc": "全ツール承認なしで実行。一番速いが誤操作リスクあり。",
        "cli_args": ["--dangerously-skip-permissions"],
    },
    "auto": {
        "emoji": "🤖",
        "label": "オート（Claude判断で承認）",
        "desc": "Claude が自動で『危険』と判断したツールだけ確認。2026年の新機能。",
        "cli_args": ["--permission-mode", "auto"],
    },
    "acceptEdits": {
        "emoji": "✏️",
        "label": "編集OK・コマンド聞く",
        "desc": "ファイル編集は自動承認、Bash等は都度確認。",
        "cli_args": ["--permission-mode", "acceptEdits"],
    },
    "default": {
        "emoji": "🔐",
        "label": "毎回確認",
        "desc": "全てのツール使用を承認ボタンで確認。安全だが遅い。",
        "cli_args": ["--permission-mode", "default"],
    },
    "plan": {
        "emoji": "🧐",
        "label": "計画のみ（読み取り専用）",
        "desc": "ファイル変更一切なし。分析・計画用。",
        "cli_args": ["--permission-mode", "plan"],
    },
    "dontAsk": {
        "emoji": "🙅",
        "label": "未承認は全拒否",
        "desc": "事前承認済みツールのみ使用、他は全拒否。",
        "cli_args": ["--permission-mode", "dontAsk"],
    },
}
DEFAULT_PERMISSION_MODE = "bypassPermissions"

def get_permission_mode(channel_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(permission_mode, ?) FROM sessions WHERE channel_id=?",
        (DEFAULT_PERMISSION_MODE, channel_id),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else DEFAULT_PERMISSION_MODE

def set_permission_mode(channel_id: str, mode: str):
    if mode not in PERMISSION_MODES:
        raise ValueError(f"invalid mode: {mode}")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT work_dir, model, COALESCE(persona,'default') FROM sessions WHERE channel_id=?",
        (channel_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE sessions SET permission_mode=?, updated_at=CURRENT_TIMESTAMP WHERE channel_id=?",
            (mode, channel_id)
        )
    else:
        conn.execute(
            "INSERT INTO sessions(channel_id, session_id, work_dir, model, persona, permission_mode) "
            "VALUES(?,NULL,?,?,?,?)",
            (channel_id, str(WORK_DIR), DEFAULT_MODEL, "default", mode)
        )
    conn.commit()
    conn.close()

PERSONA_ALIASES = {
    # 日本語名からキーへ
    "デフォルト": "default", "ふつう": "default", "普通": "default", "ノーマル": "default",
    "執事": "butler", "しつじ": "butler", "butler": "butler",
    "ギャル": "gal", "ぎゃる": "gal", "gal": "gal",
    "容赦なし": "linus", "linus": "linus", "リーナス": "linus", "辛口": "linus",
    "関西弁": "kansai", "かんさい": "kansai", "関西": "kansai", "大阪弁": "kansai",
    "禅": "zen", "ぜん": "zen", "禅僧": "zen", "zen": "zen",
}

# ── テンプレート（特定タスク用プロンプトプリセット） ────
TEMPLATES: dict[str, dict] = {
    "minutes": {
        "emoji": "📋",
        "label": "議事録",
        "prompt": (
            "あなたは議事録作成の専門家です。送られた音声文字起こしや会話メモを基に、"
            "以下の構造で議事録をMarkdownで作成してください:\n"
            "- # 会議タイトル（推測）\n- ## 日時 / 参加者（わかれば）\n- ## アジェンダ\n"
            "- ## 議論内容（要点のみ・箇条書き）\n- ## 決定事項\n- ## ToDo（担当者・期日）\n"
            "- ## 次回までに\n冗長な発言は要約。重要な数字・日付・固有名詞は必ず残す。"
        ),
    },
    "blog": {
        "emoji": "📝",
        "label": "ブログ草稿",
        "prompt": (
            "あなたは読みやすいブログ記事を書くプロです。送られたトピックや雑なメモから、"
            "以下の構成でMarkdown記事を生成してください:\n"
            "- 引きのあるタイトル（h1）\n- 導入（読者に「自分のことだ」と思わせる）\n"
            "- 本文（h2でセクション分け、具体例多め）\n- まとめ\n"
            "敬体・親しみやすい文体。SEOキーワードも自然に含める。"
        ),
    },
    "translate": {
        "emoji": "🌐",
        "label": "翻訳（日↔英）",
        "prompt": (
            "あなたは熟練した翻訳者です。日本語が来たら自然な英語に、英語が来たら自然な日本語に翻訳してください。"
            "直訳ではなく、ネイティブが書くような表現で。"
            "翻訳結果のあと、ニュアンスや使い分けの注意点があれば短く添える。"
        ),
    },
    "todo": {
        "emoji": "✅",
        "label": "ToDo整理",
        "prompt": (
            "あなたはタスク管理のプロです。送られた雑な「やることメモ」を、以下の形式で整理してください:\n"
            "## 今日やる\n- [ ] タスク1（推定時間: 30分）\n## 今週中\n- [ ] タスク2\n## いつか\n- [ ] タスク3\n\n"
            "曖昧な表現は具体化、依存関係があれば明記、優先度判定基準を最後に短く説明。"
        ),
    },
    "summarize": {
        "emoji": "📄",
        "label": "要約",
        "prompt": (
            "あなたは要約の達人です。送られた長文・記事・音声文字起こし等を、以下の構成で要約:\n"
            "- 🎯 一言で（30字以内）\n- 📌 要点（3〜5点・箇条書き）\n- 🔑 キーワード\n- 💡 自分のメモ用に残すべき重要箇所"
        ),
    },
    "code_review": {
        "emoji": "🔍",
        "label": "コードレビュー",
        "prompt": (
            "あなたは厳格で優秀なシニアエンジニアです。送られたコードを以下の観点でレビュー:\n"
            "1. バグ・潜在的問題\n2. 設計・可読性\n3. パフォーマンス\n4. セキュリティ\n5. 命名・ドキュメント\n"
            "各指摘に重要度（CRITICAL/HIGH/MEDIUM/LOW）を付け、修正コード例も提示。"
        ),
    },
    "english_lesson": {
        "emoji": "🎓",
        "label": "英語レッスン",
        "prompt": (
            "あなたは英語講師です。送られた英文・質問に対して:\n"
            "1. 自然な英語の使い方を解説\n2. 文法的に間違いがあれば指摘\n3. 別の言い回しや慣用句も提示\n"
            "4. 発音注意点があれば追記\n日本語で丁寧に説明し、例文を必ず2〜3個入れる。"
        ),
    },
}

TEMPLATE_ALIASES = {
    "議事録": "minutes", "minutes": "minutes", "minute": "minutes", "ミーティング": "minutes",
    "ブログ": "blog", "blog": "blog", "記事": "blog",
    "翻訳": "translate", "translate": "translate", "英訳": "translate", "和訳": "translate",
    "todo": "todo", "ToDo": "todo", "やること": "todo", "タスク": "todo",
    "要約": "summarize", "summarize": "summarize", "summary": "summarize", "まとめ": "summarize",
    "レビュー": "code_review", "review": "code_review", "コードレビュー": "code_review", "code-review": "code_review",
    "英語": "english_lesson", "english": "english_lesson", "英会話": "english_lesson",
}

# ── DB ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auto_respond (
            channel_id TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            channel_id  TEXT PRIMARY KEY,
            session_id  TEXT,
            work_dir    TEXT,
            model       TEXT DEFAULT 'claude-sonnet-4-5',
            persona     TEXT DEFAULT 'default',
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # ブランチ（名前付きセッション保存）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            channel_id  TEXT,
            label       TEXT,
            session_id  TEXT,
            work_dir    TEXT,
            model       TEXT,
            persona     TEXT DEFAULT 'default',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(channel_id, label)
        )
    """)
    # スケジュールタスク
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id  TEXT,
            cron_expr   TEXT,
            label       TEXT,
            prompt      TEXT,
            work_dir    TEXT,
            model       TEXT,
            persona     TEXT DEFAULT 'default',
            template    TEXT DEFAULT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_run    DATETIME,
            enabled     INTEGER DEFAULT 1
        )
    """)
    # 会話インタラクションログ（デイリーレポート用）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id   TEXT,
            timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_msg     TEXT,
            bot_response TEXT,
            model        TEXT,
            persona      TEXT
        )
    """)
    # マイグレーション
    for stmt in (
        "ALTER TABLE sessions ADD COLUMN model TEXT DEFAULT 'claude-sonnet-4-5'",
        "ALTER TABLE sessions ADD COLUMN persona TEXT DEFAULT 'default'",
        "ALTER TABLE sessions ADD COLUMN permission_mode TEXT DEFAULT 'bypassPermissions'",
        "ALTER TABLE sessions ADD COLUMN thread_title TEXT",
        "ALTER TABLE sessions ADD COLUMN template TEXT DEFAULT NULL",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

def get_session(channel_id: str):
    """戻り値: (session_id, work_dir, model, persona, template, thread_title)"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT session_id, work_dir, model, COALESCE(persona,'default'), template, thread_title "
        "FROM sessions WHERE channel_id=?",
        (channel_id,)
    ).fetchone()
    conn.close()
    return row

def is_auto_respond(channel_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT enabled FROM auto_respond WHERE channel_id=?", (channel_id,)).fetchone()
    conn.close()
    return bool(row and row[0])

def set_auto_respond(channel_id: str, enabled: bool):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO auto_respond(channel_id, enabled) VALUES(?,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP",
        (channel_id, 1 if enabled else 0)
    )
    conn.commit()
    conn.close()

def save_thread_title(channel_id: str, title: str | None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE sessions SET thread_title=?, updated_at=CURRENT_TIMESTAMP WHERE channel_id=?",
        (title, channel_id)
    )
    conn.commit()
    conn.close()

def save_template(channel_id: str, template: str | None):
    """テンプレート設定（Noneで解除）"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT work_dir, model, COALESCE(persona,'default') FROM sessions WHERE channel_id=?",
        (channel_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE sessions SET template=?, updated_at=CURRENT_TIMESTAMP WHERE channel_id=?",
            (template, channel_id)
        )
    else:
        conn.execute(
            "INSERT INTO sessions(channel_id, session_id, work_dir, model, persona, template) "
            "VALUES(?,NULL,?,?,?,?)",
            (channel_id, str(WORK_DIR), DEFAULT_MODEL, "default", template)
        )
    conn.commit()
    conn.close()

def save_session(channel_id: str, session_id: str, work_dir: str,
                  model: str = DEFAULT_MODEL, persona: str | None = None):
    # VPS で作成された session jsonl は Mac-encoded dir にも hardlink して
    # Mac の Claude Code UI 側で時系列表示されるように
    try:
        if session_id and work_dir:
            _mirror_session_to_mac(session_id, work_dir)
    except Exception:
        pass

    conn = sqlite3.connect(DB_PATH)
    if persona is None:
        # 既存のpersonaを維持
        existing = conn.execute(
            "SELECT COALESCE(persona,'default') FROM sessions WHERE channel_id=?",
            (channel_id,)
        ).fetchone()
        persona = existing[0] if existing else "default"
    conn.execute("""
        INSERT INTO sessions(channel_id, session_id, work_dir, model, persona)
        VALUES(?,?,?,?,?)
        ON CONFLICT(channel_id) DO UPDATE SET
            session_id=excluded.session_id,
            work_dir=excluded.work_dir,
            model=excluded.model,
            persona=excluded.persona,
            updated_at=CURRENT_TIMESTAMP
    """, (channel_id, session_id, work_dir, model, persona))
    conn.commit()
    conn.close()

def save_persona(channel_id: str, persona: str):
    """ペルソナ更新（セッションは新規）"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT work_dir, model FROM sessions WHERE channel_id=?", (channel_id,)
    ).fetchone()
    work_dir = row[0] if row else str(WORK_DIR)
    model    = row[1] if row else DEFAULT_MODEL
    conn.execute("""
        INSERT INTO sessions(channel_id, session_id, work_dir, model, persona)
        VALUES(?,NULL,?,?,?)
        ON CONFLICT(channel_id) DO UPDATE SET
            session_id=NULL,
            persona=excluded.persona,
            updated_at=CURRENT_TIMESTAMP
    """, (channel_id, work_dir, model, persona))
    conn.commit()
    conn.close()

# ── ブランチ ────────────────────────────────────────
def save_branch(channel_id: str, label: str) -> bool:
    """現在のセッションを名前付きで保存"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT session_id, work_dir, model, COALESCE(persona,'default') FROM sessions WHERE channel_id=?",
        (channel_id,)
    ).fetchone()
    if not row or not row[0]:
        conn.close()
        return False
    conn.execute("""
        INSERT INTO branches(channel_id, label, session_id, work_dir, model, persona)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(channel_id, label) DO UPDATE SET
            session_id=excluded.session_id,
            work_dir=excluded.work_dir,
            model=excluded.model,
            persona=excluded.persona,
            created_at=CURRENT_TIMESTAMP
    """, (channel_id, label, row[0], row[1], row[2], row[3]))
    conn.commit()
    conn.close()
    return True

def list_branches(channel_id: str) -> list[tuple]:
    """戻り値: [(label, work_dir, model, persona, created_at), ...]"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT label, work_dir, model, COALESCE(persona,'default'), created_at
        FROM branches WHERE channel_id=?
        ORDER BY created_at DESC
    """, (channel_id,)).fetchall()
    conn.close()
    return rows

def load_branch(channel_id: str, label: str) -> bool:
    """保存したブランチを現在のセッションに復元"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT session_id, work_dir, model, COALESCE(persona,'default') FROM branches WHERE channel_id=? AND label=?",
        (channel_id, label)
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("""
        INSERT INTO sessions(channel_id, session_id, work_dir, model, persona)
        VALUES(?,?,?,?,?)
        ON CONFLICT(channel_id) DO UPDATE SET
            session_id=excluded.session_id,
            work_dir=excluded.work_dir,
            model=excluded.model,
            persona=excluded.persona,
            updated_at=CURRENT_TIMESTAMP
    """, (channel_id, *row))
    conn.commit()
    conn.close()
    return True

def delete_branch(channel_id: str, label: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "DELETE FROM branches WHERE channel_id=? AND label=?",
        (channel_id, label)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def save_model(channel_id: str, model: str):
    """モデルのみ更新（セッション・会話履歴は維持）"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT session_id, work_dir FROM sessions WHERE channel_id=?",
        (channel_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE sessions SET model=?, updated_at=CURRENT_TIMESTAMP WHERE channel_id=?",
            (model, channel_id)
        )
    else:
        conn.execute(
            "INSERT INTO sessions(channel_id, session_id, work_dir, model) VALUES(?,NULL,?,?)",
            (channel_id, str(WORK_DIR), model)
        )
    conn.commit()
    conn.close()

def save_work_dir(channel_id: str, new_work_dir: str) -> str | None:
    """作業ディレクトリを変更し、セッションIDをリセット。
    切り替え前のセッションを auto_<日時> としてブランチ保存し、ラベルを返す（復元用）。
    """
    auto_label = None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT session_id, work_dir, model, COALESCE(persona,'default') FROM sessions WHERE channel_id=?",
        (channel_id,)
    ).fetchone()
    model = row[2] if row else DEFAULT_MODEL
    # 既存セッションがあれば自動ブランチ保存
    if row and row[0] and row[1]:
        old_proj = Path(row[1]).name
        auto_label = f"auto_{old_proj}_{time.strftime('%H%M')}"[:50]
        try:
            conn.execute("""
                INSERT INTO branches(channel_id, label, session_id, work_dir, model, persona)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(channel_id, label) DO UPDATE SET
                    session_id=excluded.session_id,
                    work_dir=excluded.work_dir,
                    model=excluded.model,
                    persona=excluded.persona,
                    created_at=CURRENT_TIMESTAMP
            """, (channel_id, auto_label, row[0], row[1], row[2], row[3]))
        except Exception:
            auto_label = None
    conn.execute("""
        INSERT INTO sessions(channel_id, session_id, work_dir, model)
        VALUES(?,NULL,?,?)
        ON CONFLICT(channel_id) DO UPDATE SET
            session_id=NULL,
            work_dir=excluded.work_dir,
            updated_at=CURRENT_TIMESTAMP
    """, (channel_id, new_work_dir, model))
    conn.commit()
    conn.close()
    return auto_label

def get_projects() -> list[str]:
    """WORK_DIR直下のフォルダ一覧を返す"""
    try:
        return sorted([
            d.name for d in WORK_DIR.iterdir()
            if d.is_dir() and not d.name.startswith('.')
        ])
    except Exception:
        return []

def delete_session(channel_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM sessions WHERE channel_id=?", (channel_id,))
    conn.commit()
    conn.close()

def detect_model_switch(text: str) -> str | None:
    """正規表現でモデル切り替え意図を検出。モデル名を返す（なければNone）"""
    m = _NL_MODEL_PATTERN.search(text)
    if not m:
        return None
    keyword = next((g for g in m.groups() if g), None)
    if not keyword:
        return None
    keyword = MODEL_ALIASES.get(keyword, MODEL_ALIASES.get(keyword.lower(), keyword.lower()))
    return MODEL_ALIASES.get(keyword.lower())

def detect_permission_mode_switch(text: str) -> str | None:
    """自然言語から permission mode 切替意図を検出。モード名を返す（なければNone）"""
    m = _NL_PERMISSION_PATTERN.search(text)
    if not m:
        return None
    kw = next((g for g in m.groups() if g), None)
    if not kw:
        return None
    kw_norm = kw.lower().replace(" ", "").replace("\'","")
    return _PERMISSION_NL_ALIASES.get(kw_norm)

async def detect_project_switch_ai(text: str, projects: list[str]) -> str | None:
    """Gemini Flash でプロジェクト切り替え意図を検出。フォルダ名を返す（なければNone）"""
    if not GEMINI_KEY_TEXT or not projects:
        return None
    if len(text) > 150:
        return None

    # ファイル操作系の明示的な単語があれば即 None（Geminiに聞くまでもない）
    file_op_patterns = (
        "移動先", "保存先", "コピー先", "格納先", "出力先", "書き出し先", "配置先",
        "に移動して", "にコピーして", "に保存して", "に格納して", "に出力して",
        "へ移動", "へコピー", "へ保存", "を移動", "をコピー", "を保存",
        "ファイルを", "フォルダを", "ファイルの移動", "フォルダの移動",
    )
    if any(p in text for p in file_op_patterns):
        return None

    project_list = "\n".join(f"- {p}" for p in projects)
    prompt = (
        "あなたは「ユーザー自身の作業プロジェクトを切り替えたい」意図を検出する分類器です。\n"
        "以下のフォルダ一覧から、ユーザーが**自分の作業環境を切り替えたい**ときだけフォルダ名を返してください。\n\n"
        "🟢 切り替え意図あり（フォルダ名を返す）:\n"
        "- 「oracleに切り替えて」「2番のプロジェクトに移って」\n"
        "- 「sato-law-officeのとこで作業したい」「次は債権回収やろう」\n"
        "- 「kintoneプロジェクト開いて」「あのフォルダに行きたい」\n\n"
        "🔴 切り替え意図なし（none を返す・最重要）:\n"
        "- 「sato-law-officeに移動して」←ファイル/フォルダの移動操作\n"
        "- 「sato-law-officeのフォルダに保存」←ファイル保存先の指定\n"
        "- 「これをsato-law-officeに入れて」「sato-law-officeにコピー」\n"
        "- 「sato-law-officeの場所どこ？」「sato-law-officeの中身見せて」\n"
        "- フォルダ名が**目的地・保存先・参照先**として使われている\n\n"
        "判定基準:\n"
        "- ユーザー自身が「そのフォルダ環境に切り替えて作業を始めたい」明確な意図 → フォルダ名\n"
        "- ファイル操作・保存先指定・参照・質問の文脈 → 必ず none\n"
        "- 迷ったら none を返す（誤動作を避けるため厳格に）\n\n"
        f"フォルダ一覧:\n{project_list}\n\n"
        f"ユーザーのメッセージ: {text}\n\n"
        "回答（フォルダ名 or none のみ）:"
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-flash-lite-latest:generateContent?key={GEMINI_KEY_TEXT}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 64, "temperature": 0},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    answer = (
                        data["candidates"][0]["content"]["parts"][0]["text"]
                        .strip()
                    )
                    if answer.lower() == "none":
                        return None
                    # フォルダ名と前方一致または完全一致するものを探す
                    answer_lower = answer.lower()
                    for p in projects:
                        if p.lower() == answer_lower or answer_lower.startswith(p.lower()):
                            return p
                    # 部分一致フォールバック
                    for p in projects:
                        if p.lower() in answer_lower or answer_lower in p.lower():
                            return p
    except Exception:
        pass
    return None

async def detect_model_switch_ai(text: str) -> str | None:
    """Gemini Flash（無料）でモデル切り替え意図をAI判定。
    正規表現で拾えなかった曖昧な表現（「重いやつ」「一番賢いので」等）に対応。
    """
    if not GEMINI_KEY:
        return None
    # 短すぎる or 長すぎるメッセージはスキップ（長文は作業依頼の可能性が高い）
    if len(text) > 150:
        return None

    prompt = (
        "あなたはAIモデル切り替えの意図を検出する分類器です。\n"
        "以下3種類のモデルを管理しています:\n"
        "- opus: 最高性能・重い・遅い（例: 重いやつ/一番賢い/最強/Opus/クロードの上位版）\n"
        "- sonnet: 標準・バランス（例: 普通/標準/Sonnet/デフォルト）\n"
        "- haiku: 軽量・高速・安い（例: 軽いやつ/速いの/安いの/Haiku/手軽なの）\n\n"
        "ルール:\n"
        "- モデル切り替え要求なら opus / sonnet / haiku のいずれかだけ返す\n"
        "- モデル切り替えでないなら none だけ返す\n"
        "- 他の言葉は絶対に返さない\n\n"
        f"メッセージ: {text}"
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-flash-lite-latest:generateContent?key={GEMINI_KEY_TEXT}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 10, "temperature": 0},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    answer = (
                        data["candidates"][0]["content"]["parts"][0]["text"]
                        .strip().lower()
                    )
                    # 前方一致（"haiku\n"等に対応）
                    for model in ("opus", "sonnet", "haiku"):
                        if answer.startswith(model):
                            return model
    except Exception:
        pass
    return None

# ── テキスト分割 ────────────────────────────────────
def split_message(text: str, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    in_code_block = False
    lang = ""
    current = ""
    for line in text.split("\n"):
        if line.startswith("```"):
            in_code_block = not in_code_block
            lang = line[3:].strip() if in_code_block else ""
        candidate = current + line + "\n"
        if len(candidate) > limit:
            if in_code_block:
                current += "```\n"
            chunks.append(current.rstrip())
            current = (f"```{lang}\n" if in_code_block else "") + line + "\n"
        else:
            current = candidate
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [text[:limit]]

# ── Claude実行（ストリーミング進捗表示対応） ───────────
def _format_tool_call(name: str, inp: dict) -> str:
    """ツール呼び出しを1行サマリに整形"""
    name = name or "?"
    if name == "Bash":
        cmd = (inp.get("command", "") or "").replace("\n", " ⏎ ")
        return f"🖥️ `{cmd[:120]}`"
    if name == "Read":
        return f"📖 `{Path(inp.get('file_path','?')).name}`"
    if name in ("Edit", "Write"):
        emoji = "✏️" if name == "Edit" else "📝"
        return f"{emoji} `{Path(inp.get('file_path','?')).name}`"
    if name == "Glob":
        return f"🔍 `{inp.get('pattern','?')}`"
    if name == "Grep":
        return f"🔎 `{inp.get('pattern','?')}`"
    if name in ("WebFetch", "WebSearch"):
        q = inp.get("url") or inp.get("query", "?")
        return f"🌐 `{str(q)[:80]}`"
    if name == "TodoWrite":
        todos = inp.get("todos", [])
        return f"✅ ToDo更新 ({len(todos)}件)"
    if name == "Task":
        return f"🤖 サブエージェント `{inp.get('subagent_type','?')}`"
    # MCP / その他
    short = name.replace("mcp__", "")
    return f"🔧 `{short}`"

# ── スケジュールタスク ──────────────────────────────
def add_scheduled_task(channel_id: str, cron_expr: str, label: str, prompt: str,
                       work_dir: str, model: str, persona: str = "default",
                       template: str | None = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO scheduled_tasks(channel_id, cron_expr, label, prompt, work_dir, model, persona, template, enabled) "
        "VALUES(?,?,?,?,?,?,?,?,1)",
        (channel_id, cron_expr, label, prompt, work_dir, model, persona, template)
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id

def list_scheduled_tasks(channel_id: str | None = None) -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    if channel_id:
        rows = conn.execute(
            "SELECT id, channel_id, cron_expr, label, prompt, work_dir, model, persona, template, last_run, enabled "
            "FROM scheduled_tasks WHERE channel_id=? ORDER BY id",
            (channel_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, channel_id, cron_expr, label, prompt, work_dir, model, persona, template, last_run, enabled "
            "FROM scheduled_tasks WHERE enabled=1"
        ).fetchall()
    conn.close()
    return rows

def delete_scheduled_task(channel_id: str, task_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "DELETE FROM scheduled_tasks WHERE channel_id=? AND id=?",
        (channel_id, task_id)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def toggle_scheduled_task(channel_id: str, task_id: int, enabled: bool) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE scheduled_tasks SET enabled=? WHERE channel_id=? AND id=?",
        (1 if enabled else 0, channel_id, task_id)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def update_task_last_run(task_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE scheduled_tasks SET last_run=CURRENT_TIMESTAMP WHERE id=?",
        (task_id,)
    )
    conn.commit()
    conn.close()

async def parse_schedule_nl(text: str) -> tuple[str, str, str] | None:
    """Geminiで自然言語スケジュールをパース。
    返り値: (cron_expr, label, prompt) or None
    """
    if not GEMINI_KEY:
        return None
    prompt = (
        "次の文を「定期実行スケジュール」として解釈してください。\n"
        "以下のJSONを返してください（コードブロックなし、JSONだけ）:\n"
        '{"cron":"<5フィールドcron式>","label":"<短い識別名>","prompt":"<実行する指示>"}\n\n'
        "ルール:\n"
        "- cronは標準5フィールド形式（分 時 日 月 曜日）\n"
        "- 曜日は0-6（0=日曜）。範囲は「1-5」、リストは「1,3,5」\n"
        "- 「毎朝」「毎日」「毎週月曜」「平日」「30分ごと」等を解釈\n"
        "- スケジュール表現でない場合は {\"cron\":null} を返す\n\n"
        "例:\n"
        "「毎朝7時に天気予報を教えて」→ "
        '{"cron":"0 7 * * *","label":"朝の天気","prompt":"今日の天気予報を簡潔に教えて"}\n'
        "「平日の18時にその日のニュース要約」→ "
        '{"cron":"0 18 * * 1-5","label":"平日ニュース","prompt":"今日の主要ニュースを5項目で要約して"}\n\n'
        f"入力: {text}"
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY_TEXT}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 256, "responseMimeType": "application/json"},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                text_resp = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                parsed = json.loads(text_resp)
                if not parsed.get("cron"):
                    return None
                cron = parsed["cron"]
                # croniterで検証
                from croniter import croniter
                if not croniter.is_valid(cron):
                    return None
                return (cron, parsed.get("label", "task")[:50], parsed.get("prompt", "").strip())
    except Exception:
        return None

async def scheduler_loop():
    """30秒ごとに発火タスクをチェックして実行"""
    from croniter import croniter
    from datetime import datetime
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.now()
            for task in list_scheduled_tasks():
                tid, ch_id, cron, label, prompt, wd, model, persona, template, last_run, enabled = task
                # 前回実行時刻ベースで次回時刻計算
                base = datetime.fromisoformat(last_run) if last_run else now.replace(second=0, microsecond=0)
                try:
                    next_run = croniter(cron, base).get_next(datetime)
                except Exception:
                    continue
                if next_run <= now:
                    # 実行
                    try:
                        channel = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
                        await channel.send(f"⏰ スケジュール実行: `{label}`")
                        # ダミーメッセージで run_claude を呼ぶための工夫
                        # シンプルに channel.send で結果を返す形にする
                        await _run_scheduled_task(channel, ch_id, prompt, model, persona, template, wd)
                    except Exception as e:
                        print(f"[scheduler] task {tid} failed: {e}")
                    update_task_last_run(tid)
        except Exception as e:
            print(f"[scheduler loop error] {e}")
        await asyncio.sleep(30)

async def _generate_daily_report(channel, channel_id: str, immediate: bool = False):
    """その日のインタラクションログを集計してClaudeに要約させ、Obsidianに保存"""
    from datetime import datetime, timedelta
    today = datetime.now().date()
    start = datetime(today.year, today.month, today.day)
    end   = start + timedelta(days=1)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT timestamp, user_msg, bot_response, model, persona
        FROM interactions
        WHERE channel_id=? AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    """, (channel_id, start.isoformat(), end.isoformat())).fetchall()
    conn.close()
    if not rows:
        if immediate:
            await channel.send("📊 今日はまだ会話履歴がありません。")
        return
    # 簡易テキスト化
    log_text = "\n\n".join(
        f"### {ts[11:16]} ({model}/{persona})\n👤 {umsg[:300]}\n🤖 {bres[:500]}"
        for ts, umsg, bres, model, persona in rows
    )
    summary_prompt = (
        "以下は今日のClaude Botとの会話履歴です。"
        "Markdown形式でデイリーレポートを作成してください。\n"
        "# 構成\n"
        "## 🎯 今日の主なトピック（3〜5項目）\n"
        "## ✅ 完了したこと\n"
        "## 💡 重要な気づき・決定事項\n"
        "## 📝 翌日への引き継ぎ\n"
        "## 📊 統計（やりとり件数、使ったモデル）\n\n"
        f"---\n会話ログ:\n{log_text[:30000]}"
    )
    # シンプルにClaude呼び出し
    args = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--input-format", "text",
        "--model", "sonnet",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY_CONSOLE")}
    # ANTHROPIC_API_KEY はユーザーが明示的に環境変数で設定した時のみ subprocess に渡す
    if os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd="/tmp", env=env,
        limit=10 * 1024 * 1024,
    )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(summary_prompt.encode()), timeout=180
        )
        report = ""
        for line in stdout.decode(errors="replace").splitlines():
            try:
                ev = json.loads(line.strip())
                if ev.get("type") == "result":
                    report = ev.get("result", "")
            except Exception:
                continue
        if not report:
            report = "（要約生成失敗）"
        # Obsidianに保存
        report_dir = Path("/home/ubuntu/obsidian/Claude Code/Discord デイリー")
        report_dir.mkdir(parents=True, exist_ok=True)
        out_path = report_dir / f"{today.isoformat()}.md"
        ch_name = getattr(channel, "name", "DM")
        header = f"# Discord Bot デイリーレポート ({today.isoformat()})\n\nチャンネル: {ch_name}\n\n---\n\n"
        out_path.write_text(header + report, encoding="utf-8")
        await channel.send(
            f"📊 デイリーレポート生成完了 ({len(rows)}件のやりとり)\n"
            f"保存先: `{out_path.name}` (Obsidian/Claude Code/Discord デイリー/)"
        )
        # サマリ送信
        for chunk in split_message(report)[:3]:  # 長すぎる場合は最初の3チャンクだけ
            await channel.send(chunk)
    except Exception as e:
        await channel.send(f"⚠️ レポート生成エラー: {e}")

async def _run_scheduled_task(channel, channel_id: str, prompt: str, model: str,
                                persona: str, template: str | None, work_dir: str):
    """スケジュール起動時のClaude実行（簡易版・進捗表示なし）"""
    # 特殊マーカー: デイリーレポート
    if prompt == "__DAILY_REPORT__":
        await _generate_daily_report(channel, channel_id, immediate=False)
        return
    args = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--input-format", "text",
        "--model", model,
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    persona_data = PERSONAS.get(persona, PERSONAS["default"])
    system_parts = []
    if persona_data["prompt"]:
        system_parts.append(persona_data["prompt"])
    if template and template in TEMPLATES:
        system_parts.append(TEMPLATES[template]["prompt"])
    if system_parts:
        args += ["--append-system-prompt", "\n\n".join(system_parts)]

    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY_CONSOLE")}
    # ANTHROPIC_API_KEY はユーザーが明示的に環境変数で設定した時のみ subprocess に渡す
    if os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env=env,
        limit=10 * 1024 * 1024,
    )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=300
        )
        result_text = ""
        for line in stdout.decode(errors="replace").splitlines():
            try:
                event = json.loads(line.strip())
            except Exception:
                continue
            if event.get("type") == "result":
                result_text = event.get("result", "")
        if not result_text:
            result_text = "（応答なし）"
        for chunk in split_message(result_text):
            await channel.send(chunk)
    except Exception as e:
        await channel.send(f"⚠️ スケジュール実行エラー: {e}")

def log_interaction(channel_id: str, user_msg: str, bot_response: str, model: str, persona: str):
    """インタラクションをログ（デイリーレポート用）"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO interactions(channel_id, user_msg, bot_response, model, persona) "
            "VALUES(?,?,?,?,?)",
            (channel_id, user_msg[:2000], bot_response[:5000], model, persona)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

async def run_claude_sdk(prompt: str, channel_id: str, attachment_texts: list[str],
                          model: str, persona: str, message: discord.Message,
                          template: str | None, perm_mode: str) -> tuple[str, str]:
    """SDK + PreToolUse Hook で Permission Buttons を実現する版"""
    row = get_session(channel_id)
    session_id = row[0] if row else None
    work_dir   = Path(row[1]) if row else WORK_DIR
    work_dir.mkdir(parents=True, exist_ok=True)

    full_prompt = prompt
    for txt in attachment_texts:
        full_prompt += f"\n\n---添付ファイル---\n{txt}"

    # ペルソナ＋テンプレートを system prompt として付与
    persona_data = PERSONAS.get(persona, PERSONAS["default"])
    system_parts = []
    if persona_data["prompt"]:
        system_parts.append(persona_data["prompt"])
    if template and template in TEMPLATES:
        system_parts.append(TEMPLATES[template]["prompt"])
    sys_prompt = "\n\n".join(system_parts) if system_parts else None

    # Permission UI と Hook
    sess_allowed = _SESSION_ALLOWED_TOOLS.setdefault(channel_id, set())
    perm_ui = DiscordPermissionUI(message.channel, allowed_session=sess_allowed)
    hook_fn = make_pretool_hook(perm_ui)

    opts_kwargs = dict(
        cwd=str(work_dir),
        model=model,
        permission_mode=perm_mode if perm_mode in ("default", "acceptEdits", "auto", "plan", "dontAsk") else "default",
        hooks={"PreToolUse": [HookMatcher(matcher="*", hooks=[hook_fn])]},
    )
    if sys_prompt:
        # SystemPromptPreset is the structured way; for simple append, use system_prompt + custom appendin
        # In SDK 0.1.63, system_prompt is just a string and it REPLACES the system prompt.
        # We use it to append by including the default behavior + our addition.
        # Actually just providing a string sets it. Combine with the default Claude Code SP isn't easy.
        # So we use it carefully: if user has a persona/template, set system_prompt to that (override Claude Code's default SP)
        # That's a tradeoff; persona/template still injects but loses Claude Code's default SP.
        # For now just do that.
        opts_kwargs["system_prompt"] = sys_prompt
    if session_id:
        opts_kwargs["resume"] = session_id

    opts = ClaudeAgentOptions(**opts_kwargs)

    progress_msg = await message.reply(f"🤔 考え中… (mode: {perm_mode})")
    progress_log: list[str] = []
    last_update = 0.0
    UPDATE_INTERVAL = 1.5

    async def push_progress(force: bool = False):
        nonlocal last_update
        now = asyncio.get_event_loop().time()
        if not force and (now - last_update < UPDATE_INTERVAL):
            return
        last_update = now
        recent = progress_log[-10:]
        body = f"🤔 考え中… (mode: {perm_mode})\n" + "\n".join(recent)
        if len(body) > 1900:
            body = body[:1900] + "…"
        try:
            await progress_msg.edit(content=body)
        except discord.HTTPException:
            pass

    result_text = ""
    new_session_id = session_id

    try:
        async with ClaudeSDKClient(opts) as client:
            await client.query(full_prompt)
            async for msg in client.receive_response():
                t = type(msg).__name__
                if t == "AssistantMessage":
                    for block in msg.content or []:
                        bt = type(block).__name__
                        if bt == "ToolUseBlock":
                            progress_log.append(_format_tool_call(
                                block.name, block.input or {}
                            ))
                            await push_progress()
                elif t == "ResultMessage":
                    result_text = msg.result or "（応答なし）"
                    new_session_id = getattr(msg, "session_id", session_id) or session_id
                    break
    except Exception as e:
        result_text = f"⚠️ SDK実行エラー: `{type(e).__name__}: {str(e)[:300]}`"

    if not result_text:
        result_text = "（応答なし）"

    if new_session_id:
        save_session(channel_id, new_session_id, str(work_dir), model, persona)

    log_interaction(channel_id, prompt, result_text, model, persona)

    status = get_status_line(channel_id)
    chunks = split_message(result_text)
    final_first = chunks[0] + (f"\n\n{status}" if len(chunks) == 1 else "")
    if len(final_first) > 2000:
        final_first = final_first[:1990] + "…"
    try:
        await progress_msg.edit(content=final_first)
    except discord.HTTPException:
        await message.channel.send(final_first[:2000])

    for i, chunk in enumerate(chunks[1:], start=1):
        is_last = (i == len(chunks) - 1)
        text = chunk + (f"\n\n{status}" if is_last else "")
        await message.channel.send(text[:2000])

    # 完了通知
    try:
        await message.channel.send(
            f"✅ {message.author.mention} 完了しました。",
            allowed_mentions=discord.AllowedMentions(users=[message.author])
        )
    except Exception:
        pass

    return result_text, str(work_dir)


async def run_claude(prompt: str, channel_id: str, attachment_texts: list[str],
                      model: str, persona: str, message: discord.Message,
                      template: str | None = None) -> tuple[str, str]:
    """Claudeを実行（ストリーミング進捗表示）してテキスト結果とwork_dirを返す"""
    perm_mode = get_permission_mode(channel_id)
    # bypassPermissions 以外 + SDK使えるなら SDK 経由（Permission Button 出る）
    if SDK_AVAILABLE and perm_mode != "bypassPermissions":
        try:
            return await run_claude_sdk(
                prompt, channel_id, attachment_texts, model, persona, message, template, perm_mode
            )
        except Exception as e:
            # SDK エラー時は subprocess fallback
            try:
                await message.reply(f"⚠️ SDK実行失敗、subprocess fallback: `{type(e).__name__}: {str(e)[:200]}`")
            except Exception:
                pass
    row = get_session(channel_id)
    session_id = row[0] if row else None
    work_dir   = Path(row[1]) if row else WORK_DIR
    work_dir.mkdir(parents=True, exist_ok=True)

    full_prompt = prompt
    for txt in attachment_texts:
        full_prompt += f"\n\n---添付ファイル---\n{txt}"

    args = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--input-format", "text",
        "--model", model,
        "--verbose",
    ]
    # 権限モード追加
    perm_mode = get_permission_mode(str(message.channel.id))
    args += PERMISSION_MODES.get(perm_mode, PERMISSION_MODES[DEFAULT_PERMISSION_MODE])["cli_args"]
    if session_id:
        args += ["--resume", session_id]

    # ペルソナ＋テンプレートを system prompt として付与
    persona_data = PERSONAS.get(persona, PERSONAS["default"])
    system_parts = []
    if persona_data["prompt"]:
        system_parts.append(persona_data["prompt"])
    if template and template in TEMPLATES:
        system_parts.append(TEMPLATES[template]["prompt"])
    # 自殺防止のみ（bot.pyの編集は許可、ただし衝突回避手順あり）
    system_parts.append(
        "重要: あなたはDiscord Bot の中で動いている Claude Code です。\n"
        "【必須】`discord-claude-bot` systemd serviceを restart/stop/kill しない（自殺になる）。\n"
        "systemd reload が必要な変更を bot.py に加えた場合はユーザーに「再起動してください」と伝えるだけにする。\n"
        "\n"
        "【bot.py 編集時の衝突回避ルール】Mac側のClaude Codeが同時に編集してる可能性あり。\n"
        "1. 編集前に必ず `stat /home/ubuntu/discord-bot/bot.py` で mtime を確認\n"
        "2. mtime が **直近5分以内** だったら、ユーザーに『Mac側で編集中の可能性があります。続行しますか？』と確認してから編集\n"
        "3. 編集後は即座に `cd /home/ubuntu/discord-bot && git add bot.py && git commit -m 'feat: ...' && git push` で履歴に残す（Mac側が追従できる）\n"
        "4. git conflict が出たら無理に解決せず、ユーザーに報告して手動解決を依頼\n"
        "\n"
        "bot.py 以外の通常作業は自由に行って問題ありません。"
    )
    args += ["--append-system-prompt", "\n\n".join(system_parts)]

    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY_CONSOLE")}
    # ANTHROPIC_API_KEY はユーザーが明示的に環境変数で設定した時のみ subprocess に渡す
    if os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
        env=env,
        limit=10 * 1024 * 1024,
    )

    # プロンプトをstdin送信
    proc.stdin.write(full_prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    # 進捗メッセージ
    progress_msg = await message.reply("🤔 考え中…")
    progress_log: list[str] = []
    last_update = 0.0
    UPDATE_INTERVAL = 1.5  # Discord編集レート制限考慮

    async def push_progress(force: bool = False):
        nonlocal last_update
        now = asyncio.get_event_loop().time()
        if not force and (now - last_update < UPDATE_INTERVAL):
            return
        last_update = now
        # 直近10件だけ表示（多すぎると2000文字超過するので）
        recent = progress_log[-10:]
        body = "🤔 考え中…\n" + "\n".join(recent)
        if len(body) > 1900:
            body = body[:1900] + "…"
        try:
            await progress_msg.edit(content=body)
        except discord.HTTPException:
            pass

    result_text = ""
    new_session_id = session_id

    async def consume_stdout():
        nonlocal result_text, new_session_id
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode(errors="replace").strip())
            except (json.JSONDecodeError, ValueError):
                continue
            etype = event.get("type", "")
            if etype == "result":
                result_text    = event.get("result", "（応答なし）")
                new_session_id = event.get("session_id", session_id)
            elif etype == "assistant":
                msg = event.get("message", {}) or {}
                for block in msg.get("content", []) or []:
                    btype = block.get("type", "")
                    if btype == "tool_use":
                        progress_log.append(_format_tool_call(
                            block.get("name", ""), block.get("input", {}) or {}
                        ))
                        await push_progress()

    try:
        await asyncio.wait_for(consume_stdout(), timeout=300)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise

    await proc.wait()

    if not result_text:
        try:
            err = (await proc.stderr.read()).decode(errors="replace")
            result_text = f"⚠️ エラー:\n```\n{err[:1500]}\n```"
        except Exception:
            result_text = "（応答なし）"

    if new_session_id:
        save_session(channel_id, new_session_id, str(work_dir), model, persona)

    # インタラクションログ（デイリーレポート用）
    log_interaction(channel_id, prompt, result_text, model, persona)

    # 進捗メッセージを最終結果で置き換え（1チャンク目）
    status = get_status_line(channel_id)
    chunks = split_message(result_text)
    final_first = chunks[0] + (f"\n\n{status}" if len(chunks) == 1 else "")
    if len(final_first) > 2000:
        final_first = final_first[:1990] + "…"
    try:
        await progress_msg.edit(content=final_first)
    except discord.HTTPException:
        # 編集できなかったら新規送信
        await message.channel.send(final_first[:2000])

    # 残りのチャンク
    for i, chunk in enumerate(chunks[1:], start=1):
        is_last = (i == len(chunks) - 1)
        text = chunk + (f"\n\n{status}" if is_last else "")
        await message.channel.send(text[:2000])

    # 完了通知 (ユーザーをpingして気づかせる)
    try:
        await message.channel.send(
            f"✅ {message.author.mention} 完了しました。",
            allowed_mentions=discord.AllowedMentions(users=[message.author])
        )
    except Exception:
        pass

    return result_text, str(work_dir)

# ── Discord Bot ─────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# DM・User-install で Slash Commands を使えるようにする
DM_ALLOWED  = discord.app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
DM_INSTALLS = discord.app_commands.AppInstallationType(guild=True, user=True)

def get_status_line(channel_id: str) -> str:
    """現在のプロジェクト・スレッド・モデル・モード・ペルソナ・テンプレートを1行で返す"""
    row = get_session(channel_id)
    project  = Path(row[1]).name if row and row[1] else WORK_DIR.name
    model    = row[2] if row and row[2] else DEFAULT_MODEL
    persona  = row[3] if row and len(row) > 3 and row[3] else "default"
    template = row[4] if row and len(row) > 4 and row[4] else None
    thread_title = row[5] if row and len(row) > 5 and row[5] else None
    perm_mode = get_permission_mode(channel_id)
    m_emoji  = MODEL_EMOJI.get(model, "🤖")
    p_data   = PERSONAS.get(persona, PERSONAS["default"])
    p_part   = "" if persona == "default" else f"  ｜  {p_data['emoji']} `{p_data['label']}`"
    t_part   = ""
    if template and template in TEMPLATES:
        t_data = TEMPLATES[template]
        t_part = f"  ｜  {t_data['emoji']} `{t_data['label']}`"
    perm_v = PERMISSION_MODES.get(perm_mode, {})
    perm_emoji = perm_v.get("emoji", "🔒")
    perm_label = perm_v.get("label", perm_mode)
    thread_part = f" / 🧵 `{thread_title}`" if thread_title else " / 🧵 `(新規)`"
    return f"-# 📁 `{project}`{thread_part}  ｜  {m_emoji} `{model}`  ｜  {perm_emoji} `{perm_label}`{p_part}{t_part}"

async def update_channel_topic(channel: discord.abc.Messageable, channel_id: str):
    """チャンネルトピックに現在のプロジェクト・スレッド・モデル・モードを表示。"""
    if not isinstance(channel, discord.TextChannel):
        return
    row = get_session(channel_id)
    project = Path(row[1]).name if row and row[1] else WORK_DIR.name
    model   = row[2] if row and row[2] else DEFAULT_MODEL
    emoji   = MODEL_EMOJI.get(model, "🤖")
    thread_title = row[5] if row and len(row) > 5 and row[5] else None
    perm_mode = get_permission_mode(channel_id)
    perm_v = PERMISSION_MODES.get(perm_mode, {})
    perm_emoji = perm_v.get("emoji", "🔒")
    thread_part = f" / 🧵{thread_title}" if thread_title else ""
    topic   = f"📁 {project}{thread_part}  ｜  {emoji} {model}  ｜  {perm_emoji}"
    topic = topic[:1024]  # Discord topic max
    try:
        await channel.edit(topic=topic)
    except (discord.Forbidden, discord.HTTPException):
        pass  # 権限なし or レート制限は無視

# ── 添付ファイル処理 ───────────────────────────────
import base64
import tempfile
import time
import urllib.parse

ATTACHMENT_DIR = Path("/tmp/discord-attachments")
ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
OBSIDIAN_SAVE_DIR = Path("/home/ubuntu/obsidian/Discord保存")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".oga", ".opus"}
PDF_EXTS   = {".pdf"}
OFFICE_EXTS = {".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".csv"}
TEXT_PREFIXES = ("text/", "application/json", "application/xml", "application/yaml")

async def _save_attachment_to_disk(att: discord.Attachment, channel_id: str) -> Path:
    """添付ファイルを /tmp/discord-attachments/<channel_id>/ に保存"""
    ch_dir = ATTACHMENT_DIR / channel_id
    ch_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^\w.\-]', '_', att.filename)
    timestamp = int(time.time())
    out_path = ch_dir / f"{timestamp}_{safe_name}"
    async with aiohttp.ClientSession() as session:
        async with session.get(att.url) as resp:
            data = await resp.read()
    out_path.write_bytes(data)
    return out_path

async def _transcribe_audio_gemini(audio_bytes: bytes, mime_type: str) -> str | None:
    """Gemini Flash で音声を文字起こし。返り値はテキスト（失敗時None）"""
    if not GEMINI_KEY:
        return None
    if len(audio_bytes) > 19 * 1024 * 1024:  # inline_data 上限約20MB
        return None
    b64 = base64.b64encode(audio_bytes).decode()
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY_TEXT}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": "この音声を日本語で正確に文字起こししてください。話者が複数いる場合は「話者A:」「話者B:」のように区別してください。文字起こしのテキストのみを返してください。"},
                {"inline_data": {"mime_type": mime_type, "data": b64}},
            ]
        }],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return None
    except Exception:
        return None

async def download_attachment(att: discord.Attachment, channel_id: str) -> str | None:
    """添付ファイルを処理してプロンプトに追加するテキストを返す。
    画像/PDF/Office: 一時保存してClaudeにパスを伝える（ClaudeがRead/Bashで処理）
    音声: Geminiで文字起こしして本文に埋め込む
    テキスト系: 内容をそのまま埋め込む
    """
    ext = Path(att.filename).suffix.lower()
    ctype = (att.content_type or "").lower()

    # 巨大ファイルは保存だけしてパス通知
    if att.size > 25 * 1024 * 1024:
        return f"[ファイル {att.filename} は大きすぎ ({att.size//1024//1024}MB) - 処理スキップ]"

    # 画像 → Claudeのvision (Read tool) で読める
    if ctype.startswith("image/") or ext in IMAGE_EXTS:
        path = await _save_attachment_to_disk(att, channel_id)
        return (
            f"[画像ファイルが添付されました: {att.filename}]\n"
            f"保存場所: `{path}`\n"
            f"→ Read tool でこの画像を読み取って内容を分析してください。"
        )

    # PDF → ClaudeのRead toolが対応
    if ext in PDF_EXTS or ctype == "application/pdf":
        path = await _save_attachment_to_disk(att, channel_id)
        return (
            f"[PDFが添付されました: {att.filename}]\n"
            f"保存場所: `{path}`\n"
            f"→ Read tool でPDFを読んで内容を要約・回答してください。"
        )

    # 音声 → Geminiで文字起こし
    if ctype.startswith("audio/") or ext in AUDIO_EXTS:
        async with aiohttp.ClientSession() as session:
            async with session.get(att.url) as resp:
                audio_bytes = await resp.read()
        mime = ctype if ctype.startswith("audio/") else f"audio/{ext.lstrip('.')}"
        # Gemini APIでよく使われるMIMEに正規化
        mime_map = {"audio/m4a": "audio/mp4", "audio/oga": "audio/ogg"}
        mime = mime_map.get(mime, mime)
        transcript = await _transcribe_audio_gemini(audio_bytes, mime)
        if transcript:
            return (
                f"[音声ファイル {att.filename} の文字起こし（Gemini Flash）]\n"
                f"```\n{transcript[:8000]}\n```"
            )
        # 失敗時はファイルパスだけ通知
        path = await _save_attachment_to_disk(att, channel_id)
        return (
            f"[音声ファイル {att.filename} - 自動文字起こし失敗]\n"
            f"保存場所: `{path}`\n"
            f"→ ffmpeg等で処理を試みてください。"
        )

    # Office系 → ファイル保存してClaudeにツール使ってもらう
    if ext in OFFICE_EXTS:
        path = await _save_attachment_to_disk(att, channel_id)
        return (
            f"[Officeファイルが添付されました: {att.filename}]\n"
            f"保存場所: `{path}`\n"
            f"→ pandas/openpyxl/python-docx/python-pptx 等で内容を読み取って分析してください。"
        )

    # テキスト系 → 直接埋め込み
    if att.size <= 500_000 and any(ctype.startswith(p) for p in TEXT_PREFIXES):
        async with aiohttp.ClientSession() as session:
            async with session.get(att.url) as resp:
                data = await resp.read()
                return f"[{att.filename}]\n{data.decode(errors='replace')}"

    # その他 → ファイル保存だけ
    path = await _save_attachment_to_disk(att, channel_id)
    return (
        f"[添付ファイル: {att.filename} (type={ctype or '?'})]\n"
        f"保存場所: `{path}`\n"
        f"→ 必要なら適切なツールで読み取ってください。"
    )

async def handle_message(message: discord.Message, content: str):
    """メッセージ処理の共通ロジック"""
    channel_id = str(message.channel.id)

    stripped = content.strip()
    lower = stripped.lower()

    # /clear コマンド
    if lower in ("/clear", "!clear"):
        delete_session(channel_id)
        await message.reply(f"🗑️ セッションをリセットしました。\n{get_status_line(channel_id)}")
        await update_channel_topic(message.channel, channel_id)
        return

    # /persona コマンド（例: /persona, /persona 執事, /persona default）
    persona_match = re.match(r'^[/!]persona(?:\s+(.+))?$', stripped, re.IGNORECASE)
    if persona_match:
        requested = (persona_match.group(1) or "").strip()
        if not requested:
            row = get_session(channel_id)
            current = row[3] if row and len(row) > 3 and row[3] else "default"
            cur_data = PERSONAS.get(current, PERSONAS["default"])
            options = "\n".join(
                f"  {p['emoji']} `{key}` ({p['label']})"
                for key, p in PERSONAS.items()
            )
            await message.reply(
                f"{cur_data['emoji']} 現在のキャラクター: `{current}` ({cur_data['label']})\n\n"
                f"切り替え可能:\n{options}\n\n"
                f"例: `/persona 執事` または `/persona gal`"
            )
        else:
            key = PERSONA_ALIASES.get(requested.lower()) or PERSONA_ALIASES.get(requested)
            if not key:
                # キー名で直接指定された場合も許容
                if requested.lower() in PERSONAS:
                    key = requested.lower()
            if not key:
                await message.reply(
                    f"❌ 不明なキャラクター: `{requested}`\n"
                    f"使用可能: {', '.join(PERSONAS.keys())}"
                )
            else:
                save_persona(channel_id, key)
                p = PERSONAS[key]
                await message.reply(
                    f"{p['emoji']} キャラクターを `{p['label']}` に切り替えました。\n"
                    f"（次の応答から反映されます。会話履歴はリセット）\n"
                    f"{get_status_line(channel_id)}"
                )
                await update_channel_topic(message.channel, channel_id)
        return

    # /schedule コマンド
    schedule_match = re.match(r'^[/!]schedule(?:\s+(\S+)(?:\s+(.+))?)?$', stripped, re.IGNORECASE)
    if schedule_match:
        sub = (schedule_match.group(1) or "").lower()
        arg = (schedule_match.group(2) or "").strip()
        if not sub or sub == "list":
            tasks = list_scheduled_tasks(channel_id)
            if not tasks:
                await message.reply(
                    "⏰ スケジュールはありません。\n"
                    "登録: `/schedule add 毎朝7時に天気を教えて`\n"
                    "一覧: `/schedule list`\n"
                    "削除: `/schedule delete <id>`\n"
                    "停止: `/schedule pause <id>`\n"
                    "再開: `/schedule resume <id>`"
                )
            else:
                lines = []
                for t in tasks:
                    tid, _, cron, label, prompt, _, model, _, _, last, enabled = t
                    status = "▶️" if enabled else "⏸️"
                    lines.append(
                        f"  {status} `#{tid}` `{cron}` — **{label}**\n"
                        f"      → {prompt[:80]}{'…' if len(prompt) > 80 else ''}"
                    )
                await message.reply(f"⏰ 登録済みスケジュール:\n" + "\n".join(lines))
        elif sub == "add":
            if not arg:
                await message.reply(
                    "❌ スケジュール内容を指定してください\n"
                    "例: `/schedule add 毎朝7時に天気を教えて`"
                )
            else:
                # 自然言語パース
                parsed = await parse_schedule_nl(arg)
                if not parsed:
                    await message.reply(
                        "❌ スケジュールを解釈できませんでした。\n"
                        "例:「毎朝7時に〇〇」「平日の18時に〇〇」「30分ごとに〇〇」"
                    )
                else:
                    cron, label, sched_prompt = parsed
                    row = get_session(channel_id)
                    cur_wd      = row[1] if row and row[1] else str(WORK_DIR)
                    cur_model   = row[2] if row and row[2] else DEFAULT_MODEL
                    cur_persona = row[3] if row and len(row) > 3 and row[3] else "default"
                    cur_tmpl    = row[4] if row and len(row) > 4 and row[4] else None
                    tid = add_scheduled_task(
                        channel_id, cron, label, sched_prompt,
                        cur_wd, cur_model, cur_persona, cur_tmpl
                    )
                    await message.reply(
                        f"⏰ スケジュール登録完了 `#{tid}`\n"
                        f"  📛 名前: **{label}**\n"
                        f"  ⏱️ cron: `{cron}`\n"
                        f"  📝 内容: {sched_prompt}\n"
                        f"  🤖 モデル: `{cur_model}` / 🎭 ペルソナ: `{cur_persona}`"
                    )
        elif sub == "delete":
            if not arg.isdigit():
                await message.reply("❌ 削除するID（数字）を指定: `/schedule delete 3`")
            elif delete_scheduled_task(channel_id, int(arg)):
                await message.reply(f"🗑️ スケジュール `#{arg}` を削除しました。")
            else:
                await message.reply(f"❌ スケジュール `#{arg}` が見つかりません。")
        elif sub in ("pause", "resume"):
            if not arg.isdigit():
                await message.reply(f"❌ {sub}するID（数字）を指定: `/schedule {sub} 3`")
            elif toggle_scheduled_task(channel_id, int(arg), enabled=(sub == "resume")):
                emoji = "⏸️" if sub == "pause" else "▶️"
                await message.reply(f"{emoji} スケジュール `#{arg}` を{'停止' if sub == 'pause' else '再開'}しました。")
            else:
                await message.reply(f"❌ スケジュール `#{arg}` が見つかりません。")
        else:
            await message.reply(
                "使い方:\n"
                "  `/schedule add <自然言語>` - 例: 毎朝7時に天気\n"
                "  `/schedule list` - 一覧\n"
                "  `/schedule delete <id>` - 削除\n"
                "  `/schedule pause <id>` - 停止\n"
                "  `/schedule resume <id>` - 再開"
            )
        return

    # /report コマンド（デイリーレポート）
    report_match = re.match(r'^[/!]report(?:\s+(\S+))?$', stripped, re.IGNORECASE)
    if report_match:
        sub = (report_match.group(1) or "").lower()
        if not sub or sub in ("today", "now"):
            # 今すぐ今日分のレポートを生成
            await _generate_daily_report(message.channel, channel_id, immediate=True)
        elif sub == "enable":
            # 毎晩23時に自動実行
            row = get_session(channel_id)
            cur_wd      = row[1] if row and row[1] else str(WORK_DIR)
            cur_model   = row[2] if row and row[2] else DEFAULT_MODEL
            cur_persona = row[3] if row and len(row) > 3 and row[3] else "default"
            tid = add_scheduled_task(
                channel_id, "0 23 * * *", "デイリーレポート",
                "__DAILY_REPORT__",  # 特殊マーカー
                cur_wd, cur_model, cur_persona
            )
            await message.reply(
                f"📊 毎日23時にデイリーレポートを自動生成します（`/schedule` で確認・解除）\n"
                f"今すぐ生成: `/report` または `/report today`"
            )
        else:
            await message.reply(
                "📊 デイリーレポート:\n"
                "  `/report` または `/report today` - 今日分を今すぐ生成\n"
                "  `/report enable` - 毎晩23時に自動生成を有効化"
            )
        return

    # /template コマンド
    template_match = re.match(r'^[/!]template(?:\s+(.+))?$', stripped, re.IGNORECASE)
    if template_match:
        requested = (template_match.group(1) or "").strip()
        if not requested:
            row = get_session(channel_id)
            current = row[4] if row and len(row) > 4 and row[4] else None
            if current and current in TEMPLATES:
                cur_data = TEMPLATES[current]
                cur_str = f"{cur_data['emoji']} `{current}` ({cur_data['label']})"
            else:
                cur_str = "なし"
            options = "\n".join(
                f"  {t['emoji']} `{key}` ({t['label']})"
                for key, t in TEMPLATES.items()
            )
            await message.reply(
                f"📋 現在のテンプレート: {cur_str}\n\n"
                f"使えるテンプレート:\n{options}\n\n"
                f"設定: `/template 議事録` / 解除: `/template clear`"
            )
        elif requested.lower() in ("clear", "off", "解除", "なし", "none"):
            save_template(channel_id, None)
            await message.reply(
                f"📋 テンプレートを解除しました。\n{get_status_line(channel_id)}"
            )
        else:
            key = TEMPLATE_ALIASES.get(requested.lower()) or TEMPLATE_ALIASES.get(requested)
            if not key and requested.lower() in TEMPLATES:
                key = requested.lower()
            if not key:
                await message.reply(
                    f"❌ 不明なテンプレート: `{requested}`\n"
                    f"使用可能: {', '.join(TEMPLATES.keys())}"
                )
            else:
                save_template(channel_id, key)
                t = TEMPLATES[key]
                await message.reply(
                    f"{t['emoji']} テンプレート `{t['label']}` を有効にしました。\n"
                    f"このテンプレートに沿って応答します。解除は `/template clear`\n"
                    f"{get_status_line(channel_id)}"
                )
        return

    # /branch コマンド
    branch_match = re.match(r'^[/!]branch(?:\s+(\S+)(?:\s+(.+))?)?$', stripped, re.IGNORECASE)
    if branch_match:
        sub = (branch_match.group(1) or "").lower()
        arg = (branch_match.group(2) or "").strip()
        if not sub or sub == "list":
            branches = list_branches(channel_id)
            if not branches:
                await message.reply(
                    "🌳 保存されたブランチはありません。\n"
                    "使い方:\n"
                    "  `/branch save <名前>` - 現在の会話を保存\n"
                    "  `/branch list` - 一覧\n"
                    "  `/branch load <名前>` - 復元\n"
                    "  `/branch delete <名前>` - 削除"
                )
            else:
                lines = "\n".join(
                    f"  • `{label}` — {Path(wd).name} / {model} / {created_at[:16]}"
                    for label, wd, model, persona, created_at in branches
                )
                await message.reply(
                    f"🌳 保存済みブランチ ({len(branches)}件):\n{lines}\n\n"
                    f"復元: `/branch load <名前>`"
                )
        elif sub == "save":
            if not arg:
                await message.reply("❌ 保存名を指定してください: `/branch save <名前>`")
            elif save_branch(channel_id, arg):
                await message.reply(
                    f"🌳 ブランチ `{arg}` を保存しました。\n"
                    f"後で `/branch load {arg}` で復元できます。"
                )
            else:
                await message.reply("❌ 保存できませんでした（現在進行中のセッションが必要です）")
        elif sub == "load":
            if not arg:
                await message.reply("❌ 復元するブランチ名を指定してください")
            elif load_branch(channel_id, arg):
                await message.reply(
                    f"🌳 ブランチ `{arg}` を復元しました。\n"
                    f"{get_status_line(channel_id)}"
                )
                await update_channel_topic(message.channel, channel_id)
            else:
                await message.reply(f"❌ ブランチ `{arg}` が見つかりません。`/branch list` で確認")
        elif sub == "delete":
            if not arg:
                await message.reply("❌ 削除するブランチ名を指定してください")
            elif delete_branch(channel_id, arg):
                await message.reply(f"🗑️ ブランチ `{arg}` を削除しました。")
            else:
                await message.reply(f"❌ ブランチ `{arg}` が見つかりません。")
        else:
            await message.reply(
                "使い方:\n"
                "  `/branch save <名前>` - 現在の会話を保存\n"
                "  `/branch list` - 一覧\n"
                "  `/branch load <名前>` - 復元\n"
                "  `/branch delete <名前>` - 削除"
            )
        return

    # /project コマンド（例: /project oracle, /project）
    project_match = re.match(r'^[/!]project(?:\s+(.+))?$', content.strip(), re.IGNORECASE)
    if project_match:
        requested = (project_match.group(1) or "").strip()
        projects = get_projects()
        if not requested:
            # プロジェクト一覧を表示
            row = get_session(channel_id)
            current_dir = Path(row[1]).name if row and row[1] else WORK_DIR.name
            if not projects:
                await message.reply("📁 プロジェクトフォルダが見つかりません。")
            else:
                proj_list = "\n".join(f"  `{p}`" for p in projects)
                await message.reply(
                    f"📁 現在のプロジェクト: `{current_dir}`\n\n"
                    f"利用可能なプロジェクト:\n{proj_list}\n\n"
                    f"切り替え: `/project フォルダ名` または「2番のプロジェクトに移って」"
                )
        else:
            # 番号指定（1始まり）
            if requested.isdigit():
                idx = int(requested) - 1
                if 0 <= idx < len(projects):
                    matched = projects[idx]
                else:
                    await message.reply(f"❌ {requested}番のプロジェクトは存在しません。`/project` で一覧を確認してください。")
                    return
            else:
                # 名前で前方一致 or 完全一致
                req_lower = requested.lower()
                matched = next(
                    (p for p in projects if p.lower() == req_lower or p.lower().startswith(req_lower)),
                    None
                )
                if not matched:
                    await message.reply(
                        f"❌ `{requested}` に一致するプロジェクトが見つかりません。\n"
                        f"`/project` で一覧を確認してください。"
                    )
                    return
            new_path = str(WORK_DIR / matched)
            auto_label = save_work_dir(channel_id, new_path)
            await update_channel_topic(message.channel, channel_id)
            recover_msg = (
                f"\n💾 前の会話を `{auto_label}` として保存しました。"
                f"戻すには `/branch load {auto_label}`"
            ) if auto_label else ""
            await message.reply(
                f"📁 `{matched}` に移動しました。（会話履歴はリセット）"
                f"{recover_msg}\n"
                f"{get_status_line(channel_id)}"
            )
        return

    # /model コマンド（例: /model opus, /model sonnet, /model）
    model_match = re.match(r'^[/!]model(?:\s+(\S+))?$', content.strip(), re.IGNORECASE)
    if model_match:
        requested = model_match.group(1)
        if not requested:
            # 現在のモデルを表示
            row = get_session(channel_id)
            current = row[2] if row and row[2] else DEFAULT_MODEL
            emoji = MODEL_EMOJI.get(current, "🤖")
            model_list = "\n".join([
                "  `sonnet` 🔵（デフォルト・高速）← CLIが自動で最新版を選択",
                "  `opus`   🟣（最高性能・低速）",
                "  `haiku`  🟢（軽量・超高速）",
            ])
            await message.reply(
                f"{emoji} 現在のモデル: `{current}`\n\n"
                f"切り替え方法:\n{model_list}\n"
                f"例: `/model opus` または `opusに切り替えて`"
            )
        else:
            new_model = MODEL_ALIASES.get(requested.lower())
            if not new_model:
                await message.reply(
                    f"❌ 不明なモデル: `{requested}`\n"
                    f"使用可能: `sonnet`, `opus`, `haiku`"
                )
            else:
                save_model(channel_id, new_model)
                await update_channel_topic(message.channel, channel_id)
                emoji = MODEL_EMOJI.get(new_model, "🤖")
                await message.reply(
                    f"{emoji} モデルを `{new_model}` に切り替えました。\n"
                    f"（既存のセッション・会話履歴は維持されます）\n"
                    f"{get_status_line(channel_id)}"
                )
        return

    # ① 正規表現でモデル切り替え検出（高速・無料）
    nl_model = detect_model_switch(content.strip())

    # ② 正規表現で引っかからず、モデル関連ワードが含まれる → Gemini Flashで判定
    # 自然言語 permission mode 切替検出
    nl_perm = detect_permission_mode_switch(content)
    if nl_perm:
        set_permission_mode(str(message.channel.id), nl_perm)
        v = PERMISSION_MODES[nl_perm]
        await message.reply(
            f"🔐 権限モードを {v["emoji"]} **{v["label"]}** (`{nl_perm}`) に変更しました。\n"
            f"{v["desc"]}"
        )
        return

    if not nl_model and _MODEL_HINT_RE.search(content):
        nl_model = await detect_model_switch_ai(content.strip())

    if nl_model:
        save_model(channel_id, nl_model)
        await update_channel_topic(message.channel, channel_id)
        emoji = MODEL_EMOJI.get(nl_model, "🤖")
        await message.reply(
            f"{emoji} モデルを `{nl_model}` に切り替えました。\n"
            f"（既存のセッション・会話履歴は維持されます）\n"
            f"{get_status_line(channel_id)}"
        )
        return

    # ③ プロジェクト切り替えヒントがあれば Gemini で検出
    if _PROJECT_HINT_RE.search(content):
        projects = get_projects()
        matched_proj = await detect_project_switch_ai(content.strip(), projects)
        if matched_proj:
            new_path = str(WORK_DIR / matched_proj)
            auto_label = save_work_dir(channel_id, new_path)
            await update_channel_topic(message.channel, channel_id)
            recover_msg = (
                f"\n💾 前の会話を `{auto_label}` として保存しました。"
                f"戻すには `/branch load {auto_label}`"
            ) if auto_label else ""
            await message.reply(
                f"📁 `{matched_proj}` に移動しました。（会話履歴はリセット）"
                f"{recover_msg}\n"
                f"{get_status_line(channel_id)}"
            )
            return

    # 添付ファイル取得（画像/PDF/音声/Office対応）
    attachment_texts = []
    for att in message.attachments:
        txt = await download_attachment(att, channel_id)
        if txt:
            attachment_texts.append(txt)

    # 現在のモデル・ペルソナ・テンプレートを取得
    row = get_session(channel_id)
    current_model    = row[2] if row and row[2] else DEFAULT_MODEL
    current_persona  = row[3] if row and len(row) > 3 and row[3] else "default"
    current_template = row[4] if row and len(row) > 4 and row[4] else None

    async with semaphore:
        async with message.channel.typing():
            try:
                # ストリーミング進捗表示版（メッセージ送信もrun_claude内で完結）
                await run_claude(
                    content, channel_id, attachment_texts,
                    current_model, current_persona, message,
                    template=current_template,
                )
            except asyncio.TimeoutError:
                await message.reply("⏱️ タイムアウト（5分）しました。")
            except Exception as e:
                await message.reply(f"⚠️ エラー: {e}")

# ── ローカル音声文字起こし（whisper.cpp） ─────────────
WHISPER_BIN = Path("/home/ubuntu/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODELS_DIR = Path("/home/ubuntu/whisper.cpp/models")

async def _transcribe_local_whisper(audio_path: str, model: str = "large-v3-turbo") -> str | None:
    """whisper.cpp で音声を文字起こし（ローカル・無制限・無料）"""
    model_file = WHISPER_MODELS_DIR / f"ggml-{model}.bin"
    if not model_file.exists():
        # フォールバックとして base を試す
        alt = WHISPER_MODELS_DIR / "ggml-base.bin"
        if alt.exists():
            model_file = alt
        else:
            return f"⚠️ Whisperモデル {model} が未ダウンロードです。"
    if not WHISPER_BIN.exists():
        return "⚠️ whisper-cli バイナリが見つかりません。"

    # 入力が wav 以外なら ffmpeg で変換
    audio_path_obj = Path(audio_path)
    work_path = audio_path
    if audio_path_obj.suffix.lower() not in (".wav",):
        # ffmpeg で 16kHz mono wav に変換
        wav_path = Path(tempfile.mkdtemp()) / "input.wav"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(audio_path),
            "-ar", "16000", "-ac", "1", str(wav_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0 or not wav_path.exists():
            return "⚠️ ffmpeg 変換失敗"
        work_path = str(wav_path)

    # whisper-cli 実行
    proc = await asyncio.create_subprocess_exec(
        str(WHISPER_BIN),
        "-m", str(model_file),
        "-f", work_path,
        "-l", "auto",
        "-t", "4",  # スレッド数
        "--no-timestamps",
        "--output-txt",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        return "⚠️ タイムアウト（10分）"
    output = stdout.decode(errors="replace").strip()
    if not output and stderr:
        err = stderr.decode(errors="replace")[-500:]
        return f"⚠️ Whisperエラー:\n```\n{err}\n```"
    return output

# ── 画像生成 ───────────────────────────────────────
async def generate_image_pollinations(prompt: str, model: str = "flux", width: int = 1024, height: int = 1024) -> bytes | None:
    """Pollinations.ai で画像生成（APIキー不要・完全無料・無制限）"""
    import urllib.parse as _up
    encoded = _up.quote(prompt)
    # model: flux / flux-realism / flux-anime / flux-3d / turbo / any
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&model={model}&nologo=true&enhance=true"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data and len(data) > 1000:  # 最低限の画像サイズ
                        return data
                return None
    except Exception as e:
        print(f"[image gen] pollinations error: {e}")
        return None

POLLINATIONS_MODELS = {
    "flux":          "🎨 FLUX（標準・高品質・万能）",
    "flux-realism":  "📷 FLUX Realism（写実的）",
    "flux-anime":    "🎭 FLUX Anime（アニメ風）",
    "flux-3d":       "🎲 FLUX 3D（3Dレンダ風）",
    "turbo":         "⚡ Turbo（高速・標準品質）",
}

CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN  = os.environ.get("CLOUDFLARE_API_TOKEN", "")

async def generate_image_nano_banana(prompt: str, aspect_ratio: str = "1:1",
                                       version: str = "v2") -> bytes | None:
    """Gemini Nano Banana (画像生成)
    version:
      v1  : gemini-2.5-flash-image            ($0.039/枚)
      v2  : gemini-3.1-flash-image-preview    ($0.045/枚・日本語◎・標準デフォルト)
      pro : gemini-3-pro-image-preview        ($0.134/枚・日本語◎◎・4K対応・最高品質)
    """
    if not GEMINI_KEY_IMAGE:
        return None
    model_map = {
        "v1":  "gemini-2.5-flash-image",
        "v2":  "gemini-3.1-flash-image-preview",
        "pro": "gemini-3-pro-image-preview",
    }
    model = model_map.get(version, "gemini-3.1-flash-image-preview")
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/{model}:generateContent?key={GEMINI_KEY_IMAGE}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio, "imageSize": "1K"},
        },
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                    inline = part.get("inline_data") or part.get("inlineData")
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"])
    except Exception as e:
        print(f"[image gen] nano-banana error: {e}")
    return None

async def generate_image_cloudflare(prompt: str, model: str = "@cf/black-forest-labs/flux-1-schnell") -> bytes | None:
    """Cloudflare Workers AI で画像生成（10000 Neurons/日 無料）"""
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
    payload = {"prompt": prompt, "steps": 4}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    img_b64 = data.get("result", {}).get("image")
                    if img_b64:
                        return base64.b64decode(img_b64)
                return None
    except Exception as e:
        print(f"[image gen] cloudflare error: {e}")
        return None

async def _translate_prompt_to_en(prompt_jp: str) -> str | None:
    """画像生成用に日本語プロンプトを英語に翻訳（Geminiで・任意）"""
    if not GEMINI_KEY:
        return None
    if all(ord(c) < 128 for c in prompt_jp):
        return prompt_jp  # 既に英語
    payload_text = (
        "Translate the following Japanese image generation prompt to natural, "
        "detailed English suitable for FLUX/SD. Keep proper nouns. "
        "Add quality boosters like 'highly detailed, professional, 8k' if appropriate. "
        "Return ONLY the English prompt, no explanation.\n\n"
        f"Prompt: {prompt_jp}"
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY_TEXT}"
    )
    payload = {
        "contents": [{"parts": [{"text": payload_text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 256},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        pass
    return None

# ── Slash Commands（ネイティブ化） ────────────────
@bot.tree.command(name="clear", description="会話セッションをリセットします")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_clear(interaction: discord.Interaction):
    cid = str(interaction.channel_id)
    delete_session(cid)
    await interaction.response.send_message(
        f"🗑️ セッションをリセットしました。\n{get_status_line(cid)}"
    )

@bot.tree.command(name="model", description="使用するClaudeモデルを切り替えます")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(model="使うモデル")
@app_commands.choices(model=[
    app_commands.Choice(name="🔵 Sonnet（デフォルト）", value="sonnet"),
    app_commands.Choice(name="🟣 Opus（高性能）", value="opus"),
    app_commands.Choice(name="🟢 Haiku（軽量・高速）", value="haiku"),
])
async def slash_model(interaction: discord.Interaction, model: app_commands.Choice[str]):
    cid = str(interaction.channel_id)
    save_model(cid, model.value)
    emoji = MODEL_EMOJI.get(model.value, "🤖")
    await interaction.response.send_message(
        f"{emoji} モデルを `{model.value}` に切り替えました。\n{get_status_line(cid)}"
    )

async def _project_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p, value=p)
        for p in get_projects()
        if current.lower() in p.lower()
    ][:25]

@bot.tree.command(name="project", description="作業プロジェクト（フォルダ）を切り替えます")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(folder="プロジェクトフォルダ名")
@app_commands.autocomplete(folder=_project_autocomplete)
async def slash_project(interaction: discord.Interaction, folder: str):
    cid = str(interaction.channel_id)
    if folder not in get_projects():
        await interaction.response.send_message(
            f"❌ フォルダ `{folder}` が見つかりません。", ephemeral=True
        )
        return
    save_work_dir(cid, str(WORK_DIR / folder))
    await interaction.response.send_message(
        f"📁 `{folder}` に移動しました。（会話履歴はリセット）\n{get_status_line(cid)}"
    )

@bot.tree.command(name="persona", description="Botのキャラクター（口調）を切り替えます")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(persona="キャラクター")
@app_commands.choices(persona=[
    app_commands.Choice(name=f"{p['emoji']} {p['label']}", value=key)
    for key, p in PERSONAS.items()
])
async def slash_persona(interaction: discord.Interaction, persona: app_commands.Choice[str]):
    cid = str(interaction.channel_id)
    save_persona(cid, persona.value)
    p = PERSONAS[persona.value]
    await interaction.response.send_message(
        f"{p['emoji']} キャラクターを `{p['label']}` に切り替えました。\n{get_status_line(cid)}"
    )

@bot.tree.command(name="template", description="応答テンプレート（議事録モード等）を切り替え")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(template="テンプレート")
@app_commands.choices(template=[
    app_commands.Choice(name=f"{t['emoji']} {t['label']}", value=key)
    for key, t in TEMPLATES.items()
] + [app_commands.Choice(name="❌ 解除", value="__clear__")])
async def slash_template(interaction: discord.Interaction, template: app_commands.Choice[str]):
    cid = str(interaction.channel_id)
    if template.value == "__clear__":
        save_template(cid, None)
        await interaction.response.send_message(
            f"📋 テンプレートを解除しました。\n{get_status_line(cid)}"
        )
    else:
        save_template(cid, template.value)
        t = TEMPLATES[template.value]
        await interaction.response.send_message(
            f"{t['emoji']} テンプレート `{t['label']}` を有効にしました。\n{get_status_line(cid)}"
        )

@bot.tree.command(name="status", description="現在のモデル・プロジェクト・キャラ・テンプレを確認")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_status(interaction: discord.Interaction):
    cid = str(interaction.channel_id)
    await interaction.response.send_message(get_status_line(cid))

@bot.tree.command(name="schedule_add", description="スケジュールを自然言語で登録")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(when_what="例: 毎朝7時に天気を教えて")
async def slash_schedule_add(interaction: discord.Interaction, when_what: str):
    cid = str(interaction.channel_id)
    await interaction.response.defer()
    parsed = await parse_schedule_nl(when_what)
    if not parsed:
        await interaction.followup.send("❌ スケジュールを解釈できませんでした。")
        return
    cron, label, sched_prompt = parsed
    row = get_session(cid)
    cur_wd      = row[1] if row and row[1] else str(WORK_DIR)
    cur_model   = row[2] if row and row[2] else DEFAULT_MODEL
    cur_persona = row[3] if row and len(row) > 3 and row[3] else "default"
    tid = add_scheduled_task(cid, cron, label, sched_prompt, cur_wd, cur_model, cur_persona)
    await interaction.followup.send(
        f"⏰ 登録 `#{tid}`: **{label}** (`{cron}`)\n→ {sched_prompt}"
    )

@bot.tree.command(name="schedule_list", description="登録済みスケジュール一覧")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_schedule_list(interaction: discord.Interaction):
    cid = str(interaction.channel_id)
    tasks = list_scheduled_tasks(cid)
    if not tasks:
        await interaction.response.send_message("⏰ スケジュールはありません。", ephemeral=True)
        return
    lines = [
        f"  {'▶️' if t[10] else '⏸️'} `#{t[0]}` `{t[2]}` — **{t[3]}**"
        for t in tasks
    ]
    await interaction.response.send_message("⏰ 登録済み:\n" + "\n".join(lines))

@bot.tree.command(name="report", description="今日のデイリーレポートを今すぐ生成")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_report(interaction: discord.Interaction):
    cid = str(interaction.channel_id)
    await interaction.response.send_message("📊 レポート生成中…", ephemeral=True)
    await _generate_daily_report(interaction.channel, cid, immediate=True)

@bot.tree.command(name="image", description="無料AI画像生成（Pollinations / Nano Banana / Cloudflare）")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(
    prompt="生成したい画像の説明（日本語OK・自動英訳）",
    provider="プロバイダ選択",
    style="Pollinationsスタイル（provider=pollinations時のみ）",
    size="画像サイズ",
)
@app_commands.choices(provider=[
    app_commands.Choice(name="🍌2 Nano Banana 2 Flash (デフォルト・$0.045/枚)",        value="nano-banana-2"),
    app_commands.Choice(name="🍌👑 Nano Banana 2 PRO (日本語・4K最高品質・$0.134/枚)", value="nano-banana-2-pro"),
    app_commands.Choice(name="🍌 Nano Banana v1 ($0.039/枚)",                          value="nano-banana"),
    app_commands.Choice(name="🎨 Pollinations.ai (FLUX・無料無制限)",                  value="pollinations"),
    app_commands.Choice(name="☁️ Cloudflare (FLUX-schnell・10kN/日)",                  value="cloudflare"),
])
@app_commands.choices(style=[
    app_commands.Choice(name=v, value=k) for k, v in POLLINATIONS_MODELS.items()
])
@app_commands.choices(size=[
    app_commands.Choice(name="1024×1024 正方形", value="1024x1024"),
    app_commands.Choice(name="1280×720 横長(16:9)", value="1280x720"),
    app_commands.Choice(name="720×1280 縦長(9:16)", value="720x1280"),
    app_commands.Choice(name="1920×1080 大画面FHD", value="1920x1080"),
])
async def slash_image(
    interaction: discord.Interaction,
    prompt: str,
    provider: app_commands.Choice[str] | None = None,
    style: app_commands.Choice[str] | None = None,
    size: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer()
    prov = (provider.value if provider else "nano-banana-2")  # デフォルトはv2
    model = (style.value if style else "flux")
    w, h = (size.value.split("x") if size else ("1024", "1024"))
    width, height = int(w), int(h)
    # 日本語→英語に翻訳（あれば）
    en_prompt = await _translate_prompt_to_en(prompt) or prompt

    # アスペクト比計算（Nano Banana用）
    from math import gcd
    g = gcd(width, height)
    rw, rh = width // g, height // g
    aspect_map = {(1,1):"1:1",(16,9):"16:9",(9,16):"9:16",(4,3):"4:3",(3,4):"3:4"}
    aspect = aspect_map.get((rw, rh), "1:1")

    img_bytes = None
    used_provider = ""
    if prov == "nano-banana":
        img_bytes = await generate_image_nano_banana(en_prompt, aspect_ratio=aspect, version="v1")
        used_provider = "🍌 Nano Banana v1 (Gemini 2.5 Flash Image)"
    elif prov == "nano-banana-2":
        img_bytes = await generate_image_nano_banana(en_prompt, aspect_ratio=aspect, version="v2")
        used_provider = "🍌2 Nano Banana 2 Flash (Gemini 3.1)"
    elif prov == "nano-banana-2-pro":
        img_bytes = await generate_image_nano_banana(en_prompt, aspect_ratio=aspect, version="pro")
        used_provider = "🍌👑 Nano Banana 2 PRO (Gemini 3 Pro Image)"
    elif prov == "cloudflare":
        img_bytes = await generate_image_cloudflare(en_prompt)
        used_provider = "☁️ Cloudflare FLUX-schnell"
    else:
        img_bytes = await generate_image_pollinations(en_prompt, model=model, width=width, height=height)
        used_provider = f"🎨 Pollinations {model}"
    # フォールバック
    if not img_bytes and prov != "pollinations":
        img_bytes = await generate_image_pollinations(en_prompt, model="flux", width=width, height=height)
        if img_bytes:
            used_provider += " → Pollinations fallback"
    if not img_bytes:
        await interaction.followup.send("⚠️ 画像生成に失敗しました。")
        return
    # 一時保存して送信
    import io
    file = discord.File(io.BytesIO(img_bytes), filename=f"gen_{int(time.time())}.png")
    info = (
        f"**{used_provider}** / {width}×{height}\n"
        f"📝 `{prompt[:200]}`"
    )
    if en_prompt != prompt:
        info += f"\n🌐 `{en_prompt[:200]}`"
    await interaction.followup.send(content=info, file=file)

async def _download_audio_from_url(url: str, max_size_mb: int = 2048) -> Path | None:
    """URLから音声ファイルをVPSローカルにDL（最大2GBまで）"""
    # Dropbox 共有リンク変換
    if "dropbox.com" in url and "?dl=0" in url:
        url = url.replace("?dl=0", "?dl=1")
    elif "dropbox.com" in url and "?dl=" not in url:
        url = url + ("&dl=1" if "?" in url else "?dl=1")
    # Google Drive 共有リンク変換
    if "drive.google.com" in url and "/file/d/" in url:
        try:
            file_id = url.split("/file/d/")[1].split("/")[0]
            url = f"https://drive.google.com/uc?export=download&id={file_id}"
        except Exception:
            pass

    # ファイル名推測
    parsed = urllib.parse.urlparse(url)
    name_hint = Path(parsed.path).name or f"audio_{int(time.time())}"
    if "." not in name_hint:
        name_hint += ".bin"
    safe_name = re.sub(r'[^\w.\-]', '_', name_hint)[:80]
    out_dir = ATTACHMENT_DIR / "url_downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{int(time.time())}_{safe_name}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    return None
                # サイズチェック
                cl = resp.headers.get("Content-Length")
                if cl and int(cl) > max_size_mb * 1024 * 1024:
                    return None
                # ストリーミング書き込み
                with open(out_path, "wb") as f:
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        f.write(chunk)
                        total += len(chunk)
                        if total > max_size_mb * 1024 * 1024:
                            f.close()
                            out_path.unlink(missing_ok=True)
                            return None
        return out_path
    except Exception as e:
        print(f"[audio download] error: {e}")
        return None

# ── ファイルブラウザ ───────────────────────────────
def _format_file_size(size: int) -> str:
    if size < 1024: return f"{size}B"
    if size < 1024 * 1024: return f"{size//1024}KB"
    return f"{size//1024//1024}MB"

def _file_emoji(path: Path) -> str:
    """ファイル種類別の絵文字"""
    if path.is_dir(): return "📁"
    ext = path.suffix.lower()
    if ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h"): return "📜"
    if ext in (".md", ".txt", ".rst"): return "📝"
    if ext in (".json", ".yaml", ".yml", ".toml", ".ini", ".env"): return "⚙️"
    if ext in (".html", ".css", ".scss"): return "🌐"
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"): return "🖼️"
    if ext in (".mp3", ".m4a", ".wav", ".ogg", ".flac"): return "🎤"
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"): return "🎬"
    if ext in (".pdf"): return "📄"
    if ext in (".xlsx", ".xls", ".csv"): return "📊"
    if ext in (".docx", ".doc"): return "📃"
    if ext in (".pptx", ".ppt"): return "📑"
    if ext in (".zip", ".tar", ".gz", ".7z"): return "📦"
    if ext in (".sh", ".bash", ".zsh"): return "🖥️"
    return "📄"

def _resolve_path(channel_id: str, subpath: str) -> Path:
    """セッションのwork_dir基準で相対パスを絶対パスに解決"""
    row = get_session(channel_id)
    base = Path(row[1]) if row and row[1] else WORK_DIR
    if not subpath or subpath == ".":
        return base
    if subpath.startswith("/"):
        return Path(subpath)
    # 安全のため WORK_DIR配下かチェック（簡易的に）
    return (base / subpath).resolve()

async def _file_autocomplete(interaction: discord.Interaction, current: str):
    """ファイルパスのオートコンプリート（最大25件）"""
    cid = str(interaction.channel_id)
    base = _resolve_path(cid, "")
    matches = []
    try:
        # current 入力からディレクトリを推定
        if "/" in current:
            sub_dir = "/".join(current.split("/")[:-1])
            search_dir = base / sub_dir if sub_dir else base
            prefix_in_dir = current.split("/")[-1].lower()
            display_prefix = sub_dir + "/" if sub_dir else ""
        else:
            search_dir = base
            prefix_in_dir = current.lower()
            display_prefix = ""
        if search_dir.is_dir():
            for entry in sorted(search_dir.iterdir()):
                if entry.name.startswith(".") and not prefix_in_dir.startswith("."):
                    continue
                if prefix_in_dir and not entry.name.lower().startswith(prefix_in_dir):
                    continue
                full = display_prefix + entry.name + ("/" if entry.is_dir() else "")
                emoji = _file_emoji(entry)
                matches.append(app_commands.Choice(name=f"{emoji} {full}"[:100], value=full[:100]))
                if len(matches) >= 25:
                    break
    except Exception:
        pass
    return matches

@bot.tree.command(name="files", description="現在のプロジェクト内のファイル一覧を表示")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(subpath="サブフォルダのパス（省略時はカレント）")
@app_commands.autocomplete(subpath=_file_autocomplete)
async def slash_files(interaction: discord.Interaction, subpath: str = ""):
    cid = str(interaction.channel_id)
    target = _resolve_path(cid, subpath)
    if not target.exists():
        await interaction.response.send_message(f"❌ パスが見つかりません: `{target}`", ephemeral=True)
        return
    if not target.is_dir():
        await interaction.response.send_message(
            f"📄 これはファイルです: `{target}` ({_format_file_size(target.stat().st_size)})\n"
            f"内容を見るには `/file {subpath}` を使ってください。", ephemeral=True
        )
        return
    try:
        entries = sorted(
            target.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower())  # ディレクトリ先
        )
    except PermissionError:
        await interaction.response.send_message(f"❌ 読み取り権限がありません: `{target}`", ephemeral=True)
        return
    # 隠しファイルは末尾に
    visible = [e for e in entries if not e.name.startswith(".")]
    hidden = [e for e in entries if e.name.startswith(".")]
    all_entries = visible + hidden

    rel_to_base = target.relative_to(_resolve_path(cid, "")) if target != _resolve_path(cid, "") else Path(".")
    header = f"📂 **`{rel_to_base}/`** ({len(all_entries)}件)\n"
    lines = []
    for e in all_entries[:30]:  # 最大30件表示
        emoji = _file_emoji(e)
        try:
            if e.is_dir():
                child_count = len(list(e.iterdir())) if e.is_dir() else 0
                lines.append(f"{emoji} `{e.name}/` ({child_count}件)")
            else:
                size = _format_file_size(e.stat().st_size)
                lines.append(f"{emoji} `{e.name}` ({size})")
        except Exception:
            lines.append(f"{emoji} `{e.name}`")
    if len(all_entries) > 30:
        lines.append(f"… (他 {len(all_entries) - 30} 件)")
    body = header + "\n".join(lines)
    if len(body) > 1900:
        body = body[:1900] + "\n…"
    body += f"\n\n📖 中身を見る: `/file <path>`  |  📁 移動: `/files <subpath>`"
    await interaction.response.send_message(body)

@bot.tree.command(name="file", description="ファイルの内容を表示（プレビュー）")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(path="ファイルパス（自動補完あり）")
@app_commands.autocomplete(path=_file_autocomplete)
async def slash_file(interaction: discord.Interaction, path: str):
    cid = str(interaction.channel_id)
    target = _resolve_path(cid, path.rstrip("/"))
    if not target.exists():
        await interaction.response.send_message(f"❌ ファイルが見つかりません: `{target}`", ephemeral=True)
        return
    if target.is_dir():
        # ディレクトリだったら /files にリダイレクト
        await interaction.response.defer()
        # 簡易的に内部呼び出し
        return await slash_files.callback(interaction, path)
    size = target.stat().st_size
    if size > 200_000:
        await interaction.response.send_message(
            f"⚠️ ファイルが大きすぎます ({_format_file_size(size)})。先頭2000行のみ表示するか、Claudeに頼んでください。",
            ephemeral=True
        )
        return
    # バイナリ判定
    try:
        with open(target, "rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            # バイナリ（画像など）
            if target.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                await interaction.response.send_message(
                    content=f"🖼️ `{target.name}` ({_format_file_size(size)})",
                    file=discord.File(str(target))
                )
                return
            await interaction.response.send_message(
                f"⚠️ バイナリファイル `{target.name}` ({_format_file_size(size)}) は表示できません。",
                ephemeral=True
            )
            return
    except Exception:
        pass
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        await interaction.response.send_message(f"❌ 読み取り失敗: {e}", ephemeral=True)
        return
    # 言語ヒント
    lang_map = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
                ".html": "html", ".css": "css", ".json": "json", ".md": "markdown",
                ".sh": "bash", ".yaml": "yaml", ".yml": "yaml", ".rb": "ruby",
                ".go": "go", ".rs": "rust", ".sql": "sql", ".env": "ini"}
    lang = lang_map.get(target.suffix.lower(), "")
    header = f"📄 **`{path}`** ({_format_file_size(size)})\n```{lang}\n"
    footer = "\n```"
    max_body = 1900 - len(header) - len(footer) - 30
    if len(text) > max_body:
        text = text[:max_body] + "\n…(省略)"
    await interaction.response.send_message(f"{header}{text}{footer}")

@bot.tree.command(name="audio", description="音声ファイルをWhisperで文字起こし（パス or URL）")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(
    source="ファイルパス または URL（Dropbox/Drive/iCloud共有リンク等）",
    model="Whisperモデル",
)
@app_commands.choices(model=[
    app_commands.Choice(name="🟢 large-v3-turbo（推奨・速・高精度）", value="large-v3-turbo"),
    app_commands.Choice(name="🔵 base（軽量・高速）", value="base"),
])
async def slash_audio(interaction: discord.Interaction, source: str, model: app_commands.Choice[str] | None = None):
    cid = str(interaction.channel_id)
    await interaction.response.defer()
    model_name = model.value if model else "large-v3-turbo"
    source = source.strip()

    # URL or パス判定
    if source.startswith(("http://", "https://")):
        await interaction.followup.send(f"⬇️ ダウンロード中: `{source[:80]}`")
        downloaded = await _download_audio_from_url(source)
        if not downloaded:
            await interaction.followup.send("❌ URLからのダウンロードに失敗しました（最大2GBまで）")
            return
        file_path = downloaded
    else:
        # ローカルパス
        file_path = Path(source)
        if not file_path.is_absolute():
            row = get_session(cid)
            base = Path(row[1]) if row and row[1] else WORK_DIR
            file_path = base / source
        if not file_path.exists():
            await interaction.followup.send(
                f"❌ ファイルが見つかりません: `{file_path}`\n"
                f"Mac側で `~/dev/{source}` に保存してSyncthing同期を待つか、URL指定で実行してください。"
            )
            return

    await interaction.followup.send(
        f"🎤 文字起こし開始 — `{file_path.name}` ({file_path.stat().st_size//1024}KB)"
    )
    transcript = await _transcribe_local_whisper(str(file_path), model_name)
    if not transcript:
        await interaction.followup.send("⚠️ 文字起こしに失敗しました。")
        return
    # 結果送信（長ければ分割）
    header = f"🎤 **文字起こし完了** — `{file_path.name}` (whisper-{model_name})\n\n"
    chunks = split_message(header + transcript)
    await interaction.followup.send(chunks[0])
    for chunk in chunks[1:]:
        await interaction.channel.send(chunk)

# ── Claude Code セッション引き継ぎ ──────────────────────────────

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
_MAC_PATH_PREFIX = "/Users/nk/"
_VPS_PATH_PREFIX = str(Path.home()) + "/"

def _mac_to_vps_path(mac_path: str) -> str:
    if mac_path.startswith(_MAC_PATH_PREFIX):
        return _VPS_PATH_PREFIX + mac_path[len(_MAC_PATH_PREFIX):]
    return mac_path

def _vps_cwd_to_mac_cwd(vps_cwd: str) -> str:
    """VPS の cwd を Mac の同等パスに変換 (bind mount workspaces → .vscode 含む)"""
    mac = vps_cwd
    if mac.startswith("/home/ubuntu/"):
        mac = "/Users/nk/" + mac[len("/home/ubuntu/"):]
    # bind mount: workspaces → .vscode
    mac = mac.replace("/dev/vscode-mcp/workspaces/", "/dev/vscode-mcp/.vscode/", 1)
    if mac.endswith("/dev/vscode-mcp/workspaces"):
        mac = mac[:-len("workspaces")] + ".vscode"
    return mac

def _encode_claude_path(p: str) -> str:
    """Claude Code流: スラッシュをダッシュに"""
    return p.replace("/", "-")

def _mirror_session_to_mac(session_id: str, vps_work_dir: str) -> bool:
    """VPS-encoded dir のセッションjsonlを Mac-encoded dir にも hardlink。
    Mac の Claude Code UI 側で時系列表示されるように。"""
    import os
    if not session_id or not vps_work_dir:
        return False
    mac_work_dir = _vps_cwd_to_mac_cwd(vps_work_dir)
    if mac_work_dir == vps_work_dir:
        return False  # 変換不要 = mirror 不要

    projects = os.path.expanduser("~/.claude/projects")
    vps_enc = _encode_claude_path(vps_work_dir)
    mac_enc = _encode_claude_path(mac_work_dir)

    src = os.path.join(projects, vps_enc, f"{session_id}.jsonl")
    dst_dir = os.path.join(projects, mac_enc)
    dst = os.path.join(dst_dir, f"{session_id}.jsonl")

    if not os.path.exists(src):
        return False
    if os.path.exists(dst) or os.path.islink(dst):
        return True
    try:
        os.makedirs(dst_dir, exist_ok=True)
        os.link(src, dst)
        return True
    except OSError:
        return False

def _bulk_mirror_vps_to_mac() -> int:
    """既存の全 VPS-encoded セッション JSONL を Mac-encoded dir にも hardlink"""
    import os
    projects = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects):
        return 0
    mirrored = 0
    for entry in os.listdir(projects):
        if not entry.startswith("-home-ubuntu"):
            continue
        full = os.path.join(projects, entry)
        if not os.path.isdir(full) or os.path.islink(full):
            continue
        # entry → vps_path 復元 (-home-ubuntu-... → /home/ubuntu/...)
        # ただし Claude Code のエンコードは曖昧 (連続dashが混じると不可逆)。
        # 単純に -home-ubuntu を /home/ubuntu に戻す + 残り dash を / 想定で復元 (限界あり)
        # 安全策: vps_path を / に戻して mac dir 名を計算しなおすのは難しいので、entry 自体を文字列置換する
        mac_entry = "-Users-nk" + entry[len("-home-ubuntu"):]
        # bind mount: workspaces → .vscode (encoded: -workspaces- → --vscode-)
        if "-workspaces-" in mac_entry:
            mac_entry = mac_entry.replace("-workspaces-", "--vscode-", 1)
        elif mac_entry.endswith("-workspaces"):
            mac_entry = mac_entry[:-len("-workspaces")] + "--vscode"
        mac_full = os.path.join(projects, mac_entry)
        os.makedirs(mac_full, exist_ok=True)
        for f in os.listdir(full):
            if not f.endswith(".jsonl"):
                continue
            src = os.path.join(full, f)
            dst = os.path.join(mac_full, f)
            if os.path.exists(dst) or os.path.islink(dst):
                continue
            if os.path.isdir(src):
                continue
            try:
                os.link(src, dst)
                mirrored += 1
            except OSError:
                pass
    return mirrored

def _ensure_vps_session_jsonl(session_id: str, mac_cwd: str) -> bool:
    """Mac側で作成された session jsonl を VPS-encoded project dir に hardlink。
    Claude Code --resume が VPS の cwd encoded dir からセッションを探すため必要。
    既にある場合はスキップ。
    """
    import os
    if not mac_cwd or not mac_cwd.startswith(_MAC_PATH_PREFIX):
        return True
    projects_dir = os.path.expanduser("~/.claude/projects")

    # cwdからエンコードされたdir名を計算（slashes → dashes、単純化）
    def encode_path(p: str) -> str:
        # Claude Code の流儀: 先頭/もダッシュ、各/もダッシュ、特殊文字はダッシュ
        encoded = p.replace("/", "-")
        # 連続する非ASCII等もそのまま - 実際のClaude Codeルールに合わせる必要
        return encoded

    mac_encoded = encode_path(mac_cwd)
    vps_cwd = _mac_to_vps_path(mac_cwd)
    vps_encoded = encode_path(vps_cwd)

    mac_dir = os.path.join(projects_dir, mac_encoded)
    vps_dir = os.path.join(projects_dir, vps_encoded)

    # mac_dir が存在しなければ何もしない
    if not os.path.isdir(mac_dir):
        return False

    os.makedirs(vps_dir, exist_ok=True)

    # 該当session_idのjsonl + 関連ファイルを hardlink
    linked = False
    for fname in os.listdir(mac_dir):
        if not fname.startswith(session_id):
            continue
        src = os.path.join(mac_dir, fname)
        dst = os.path.join(vps_dir, fname)
        if os.path.exists(dst) or os.path.islink(dst):
            linked = True
            continue
        if os.path.isdir(src):
            continue
        try:
            os.link(src, dst)
            linked = True
        except OSError:
            pass
    return linked

def _read_claude_sessions(limit: int = 200) -> list[dict]:
    sessions = []
    if not CLAUDE_PROJECTS_DIR.exists():
        return sessions
    jsonl_files = []
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            jsonl_files.append((jf, project_dir.name))
    jsonl_files.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    seen_ids = set()
    for jsonl_file, project_dir_name in jsonl_files[:limit * 5]:
        session_id = jsonl_file.stem
        # sync-conflict や hardlink 重複を除外
        if "sync-conflict" in session_id or session_id in seen_ids:
            continue
        seen_ids.add(session_id)
        first_user_msg = None
        custom_title = None
        last_ts = None
        cwd = None
        try:
            with open(jsonl_file, encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line.strip())
                    except Exception:
                        continue
                    if cwd is None and d.get("cwd"):
                        cwd = d["cwd"]
                    # Claude Code MacのUIで表示される短い自動タイトル (最新が勝ち)
                    if d.get("type") == "custom-title" and d.get("customTitle"):
                        custom_title = d["customTitle"].strip()
                    if first_user_msg is None and d.get("type") == "user":
                        content = d.get("message", {}).get("content", "")
                        if isinstance(content, list):
                            for cc in content:
                                if isinstance(cc, dict) and cc.get("type") == "text":
                                    text = cc.get("text", "").strip()
                                    if text:
                                        first_user_msg = text
                                        break
                        elif isinstance(content, str) and content.strip():
                            first_user_msg = content.strip()
                    ts = d.get("timestamp")
                    if ts:
                        last_ts = ts
                    # 途中breakはしない: custom-title は後ろに追記される可能性あり
        except Exception:
            continue
        if not (first_user_msg or custom_title):
            continue
        if cwd:
            project_label = Path(cwd).name or project_dir_name
        else:
            parts = [p for p in project_dir_name.replace("-", "/").strip("/").split("/") if p]
            project_label = parts[-1] if parts else project_dir_name
        # 表示用タイトル: custom-title 優先、なければ first_user_msg 全文（切らない）
        display = custom_title if custom_title else (first_user_msg or "").replace("\n", " ").strip()
        sessions.append({
            "session_id": session_id,
            "project": project_label,
            "first_msg": display,
            "timestamp": last_ts or "",
            "cwd": cwd or "",
        })
        if len(sessions) >= limit * 3:  # over-read for dedup
            break

    # dedupe: 同じ (project, title) の中で最新のものだけ残す
    from collections import OrderedDict
    deduped: dict = OrderedDict()
    # timestamp 降順でソート（最新が先）
    sessions_sorted = sorted(sessions, key=lambda s: s.get("timestamp", ""), reverse=True)
    for s in sessions_sorted:
        key = (s["project"], s["first_msg"][:120])
        if key not in deduped:
            deduped[key] = s
    result = list(deduped.values())[:limit]
    return result


class ThreadSelectView(discord.ui.View):
    PER_PAGE = 25  # Discord StringSelect 上限

    def __init__(self, sessions: list[dict], channel_id: str, page: int = 0):
        super().__init__(timeout=900)
        self.channel_id = channel_id
        self.all_sessions = sessions
        self._sessions_map = {s["session_id"]: s for s in sessions}
        self.page = page
        self._render()

    def _render(self):
        self.clear_items()
        total = len(self.all_sessions)
        total_pages = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * self.PER_PAGE
        end = start + self.PER_PAGE
        page_sessions = self.all_sessions[start:end]

        options = []
        for s in page_sessions:
            proj = s["project"][:30]
            msg = s["first_msg"][:60]
            label = f"[{proj}] {msg}"[:100]
            ts = s.get("timestamp", "")[:19]
            desc = f"📁 {s['project']}  ·  {ts}"[:100]
            options.append(discord.SelectOption(
                label=label,
                value=s["session_id"],
                description=desc,
                emoji="📁",
            ))
        if not options:
            options = [discord.SelectOption(label="(空)", value="__empty__")]
        placeholder = f"セッション選択… ({self.page + 1}/{total_pages}ページ・全{total}件)"
        sel = discord.ui.Select(
            placeholder=placeholder[:150],
            options=options,
            min_values=1,
            max_values=1,
        )
        sel.callback = self.on_select
        self.add_item(sel)

        # ページネーション ボタン (Selectが1行目、ボタンが2行目)
        prev_btn = discord.ui.Button(
            label="◀ 前のページ",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page == 0),
            row=1,
        )
        prev_btn.callback = self.on_prev
        self.add_item(prev_btn)

        page_label = discord.ui.Button(
            label=f"📄 {self.page + 1}/{total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            row=1,
        )
        self.add_item(page_label)

        next_btn = discord.ui.Button(
            label="次のページ ▶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= total_pages - 1),
            row=1,
        )
        next_btn.callback = self.on_next
        self.add_item(next_btn)

    async def on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self._render()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            await interaction.response.defer()

    async def on_next(self, interaction: discord.Interaction):
        self.page += 1
        self._render()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            await interaction.response.defer()

    async def on_select(self, interaction: discord.Interaction):
        selected_id = interaction.data["values"][0]
        session = self._sessions_map.get(selected_id)
        if not session:
            await interaction.response.send_message("❌ セッションが見つかりません。", ephemeral=True)
            return
        cwd = session.get("cwd", "")
        # Mac-encoded session jsonl を VPS-encoded dir に hardlink (--resume成功させるため)
        if cwd:
            _ensure_vps_session_jsonl(selected_id, cwd)
        vps_cwd = _mac_to_vps_path(cwd) if cwd else str(WORK_DIR)
        row = get_session(self.channel_id)
        cur_model   = row[2] if row and len(row) > 2 and row[2] else DEFAULT_MODEL
        cur_persona = row[3] if row and len(row) > 3 and row[3] else "default"
        save_session(self.channel_id, selected_id, vps_cwd, cur_model, cur_persona)
        # スレッドタイトルを記録（ステータス表示用）
        save_thread_title(self.channel_id, session.get("first_msg", "")[:120])
        await interaction.response.send_message(
            f"✅ セッション引き継ぎ完了\n"
            f"📁 プロジェクト: `{session['project']}`\n"
            f"💬 最初のメッセージ: {session['first_msg'][:60]}\n"
            f"🔑 Session ID: `{selected_id[:8]}...`\n\n"
            f"このチャンネルで続きを話しかけてください！"
        )
        self.stop()


async def _mode_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    return [
        app_commands.Choice(name=f"{v['emoji']} {v['label']}", value=k)
        for k, v in PERMISSION_MODES.items()
        if cur in k.lower() or cur in v['label'].lower()
    ][:25]

@bot.tree.command(name="mode", description="権限モード切替（デンジャラス/編集OK/毎回確認/計画のみ）")
@app_commands.describe(mode="権限モード")
@app_commands.autocomplete(mode=_mode_autocomplete)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_mode(interaction: discord.Interaction, mode: str = ""):
    cid = str(interaction.channel_id)
    if not mode:
        # 現在のモード表示
        current = get_permission_mode(cid)
        lines = [f"**現在の権限モード**: {PERMISSION_MODES[current]['emoji']} `{current}` — {PERMISSION_MODES[current]['label']}"]
        lines.append("")
        lines.append("**切替**: `/mode <モード名>` で変更:")
        for k, v in PERMISSION_MODES.items():
            marker = "👉" if k == current else "  "
            lines.append(f"{marker} {v['emoji']} `{k}` — {v['label']}")
            lines.append(f"       {v['desc']}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        return
    if mode not in PERMISSION_MODES:
        valid = ", ".join(f"`{k}`" for k in PERMISSION_MODES.keys())
        await interaction.response.send_message(f"❌ 無効なモード。有効: {valid}", ephemeral=True)
        return
    set_permission_mode(cid, mode)
    v = PERMISSION_MODES[mode]
    await interaction.response.send_message(
        f"✅ 権限モードを {v['emoji']} **{v['label']}** (`{mode}`) に切替。\n"
        f"{v['desc']}\n"
        f"次のメッセージからこの設定で実行されます。"
    )


@bot.tree.command(name="rewind", description="git で直近Nターン分のファイル変更を巻き戻し（work_dirがgitリポジトリである必要）")
@app_commands.describe(
    turns="いくつ前のコミットに戻すか（1〜50）",
    dry_run="実際には変更せずプレビューだけ表示",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_rewind(interaction: discord.Interaction, turns: int = 1, dry_run: bool = True):
    if turns < 1 or turns > 50:
        await interaction.response.send_message("❌ turns は 1〜50 で指定", ephemeral=True)
        return
    cid = str(interaction.channel_id)
    row = get_session(cid)
    work_dir = row[1] if row and row[1] else str(WORK_DIR)
    await interaction.response.defer()

    # git repo チェック
    check = await asyncio.create_subprocess_exec(
        "git", "-C", work_dir, "rev-parse", "--git-dir",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await check.wait()
    if check.returncode != 0:
        await interaction.followup.send(
            f"❌ `{work_dir}` は git リポジトリではありません。\n"
            f"rewind は git 履歴ベース。先に `git init && git add -A && git commit -m initial` してください。"
        )
        return

    # 対象コミット表示
    log_proc = await asyncio.create_subprocess_exec(
        "git", "-C", work_dir, "log", "--oneline", "-n", str(turns + 1),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    log_out, _ = await log_proc.communicate()
    log_text = log_out.decode(errors="replace").strip()

    # 差分表示
    diff_proc = await asyncio.create_subprocess_exec(
        "git", "-C", work_dir, "diff", "--stat", f"HEAD~{turns}..HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    diff_out, diff_err = await diff_proc.communicate()
    diff_text = diff_out.decode(errors="replace").strip() or "(変更なし)"

    header = f"🔄 **Rewind {turns} ターン** (`{Path(work_dir).name}`)\n\n"
    body = (
        f"**巻き戻す対象のコミット**:\n```\n{log_text[:500]}\n```\n"
        f"**変更統計**:\n```\n{diff_text[:1000]}\n```\n"
    )

    if dry_run:
        await interaction.followup.send(
            header + body +
            f"\nℹ️ これはプレビューです。実行するには: `/rewind turns:{turns} dry_run:False`"
        )
        return

    # バックアップ tag 作成してから reset
    import time as _time
    tag_name = f"before-rewind-{int(_time.time())}"
    tag_proc = await asyncio.create_subprocess_exec(
        "git", "-C", work_dir, "tag", tag_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await tag_proc.wait()

    reset_proc = await asyncio.create_subprocess_exec(
        "git", "-C", work_dir, "reset", "--hard", f"HEAD~{turns}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    reset_stdout, reset_stderr = await reset_proc.communicate()
    if reset_proc.returncode != 0:
        await interaction.followup.send(
            header + body +
            f"❌ reset 失敗:\n```\n{reset_stderr.decode(errors='replace')[:500]}\n```"
        )
        return

    await interaction.followup.send(
        header + body +
        f"✅ **Rewind 完了** — `HEAD~{turns}` に戻しました。\n"
        f"📌 巻き戻し直前の状態は tag `{tag_name}` に保存済み。復元するには:\n"
        f"`git -C {Path(work_dir).name} reset --hard {tag_name}`"
    )


@bot.tree.command(name="thread", description="タスク用の新規スレッドを作成（会話が独立）")
@app_commands.describe(
    name="スレッド名（タスクの概要・100字以内）",
    prompt="最初のメッセージ（省略可）",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_thread(interaction: discord.Interaction, name: str, prompt: str = ""):
    # DMではスレッド機能が使えない
    if interaction.channel is None or not hasattr(interaction.channel, "create_thread"):
        await interaction.response.send_message(
            "❌ このチャンネル（DM等）ではスレッドを作成できません。サーバーのテキストチャンネルで実行してください。",
            ephemeral=True,
        )
        return

    # 既にスレッド内ならその親チャンネルを取得
    parent = interaction.channel
    if isinstance(parent, discord.Thread):
        parent = parent.parent
    if parent is None:
        await interaction.response.send_message("❌ 親チャンネル取得失敗", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        thread = await parent.create_thread(
            name=name[:100],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,  # 7日
            reason=f"Claude session thread by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ スレッド作成権限が無い。サーバー管理者にbotの権限を確認してもらってください。",
            ephemeral=True,
        )
        return
    except Exception as e:
        await interaction.followup.send(f"❌ スレッド作成失敗: {e}", ephemeral=True)
        return

    # 親チャンネルから既存のmodel/persona/work_dirを継承（あれば）
    parent_row = get_session(str(parent.id))
    inh_model = parent_row[2] if parent_row and parent_row[2] else DEFAULT_MODEL
    inh_persona = parent_row[3] if parent_row and len(parent_row) > 3 and parent_row[3] else "default"
    inh_workdir = parent_row[1] if parent_row and parent_row[1] else str(WORK_DIR)

    # 新スレッドに新規セッション用のdefault設定を保存 (session_idはまだ無い、初回応答で生成)
    save_session(str(thread.id), None, inh_workdir, inh_model, inh_persona)

    emoji = MODEL_EMOJI.get(inh_model, "🤖")
    welcome = (
        f"🧵 **{name}**\n"
        f"{emoji} model: `{inh_model}` · 📁 {Path(inh_workdir).name} · 🎭 {inh_persona}\n"
        f"\n"
        f"ここに投稿するメッセージは**このスレッド内だけの独立セッション**として継続されます。\n"
        f"完了したら `/clear` でリセット。7日放置で折りたたみアーカイブ状態になるが、**削除はされない**・メッセージ来れば自動復活。"
    )
    await thread.send(welcome)

    # prompt があれば、そのまま thread に投稿（ユーザー自身の投稿として扱うには on_message で拾う必要あり）
    # 簡易実装: interaction.user の mention で prompt を投稿させ、その後botをメンションしたメッセージとしてClaude処理を起こす
    if prompt.strip():
        # bot自身のメッセージは on_message でフィルタされるため、
        # 初期プロンプトはユーザーがコピペ・送信する形に変更
        await thread.send(
            f"💬 **初回プロンプト（下記をコピペして送信してください）**:\n"
            f"```\n@{bot.user.name} {prompt[:1800]}\n```"
        )

    await interaction.followup.send(f"✅ スレッド作成: {thread.mention}")


@bot.tree.command(name="auto_respond", description="このチャンネルでメンション無しでも自動応答するか切替")
@app_commands.describe(mode="on=有効, off=無効, status=現在の状態")
@app_commands.choices(mode=[
    app_commands.Choice(name="✅ ON (メンション不要)", value="on"),
    app_commands.Choice(name="🔕 OFF (メンション必須)", value="off"),
    app_commands.Choice(name="❓ 状態確認", value="status"),
])
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_auto_respond(interaction: discord.Interaction, mode: str = "status"):
    cid = str(interaction.channel_id)
    if mode == "status":
        cur = is_auto_respond(cid)
        await interaction.response.send_message(
            f"このチャンネルの auto-respond: **{'✅ ON' if cur else '🔕 OFF'}**\n"
            f"切替: `/auto_respond on` または `/auto_respond off`",
            ephemeral=True,
        )
        return
    enable = (mode == "on")
    set_auto_respond(cid, enable)
    await interaction.response.send_message(
        f"{'✅ メンション無しで自動応答するようになりました。' if enable else '🔕 メンション必須に戻しました。'}\n"
        f"全てのメッセージ（bot自身は除く）に反応します。" if enable else ""
    )


@bot.tree.command(name="threads_recent", description="過去N時間以内に更新があったセッションだけ表示")
@app_commands.describe(hours="何時間前まで遡るか（デフォルト1）")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_threads_recent(interaction: discord.Interaction, hours: float = 1.0):
    cid = str(interaction.channel_id)
    await interaction.response.defer()
    all_sessions = _read_claude_sessions(limit=100)  # 多めに取って絞る

    # mtime/timestamp でフィルタ
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(hours=hours)
    filtered = []
    for s in all_sessions:
        ts = s.get("timestamp", "")
        if not ts:
            continue
        try:
            # ISO8601 → datetime
            t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            if t >= cutoff:
                filtered.append(s)
        except Exception:
            continue
    filtered = filtered[:25]
    if not filtered:
        await interaction.followup.send(
            f"📭 過去{hours}時間以内に更新があったセッションはありません。"
        )
        return

    view = ThreadSelectView(filtered, cid)
    summary = f"⏰ **過去{hours}時間のスレッド** {len(filtered)}件\n下のドロップダウンから選択。"
    await interaction.followup.send(summary, view=view)

    from collections import OrderedDict
    grouped: dict[str, list] = OrderedDict()
    for s in filtered:
        proj = s["project"]
        if proj not in grouped:
            grouped[proj] = []
        grouped[proj].append(s)

    lines = []
    counter = 1
    for proj, items in grouped.items():
        lines.append("")
        lines.append(f"📁 **{proj}**")
        for si, s in enumerate(items):
            is_last = (si == len(items) - 1)
            prefix = "  └─" if is_last else "  ├─"
            lines.append(f"{prefix} `{counter:>2}` 🧵 {s['first_msg']}")
            counter += 1

    chunks = []
    cur = ""
    for line in lines:
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        await interaction.followup.send(chunk)


@bot.tree.command(name="recent", description="現在のセッションの直近Nメッセージだけを引き継ぐ（履歴リセット）")
@app_commands.describe(messages="保持するメッセージ数（デフォルト10）")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_recent(interaction: discord.Interaction, messages: int = 10):
    """現在resumeされているセッションのJSONLから直近Nメッセージだけ抽出して
    新規セッションとして引き継ぐ（古い文脈を捨てる）。
    """
    if messages < 2 or messages > 100:
        await interaction.response.send_message("❌ messages は 2〜100 で指定", ephemeral=True)
        return
    cid = str(interaction.channel_id)
    row = get_session(cid)
    if not row or not row[0]:
        await interaction.response.send_message(
            "❌ 現在引き継ぎ中のセッションがありません。先に `/threads` でセッションを選んでください。",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    session_id = row[0]
    work_dir = row[1] if row[1] else str(WORK_DIR)

    # JSONL を探す（VPS-encoded path も Mac-encoded path も）
    import os
    candidates = []
    projects_dir = os.path.expanduser("~/.claude/projects")
    for d in os.listdir(projects_dir):
        full = os.path.join(projects_dir, d, f"{session_id}.jsonl")
        if os.path.exists(full):
            candidates.append(full)

    if not candidates:
        await interaction.followup.send(f"❌ セッションファイル `{session_id}` が見つからない")
        return

    src_jsonl = candidates[0]

    # 直近 N user/assistant メッセージを抽出
    import json
    keep_lines = []
    user_count = 0
    try:
        with open(src_jsonl, encoding="utf-8") as f:
            all_lines = f.readlines()
        # 末尾から逆順に user/assistant 数えて N 個に達するまで
        kept_indices = []
        for i in range(len(all_lines) - 1, -1, -1):
            try:
                d = json.loads(all_lines[i])
                t = d.get("type", "")
                if t in ("user", "assistant"):
                    kept_indices.append(i)
                    if len(kept_indices) >= messages:
                        break
            except Exception:
                continue
        if not kept_indices:
            await interaction.followup.send("❌ メッセージ抽出失敗")
            return
        start = min(kept_indices)
        # start 以降の全行を保持（system, attachment 等含む）
        keep_lines = all_lines[start:]
    except Exception as e:
        await interaction.followup.send(f"❌ JSONL読み取り失敗: {e}")
        return

    # 新規 session_id (UUID) で書き出し
    import uuid
    new_id = str(uuid.uuid4())
    dst_dir = os.path.dirname(src_jsonl)
    dst_jsonl = os.path.join(dst_dir, f"{new_id}.jsonl")
    try:
        # 各行の sessionId を新IDに置換しつつ書き出し
        with open(dst_jsonl, "w", encoding="utf-8") as f:
            for line in keep_lines:
                try:
                    d = json.loads(line)
                    if "sessionId" in d:
                        d["sessionId"] = new_id
                    if "session_id" in d:
                        d["session_id"] = new_id
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
                except Exception:
                    f.write(line)
    except Exception as e:
        await interaction.followup.send(f"❌ JSONL書き出し失敗: {e}")
        return

    # DB更新
    cur_model = row[2] if row and len(row) > 2 and row[2] else DEFAULT_MODEL
    cur_persona = row[3] if row and len(row) > 3 and row[3] else "default"
    save_session(cid, new_id, work_dir, cur_model, cur_persona)

    await interaction.followup.send(
        f"✅ 直近 **{messages}メッセージ** だけで新規セッション開始\n"
        f"🔑 New Session: `{new_id[:8]}...`\n"
        f"📁 work_dir: `{Path(work_dir).name}`\n"
        f"古い文脈は除外、軽量化されました。続けて話しかけてください。"
    )


@bot.tree.command(name="threads", description="Mac Claude Codeのセッション一覧から会話を引き継ぎ")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def slash_threads(interaction: discord.Interaction):
    cid = str(interaction.channel_id)
    await interaction.response.defer()
    sessions = _read_claude_sessions(limit=200)
    if not sessions:
        await interaction.followup.send(
            "📭 セッションが見つかりません。\n"
            "`~/.claude/projects/` がSyncthingで同期されているか確認してください。"
        )
        return
    view = ThreadSelectView(sessions, cid)
    # 概要 + dropdown を最初のメッセージで送る (常に見える位置)
    summary = (
        f"📋 **スレッド一覧**: 全{len(sessions)}件（重複除去済み）\n"
        f"下のドロップダウンから選択。`◀▶` ボタンでページ移動。"
    )
    await interaction.followup.send(summary, view=view)

    # 詳細ツリー表示は補足として続けて送る (任意で読める)
    from collections import OrderedDict
    grouped: dict[str, list] = OrderedDict()
    for s in sessions:
        proj = s["project"]
        if proj not in grouped:
            grouped[proj] = []
        grouped[proj].append(s)

    lines = []
    counter = 1
    for proj, items in grouped.items():
        lines.append("")
        lines.append(f"📁 **{proj}**")
        for si, s in enumerate(items):
            is_last = (si == len(items) - 1)
            prefix = "  └─" if is_last else "  ├─"
            lines.append(f"{prefix} `{counter:>3}` 🧵 {s["first_msg"]}")
            counter += 1

    for chunk in split_message("\n".join(lines)):
        await interaction.followup.send(chunk)


# ── リアクション操作 ───────────────────────────────
REACTION_HELP = (
    "リアクションで操作:\n"
    "  🔄 = 同じ質問でやり直し\n"
    "  📋 = この応答をObsidianに保存\n"
    "  👍 = 「続けて」と送るのと同じ\n"
    "  🗑️ = この応答を削除"
)

async def _save_to_obsidian(content: str, channel: discord.abc.Messageable) -> Path | None:
    """Botの応答をObsidian（VPSのSyncthing経由でMacにも反映）に保存"""
    try:
        OBSIDIAN_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        date_str = time.strftime("%Y-%m-%d")
        time_str = time.strftime("%Y-%m-%d %H:%M")
        ch_name = getattr(channel, "name", "DM") or "DM"
        out = OBSIDIAN_SAVE_DIR / f"{date_str}.md"
        header = "" if out.exists() else f"# Discord保存ログ ({date_str})\n\n"
        block = f"## {time_str} ({ch_name})\n\n{content}\n\n---\n\n"
        with open(out, "a", encoding="utf-8") as f:
            f.write(header + block)
        return out
    except Exception:
        return None

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    emoji = str(payload.emoji)
    if emoji not in ("🔄", "📋", "👍", "🗑️"):
        return
    try:
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return
    if message.author.id != bot.user.id:
        return  # Botの応答にだけ反応

    if emoji == "🗑️":
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return

    if emoji == "📋":
        path = await _save_to_obsidian(message.content, channel)
        if path:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            await channel.send(
                f"📋 Obsidianに保存しました: `{path.name}`",
                delete_after=10
            )
        else:
            await channel.send("⚠️ Obsidianへの保存に失敗しました。", delete_after=10)
        return

    # 🔄 = やり直し / 👍 = 続けて → 元の質問を見つけて再実行
    target_content = None
    if emoji == "🔄":
        # 直前のユーザー質問を取得（このBotメッセージが reply してる場合 reference にある）
        if message.reference and message.reference.message_id:
            try:
                orig = await channel.fetch_message(message.reference.message_id)
                # mention除去
                target_content = re.sub(r"<@!?\d+>", "", orig.content).strip()
                target_msg = orig
            except Exception:
                target_content = None
        if not target_content:
            await channel.send("⚠️ やり直す元の質問が見つかりません。", delete_after=10)
            return
        await channel.send(f"🔄 やり直します: 「{target_content[:80]}」", delete_after=5)
        await handle_message(target_msg, target_content)
    elif emoji == "👍":
        # 「続けて」と送ったのと同じ動作
        # 元のユーザーメッセージを取得して、それの「続き」として処理
        if message.reference and message.reference.message_id:
            try:
                orig = await channel.fetch_message(message.reference.message_id)
                await channel.send("👍 続けます…", delete_after=5)
                await handle_message(orig, "続けて")
            except Exception:
                await channel.send("⚠️ 元のメッセージが見つかりません。", delete_after=10)

@bot.event
async def on_ready():
    print(f"✅ Bot起動: {bot.user} (ID: {bot.user.id})")
    init_db()
    # スケジューラー起動（既に起動してたらスキップ）
    if not getattr(bot, "_scheduler_started", False):
        bot._scheduler_started = True
        bot.loop.create_task(scheduler_loop())
        print("⏰ スケジューラー起動")
    # 全Slashコマンドに DM・User-install 許可を一括適用
    try:
        for cmd in bot.tree.get_commands():
            cmd.allowed_contexts = DM_ALLOWED
            cmd.allowed_installs = DM_INSTALLS
        synced = await bot.tree.sync()
        print(f"⚡ Slash commands synced: {len(synced)} (DM対応有効)")
    except Exception as e:
        print(f"⚠️ Slash sync failed: {e}")
    # VPS-encoded session jsonl を Mac-encoded dir にも hardlink して
    # Mac の Claude Code UI 側でも時系列表示されるように
    try:
        n = _bulk_mirror_vps_to_mac()
        print(f"🔗 Mac-mirror hardlinks: {n} created")
    except Exception as e:
        print(f"⚠️ bulk mirror failed: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm      = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user in message.mentions
    is_auto    = is_auto_respond(str(message.channel.id))

    if not (is_dm or is_mention or is_auto):
        return

    # @mention部分を除去
    content = re.sub(r"<@!?\d+>", "", message.content).strip()
    if not content and not message.attachments:
        await message.reply(
            "何か話しかけてください。画像/PDF/音声/Excelファイルもどうぞ。\n"
            "**スラッシュコマンド**: `/clear` `/model` `/project` `/persona` `/template` "
            "`/schedule_add` `/schedule_list` `/report` `/status`\n"
            "**会話分岐**: `/branch save|list|load|delete`\n"
            "**自然言語OK**: 「opusに切り替えて」「2番のフォルダに移って」\n\n"
            "📌 リアクションでも操作:\n"
            "  🔄=やり直し  📋=Obsidian保存  👍=続けて  🗑️=削除"
        )
        return

    await handle_message(message, content)
    await bot.process_commands(message)

if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)
