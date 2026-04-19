"""Permission UI: Claude が tool 使用しようとした時 Discord ボタンで承認"""
import asyncio
import discord
import json

# 自動 allow するツール（読み取り系）
AUTO_ALLOW_TOOLS = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "TodoWrite", "TodoRead", "ListMcpResourcesTool",
    "AskUserQuestion", "Skill", "Task",
}


def describe_tool(tool_name: str, tool_input: dict) -> str:
    """ツール実行内容を1行で人間可読に"""
    if tool_name in ("Bash", "bash"):
        cmd = str(tool_input.get("command", ""))
        return f"`bash`: `{cmd[:200]}`"
    if tool_name == "Write":
        return f"ファイル作成/上書き: `{tool_input.get('file_path', '?')}`"
    if tool_name == "Edit":
        return f"ファイル編集: `{tool_input.get('file_path', '?')}`"
    if tool_name == "MultiEdit":
        return f"複数編集: `{tool_input.get('file_path', '?')}`"
    if tool_name == "NotebookEdit":
        return f"Notebook編集: `{tool_input.get('notebook_path', '?')}`"
    return f"ツール `{tool_name}` 実行"


class AllowDenyView(discord.ui.View):
    """Allow/Deny + Always Allow ボタン"""
    def __init__(self, tool_name: str, timeout: float = 600.0):
        super().__init__(timeout=timeout)
        self.tool_name = tool_name
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()

    @discord.ui.button(label="✅ 許可", style=discord.ButtonStyle.success)
    async def allow_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.future.done():
            self.future.set_result(("allow", False))
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="🔁 常に許可", style=discord.ButtonStyle.primary)
    async def always_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.future.done():
            self.future.set_result(("allow", True))
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ 拒否", style=discord.ButtonStyle.danger)
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.future.done():
            self.future.set_result(("deny", False))
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        if not self.future.done():
            self.future.set_result(("deny", False))


class DiscordPermissionUI:
    """Discord channel に Allow/Deny embed を送って結果を待つ"""

    def __init__(self, channel: discord.abc.Messageable, allowed_session: set | None = None):
        self.channel = channel
        # session-allow tool name の集合（"常に許可" 押されたら追加）
        self.allowed_session: set[str] = allowed_session if allowed_session is not None else set()

    async def request(self, tool_name: str, tool_input: dict) -> tuple[str, bool]:
        """戻り値: ('allow'|'deny', always_allow_in_session)"""
        # 自動allow
        if tool_name in AUTO_ALLOW_TOOLS:
            return ("allow", False)
        # 既にsession allow済み
        if tool_name in self.allowed_session:
            return ("allow", False)

        # Embed 作成
        embed = discord.Embed(
            title=f"🔐 ツール実行の承認: `{tool_name}`",
            description=describe_tool(tool_name, tool_input),
            color=0xFF9900,
        )
        # input の主要フィールドだけ field 化
        preview = json.dumps(tool_input, ensure_ascii=False, indent=2)
        if len(preview) > 800:
            preview = preview[:800] + "\n…(省略)"
        embed.add_field(name="入力", value=f"```json\n{preview}\n```", inline=False)
        embed.set_footer(text="このメッセージは10分後に自動 Deny")

        view = AllowDenyView(tool_name=tool_name)
        msg = await self.channel.send(embed=embed, view=view)
        try:
            decision, always = await view.future
        except Exception:
            decision, always = "deny", False

        # session allow 追加
        if always and decision == "allow":
            self.allowed_session.add(tool_name)
            embed.color = 0x00CC66
            embed.set_footer(text=f"✅ 常に許可 → このセッション中の {tool_name} は自動承認")
        elif decision == "allow":
            embed.color = 0x00CC66
            embed.set_footer(text="✅ 許可")
        else:
            embed.color = 0xCC4444
            embed.set_footer(text="❌ 拒否")
        try:
            await msg.edit(embed=embed)
        except Exception:
            pass

        return (decision, always)


def make_pretool_hook(perm_ui: DiscordPermissionUI):
    """SDK PreToolUse hook を返す。perm_ui に問い合わせて allow/deny する"""
    async def hook(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        decision, _ = await perm_ui.request(tool_name, tool_input)
        if decision == "allow":
            return {}  # 承認 = empty
        # 拒否
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"User denied {tool_name} via Discord button",
            }
        }
    return hook
