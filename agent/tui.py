from __future__ import annotations

import threading

from rich.cells import cell_len
from rich.markup import escape
from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Click, Key
from textual.strip import Strip
from textual.widgets import Input, Log, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from openai import OpenAIError

from agent.clipboard import copy_to_system_clipboard
from agent.assign import AssignStart
from agent.commands import execute_command, filter_commands
from agent.discuss import DiscussStart
from agent.core import Agent
from agent.errors import format_api_error
from agent.planner import format_plan_brief
from agent.workspace import format_workspace_path

USER_MESSAGE_BG = Style(bgcolor="rgb(38,50,66)")
MAX_INPUT_HISTORY = 200


class CommandInput(Input):
    async def _on_key(self, event: Key) -> None:
        app = self.app
        option_list = app.query_one("#command-list", OptionList)
        visible = option_list.has_class("visible")

        if visible and event.key in ("down", "pagedown"):
            event.prevent_default()
            event.stop()
            option_list.action_cursor_down()
            return

        if visible and event.key in ("up", "pageup"):
            event.prevent_default()
            event.stop()
            option_list.action_cursor_up()
            return

        if not visible and event.key == "up":
            if app.navigate_input_history(-1):
                event.prevent_default()
                event.stop()
                return

        if not visible and event.key == "down":
            if app.navigate_input_history(1):
                event.prevent_default()
                event.stop()
                return

        if visible and event.key == "tab":
            event.prevent_default()
            event.stop()
            app.apply_command_suggestion()
            return

        if visible and event.key == "enter":
            event.prevent_default()
            event.stop()
            app.apply_command_suggestion()
            return

        if app._command_list_locked and event.key not in {"enter", "tab"}:
            app._command_list_locked = False

        await super()._on_key(event)


class ActivityPanel(RichLog):
    """类似 Cursor 的实时活动区：思考、工具调用、回复生成。"""

    DEFAULT_CSS = """
    ActivityPanel {
        height: 0;
        min-height: 0;
        overflow: hidden;
        padding: 0 1;
        background: $panel;
        color: $text;
        border-top: none;
    }

    ActivityPanel.visible {
        height: 14;
        min-height: 1;
        border-top: solid $accent 30%;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(
            *args,
            wrap=True,
            markup=True,
            highlight=False,
            auto_scroll=True,
            max_lines=200,
            **kwargs,
        )
        self._status = ""
        self._thinking = ""
        self._tool_title = ""
        self._tool_detail = ""
        self._content = ""

    def reset(self) -> None:
        self._status = ""
        self._thinking = ""
        self._tool_title = ""
        self._tool_detail = ""
        self._content = ""
        self.clear()
        self.remove_class("visible")

    def show(self) -> None:
        self.add_class("visible")

    def set_status(self, status: str) -> None:
        self._status = status
        self._sync()

    def set_thinking(self, text: str) -> None:
        self._thinking = text
        self._sync()

    def set_tool(self, title: str, detail: str) -> None:
        self._tool_title = title
        self._tool_detail = detail
        self._sync()

    def clear_tool(self) -> None:
        self._tool_title = ""
        self._tool_detail = ""
        self._sync()

    def set_content(self, text: str) -> None:
        self._content = text
        self._sync()

    def _sync(self) -> None:
        parts: list[str] = []
        if self._status:
            parts.append(f"[bold cyan]●[/] {escape(self._status)}")
        if self._thinking:
            preview = self._thinking[-3000:] if len(self._thinking) > 3000 else self._thinking
            parts.append(f"[dim]💭 思考[/dim]\n[dim italic]{escape(preview)}[/dim italic]")
        if self._tool_title:
            parts.append(f"[yellow]⚡ {escape(self._tool_title)}[/yellow]")
            if self._tool_detail:
                parts.append(f"[dim]{escape(self._tool_detail)}[/dim]")
        if self._content:
            preview = self._content[-4000:] if len(self._content) > 4000 else self._content
            parts.append(f"[white]{escape(preview)}[/white]")

        self.clear()
        if parts:
            self.write("\n\n".join(parts))


class ChatLog(Log):
    """支持选中复制；用户消息带背景色，助手消息纯文本。"""

    ALLOW_SELECT = True

    DEFAULT_CSS = """
    ChatLog {
        background: $background;
        color: $text;
        border: none;
        padding: 0 1;
        overflow-y: scroll;

        &:focus {
            background-tint: transparent;
        }
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, auto_scroll=True, **kwargs)
        self._user_line_indices: set[int] = set()

    def scroll_to_bottom(self) -> None:
        self.scroll_end(animate=False, immediate=True, x_axis=False)

    def write_lines(
        self,
        lines,
        scroll_end: bool | None = None,
    ):
        should_scroll = self.auto_scroll if scroll_end is None else scroll_end
        super().write_lines(lines, scroll_end=False)
        if should_scroll and not self.is_vertical_scrollbar_grabbed:
            self.scroll_to_bottom()
        return self

    def write_line(self, line: str, scroll_end: bool | None = None):
        should_scroll = self.auto_scroll if scroll_end is None else scroll_end
        super().write_lines([line], scroll_end=False)
        if should_scroll and not self.is_vertical_scrollbar_grabbed:
            self.scroll_to_bottom()
        return self

    def _write_gap(self) -> None:
        """写入可见空行（Log 会忽略末尾真正的空字符串行）。"""
        self.write_line(" ")

    def _ensure_block_start(self) -> None:
        if not self._lines:
            return
        last = self._lines[-1]
        if last == "" or last.strip() == "":
            return
        self._write_gap()

    def append_user(self, text: str) -> None:
        self._ensure_block_start()
        lines = text.splitlines() or [""]
        start = len(self._lines)
        self.write_lines(lines)
        for y in range(start, start + len(lines)):
            self._user_line_indices.add(y)
        self._write_gap()

    def append_plain(self, text: str) -> None:
        if not text:
            return
        self._ensure_block_start()
        self.write_lines(text.splitlines() or [""])
        self._write_gap()

    def append_thinking(self, text: str) -> None:
        if not text.strip():
            return
        body = "\n".join(f"  {line}" for line in text.splitlines())
        self.append_plain(f"▎思考\n{body}")

    def append_tool(self, block: str) -> None:
        if not block.strip():
            return
        self.append_plain(block)

    def on_click(self, event: Click) -> None:
        """点击聊天区以聚焦，便于拖选文本后复制。"""
        self.focus()
        event.stop()

    def _prune_max_lines(self) -> None:
        if self.max_lines is None:
            return
        remove_lines = len(self._lines) - self.max_lines
        if remove_lines <= 0:
            return
        super()._prune_max_lines()
        self._user_line_indices = {
            index - remove_lines
            for index in self._user_line_indices
            if index >= remove_lines
        }

    def _render_line_strip(self, y: int, rich_style: Style) -> Strip:
        selection = self.text_selection
        if y in self._render_line_cache and selection is None:
            return self._render_line_cache[y]

        line = self._process_line(self._lines[y])
        line_text = Text(line, no_wrap=True)
        style = rich_style + USER_MESSAGE_BG if y in self._user_line_indices else rich_style
        line_text.stylize(style)

        if self.highlight:
            line_text = self.highlighter(line_text)
        if selection is not None:
            if (select_span := selection.get_span(y - self._clear_y)) is not None:
                start, end = select_span
                if end == -1:
                    end = len(line_text)
                selection_style = self.screen.get_component_rich_style("screen--selection")
                line_text.stylize(selection_style, start, end)

        strip = Strip(line_text.render(self.app.console), cell_len(line))
        if selection is not None:
            self._render_line_cache[y] = strip
        return strip


class StatusFooter(Static):
    """底部状态栏，显示模型、role、快捷键等信息。"""

    DEFAULT_CSS = """
    StatusFooter {
        height: 1;
        width: 100%;
        background: $footer-background;
        color: $footer-foreground;
        padding: 0 1;
        text-style: none;
    }
    """


class AgentApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    Screen > .screen--selection {
        background: ansi_blue;
        color: ansi_white;
        text-style: bold;
    }

    #bottom-panel {
        dock: bottom;
        height: auto;
        width: 100%;
        layout: vertical;
    }

    #chat {
        height: 1fr;
        width: 1fr;
        border: none;
        margin: 0;
        padding: 0 1;
        background: $background;
        color: $text;
    }

    #chat:focus {
        border: none;
        background-tint: transparent;
    }

    #command-list {
        height: 0;
        min-height: 0;
        max-height: 0;
        overflow: hidden;
        margin: 0 1;
        border: none;
    }

    #command-list.visible {
        height: auto;
        max-height: 8;
        min-height: 1;
        border: solid $accent;
    }

    #prompt {
        height: 3;
        margin: 0 1;
        border: solid $primary;
    }

    #prompt:focus {
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+shift+c", "copy_selection", "复制", priority=True),
        Binding("ctrl+c", "copy_selection", "复制", show=False, priority=True),
        Binding("escape", "hide_commands", "关闭命令列表", show=False),
    ]

    COMMANDS_NEED_ARGS = {"/memory add", "/memory delete", "/workspace", "/discuss", "/assign"}

    def __init__(
        self,
        agent: Agent,
        *,
        gateway_active: bool = False,
        web_url: str = "",
    ) -> None:
        super().__init__()
        self.agent = agent
        self.gateway_active = gateway_active
        self.web_url = web_url.strip()
        self._display_listener = self._on_display_entry
        self._command_list_locked = False
        self._input_history: list[str] = []
        self._input_history_index = -1
        self._input_history_draft = ""
        self._input_history_navigating = False

    def record_input_history(self, text: str) -> None:
        value = text.strip()
        if not value:
            return
        if self._input_history and self._input_history[-1] == value:
            return
        self._input_history.append(value)
        if len(self._input_history) > MAX_INPUT_HISTORY:
            self._input_history = self._input_history[-MAX_INPUT_HISTORY :]

    def _set_prompt_value(self, value: str) -> None:
        prompt = self.query_one("#prompt", CommandInput)
        self._input_history_navigating = True
        try:
            prompt.value = value
            prompt.cursor_position = len(value)
        finally:
            self._input_history_navigating = False

    def navigate_input_history(self, direction: int) -> bool:
        if not self._input_history:
            return False

        prompt = self.query_one("#prompt", CommandInput)
        if direction < 0:
            if self._input_history_index == -1:
                self._input_history_draft = prompt.value
                self._input_history_index = len(self._input_history) - 1
            elif self._input_history_index > 0:
                self._input_history_index -= 1
        elif self._input_history_index == -1:
            return False
        elif self._input_history_index < len(self._input_history) - 1:
            self._input_history_index += 1
        else:
            self._input_history_index = -1
            self._set_prompt_value(self._input_history_draft)
            self.sync_command_suggestions(self._input_history_draft)
            return True

        self._set_prompt_value(self._input_history[self._input_history_index])
        self.sync_command_suggestions(self._input_history[self._input_history_index])
        return True

    def _footer_text(self) -> str:
        cfg = self.agent.config
        role = self.agent.current_role
        role_label = f"{role.title} ({role.name})" if role else self.agent.current_role_name
        mem_count = len(self.agent.memory.list_all())
        plan_label = format_plan_brief(self.agent.planner.current)
        plan_part = f"  计划:[blue]{plan_label}[/]  " if plan_label else "  "
        gateway_part = "  [green]微信已连接[/]  " if self.gateway_active else ""
        web_part = f"  [blue]Web[/]  " if self.web_url else ""
        if self.agent.discuss_is_running():
            discuss_part = (
                "  [red]等待补充[/]  "
                if self.agent.discuss_is_waiting_user()
                else "  [red]讨论中[/]  "
            )
        elif self.agent.assign_is_running():
            discuss_part = "  [red]流水线[/]  "
        else:
            discuss_part = ""
        workspace_part = f"  工作区:[yellow]{format_workspace_path(self.agent.workspace_dir)}[/]  "
        return (
            f"[bold]{cfg.agent_name}[/]  "
            f"模型:[cyan]{self.agent.current_profile_name}[/]  "
            f"role:[magenta]{role_label}[/]  "
            f"{workspace_part}"
            f"skills:[green]{len(self.agent.skills)}[/]  "
            f"记忆:[yellow]{mem_count}[/]/{cfg.max_memory_items}"
            f"{plan_part}"
            f"{discuss_part}"
            f"{gateway_part}"
            f"{web_part}"
        )

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat", highlight=False)
        yield ActivityPanel(id="activity")
        with Vertical(id="bottom-panel"):
            yield OptionList(id="command-list")
            yield CommandInput(
                id="prompt",
                placeholder="输入消息…  输入 / 可实时补全命令",
            )
            yield StatusFooter(self._footer_text(), id="footer-bar")

    def on_mount(self) -> None:
        chat = self.query_one("#chat", ChatLog)
        restored = self.agent.get_display_entries()
        if restored:
            for entry in restored:
                self._render_display_entry(chat, entry)
        elif self.agent.history and self.agent.history.messages:
            self._render_messages_fallback(chat, self.agent.history.messages)
        else:
            chat.append_plain("直接输入问题开始对话，或使用 / 命令。")

        if self.web_url:
            chat.append_plain(f"Web 界面已启动: {self.web_url}")

        plan = self.agent.planner.current
        if plan:
            done, total = plan.progress
            chat.append_plain(
                f"已恢复进行中的计划「{plan.title}」({done}/{total})。"
                " 输入 /plan 查看，或说「继续执行计划」。"
            )

        self.agent.add_display_listener(self._display_listener)
        self.set_interval(2.0, self._poll_shared_history)
        chat.call_after_refresh(chat.scroll_to_bottom)
        self.query_one("#prompt", CommandInput).focus()

    def on_unmount(self) -> None:
        self.agent.remove_display_listener(self._display_listener)

    def _on_display_entry(self, entry_type: str, text: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self._append_display_entry(entry_type, text)
        else:
            self.call_from_thread(self._append_display_entry, entry_type, text)

    def _append_display_entry(self, entry_type: str, text: str) -> None:
        chat = self.query_one("#chat", ChatLog)
        self._render_display_entry(chat, {"type": entry_type, "text": text})

    def _poll_shared_history(self) -> None:
        new_entries = self.agent.sync_from_history()
        if not new_entries:
            return
        chat = self.query_one("#chat", ChatLog)
        for entry in new_entries:
            self._render_display_entry(chat, entry)
        chat.call_after_refresh(chat.scroll_to_bottom)
        self.refresh_status()

    def _render_messages_fallback(
        self, chat: ChatLog, messages: list[dict]
    ) -> None:
        for msg in messages:
            role = msg.get("role")
            if role == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    chat.append_user(content)
                elif content:
                    chat.append_user("[多媒体消息]")
            elif role == "assistant":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    chat.append_plain(content)

    def _render_display_entry(self, chat: ChatLog, entry: dict) -> None:
        entry_type = str(entry.get("type") or "system")
        text = str(entry.get("text") or "")
        if not text.strip():
            return
        if entry_type == "user":
            chat.append_user(text)
        elif entry_type == "thinking":
            chat.append_thinking(text)
        elif entry_type == "tool":
            chat.append_tool(text)
        else:
            chat.append_plain(text)

    def write_discuss(self, speaker: str, text: str) -> None:
        self.agent.record_display("assistant", f"[{speaker}]\n{text}")

    def write_assign(self, speaker: str, text: str) -> None:
        self.agent.record_display("assistant", f"[{speaker}]\n{text}")

    def write_user(self, text: str) -> None:
        self.agent.record_display("user", text)

    def write_agent(self, text: str) -> None:
        self.agent.record_display("assistant", text)

    def write_system(self, text: str) -> None:
        self.agent.record_display("system", text)

    def write_error(self, text: str) -> None:
        self.agent.record_display("system", text)

    def begin_activity(self) -> None:
        panel = self.query_one("#activity", ActivityPanel)
        panel.reset()
        panel.show()
        panel.set_status("准备中…")

    def end_activity(self) -> None:
        panel = self.query_one("#activity", ActivityPanel)
        panel.reset()

    def handle_stream_event(self, event: dict) -> None:
        panel = self.query_one("#activity", ActivityPanel)
        chat = self.query_one("#chat", ChatLog)
        event_type = event.get("type")

        if event_type == "turn_start":
            self.begin_activity()
            return

        if event_type == "turn_end":
            self.end_activity()
            return

        if event_type == "thinking_delta":
            panel.set_status("思考中…")
            panel.set_content("")
            panel.set_thinking(str(event.get("text") or ""))
            return

        if event_type == "thinking_done":
            text = str(event.get("text") or "")
            if text.strip():
                self.agent.record_display("thinking", text)
            panel.set_thinking("")
            return

        if event_type == "tool_call_delta":
            panel.set_status("准备工具调用…")
            panel.set_tool(
                str(event.get("title") or event.get("name") or "工具"),
                str(event.get("detail") or event.get("arguments_raw") or ""),
            )
            return

        if event_type == "tool_calls_ready":
            panel.set_status(f"执行 {event.get('count', 0)} 个工具…")
            return

        if event_type == "tool_start":
            panel.set_status("执行中…")
            panel.set_tool(
                str(event.get("title") or event.get("name") or "工具"),
                str(event.get("detail") or ""),
            )
            return

        if event_type == "tool_end":
            block = str(event.get("block") or "")
            if block.strip():
                self.agent.record_display("tool", block)
            panel.clear_tool()
            panel.set_status("等待模型…")
            return

        if event_type == "content_delta":
            panel.set_status("生成回复…")
            panel.set_content(str(event.get("text") or ""))
            return

        if event_type == "plan_updated":
            plan_data = event.get("plan")
            if not isinstance(plan_data, dict):
                return
            steps = plan_data.get("steps") or []
            title = plan_data.get("title") or "计划"
            text = f"已创建计划「{title}」，共 {len(steps)} 步。"
            self.agent.record_display("system", text)
            self.refresh_status()
            return

        if event_type == "plan_step_updated":
            self.refresh_status()
            return

        if event_type == "plan_completed":
            plan_data = event.get("plan")
            if not isinstance(plan_data, dict):
                return
            steps = plan_data.get("steps") or []
            done = sum(
                1
                for step in steps
                if isinstance(step, dict)
                and step.get("status") in {"completed", "skipped"}
            )
            total = len(steps)
            title = plan_data.get("title") or "计划"
            text = f"计划「{title}」已完成 ({done}/{total})。"
            self.agent.record_display("system", text)
            self.refresh_status()
            return

    def hide_command_list(self) -> None:
        option_list = self.query_one("#command-list", OptionList)
        option_list.clear_options()
        option_list.highlighted = None
        option_list.remove_class("visible")

    def sync_command_suggestions(self, prefix: str) -> None:
        if not prefix.startswith("/"):
            self.hide_command_list()
            return

        matches = filter_commands(prefix, self.agent)
        option_list = self.query_one("#command-list", OptionList)
        if not matches:
            self.hide_command_list()
            return

        option_list.clear_options()
        for cmd, desc in matches:
            option_list.add_option(
                Option(f"[bold cyan]{cmd}[/]  [dim]{desc}[/]", id=cmd)
            )
        option_list.highlighted = 0
        option_list.add_class("visible")

    def apply_command_suggestion(self, cmd: str | None = None) -> None:
        option_list = self.query_one("#command-list", OptionList)
        if cmd is None:
            selected = option_list.highlighted_option
            cmd = selected.id if selected else None
        if not cmd:
            return

        prompt = self.query_one("#prompt", CommandInput)
        needs_more = cmd in self.COMMANDS_NEED_ARGS
        if needs_more:
            new_value = f"{cmd} "
        else:
            new_value = cmd
        if needs_more:
            prompt.value = new_value
            prompt.cursor_position = len(new_value)
        else:
            self._command_list_locked = True
            prompt.value = new_value
            prompt.cursor_position = len(new_value)
            self.hide_command_list()
        prompt.focus()

    @on(Input.Changed, "#prompt")
    async def on_prompt_changed(self, event: Input.Changed) -> None:
        if self._command_list_locked:
            self.hide_command_list()
            self._command_list_locked = False
            return
        if not self._input_history_navigating and self._input_history_index != -1:
            self._input_history_index = -1
            self._input_history_draft = event.value
        self.sync_command_suggestions(event.value)

    def action_hide_commands(self) -> None:
        self.hide_command_list()
        self.query_one("#prompt", CommandInput).focus()

    def action_copy_selection(self) -> None:
        selected = self.screen.get_selected_text()
        if not selected:
            self.notify(
                "请先在聊天区用鼠标拖选文本（需先点击聊天区）",
                title="复制",
                severity="warning",
                timeout=3,
            )
            return
        if copy_to_system_clipboard(selected):
            self.notify("已复制到剪贴板", timeout=1)
            return
        self.copy_to_clipboard(selected)
        self.notify("已复制（若粘贴无效，请安装 xclip 或 wl-copy）", timeout=2)

    @on(OptionList.OptionSelected, "#command-list")
    async def on_command_selected(self, event: OptionList.OptionSelected) -> None:
        self.apply_command_suggestion(event.option.id or "")

    @on(Input.Submitted, "#prompt")
    async def on_prompt_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        self.hide_command_list()
        self._input_history_index = -1
        self._input_history_draft = ""
        if not text:
            return

        self.record_input_history(text)
        self.write_user(text)

        if self.agent.discuss_is_running():
            if self.agent.discuss_is_waiting_user():
                if text == "/discuss stop":
                    self.agent.request_discuss_stop("用户发送 /discuss stop")
                    self.write_system("已请求停止讨论…")
                elif self.agent.submit_discuss_input(text):
                    self.write_system("已收到补充信息，讨论继续…")
                else:
                    self.write_system("讨论正在等待您的回复，请直接输入补充信息。")
            elif text != "/discuss stop":
                self.agent.request_discuss_stop(text)
                self.write_system("已请求停止讨论，等待当前发言结束后汇总各模型方案…")
            else:
                command_result, _ = execute_command(self.agent, text)
                if isinstance(command_result, str):
                    self.write_system(command_result)
            self.refresh_status()
            return

        if self.agent.assign_is_running():
            if text != "/assign stop":
                self.agent.request_assign_stop(text)
                self.write_system("已请求停止流水线，等待当前步骤结束后汇总…")
            else:
                command_result, _ = execute_command(self.agent, text)
                if isinstance(command_result, str):
                    self.write_system(command_result)
            self.refresh_status()
            return

        command_result, should_exit = execute_command(self.agent, text)
        if should_exit:
            self.write_system("再见。")
            self.exit()
            return
        if isinstance(command_result, DiscussStart):
            names = ", ".join(profile.name for profile in command_result.profiles)
            self.write_system(
                f"开始多模型讨论: {command_result.topic}\n参与者: {names}\n"
                f"工作区: {self.agent.get_workspace_info()['display']}\n"
                "讨论基于当前工作区；缺信息时会暂停请你补充。\n"
                "其他时候发消息可停止讨论。"
            )
            self.refresh_status()
            self.run_discuss(command_result)
            return
        if isinstance(command_result, AssignStart):
            lines = []
            for job in command_result.jobs:
                step = job.index + 1
                role = "执行/写入" if job.index == 0 else "审查"
                profile = self.agent.resolve_profile_ref(job.profile_name)
                lines.append(
                    f"  步骤 {step} · {profile.name} ({role}): {job.task}\n"
                    f"    → {profile.model} @ {profile.base_url}"
                )
            self.write_system(
                "开始流水线（顺序执行）:\n"
                + "\n".join(lines)
                + f"\n工作区: {self.agent.get_workspace_info()['display']}\n"
                "后序模型审查前序产出；发现问题会 REJECT 并交回执行者修改后重审。发消息可停止。"
            )
            self.refresh_status()
            self.run_assign(command_result)
            return
        if command_result is not None:
            self.write_system(command_result)
            self.refresh_status()
            return

        prompt = self.query_one("#prompt", CommandInput)
        prompt.disabled = True
        self.run_chat(text)

    @work(thread=True, exclusive=True)
    def run_discuss(self, start: DiscussStart) -> None:
        def on_event(event: dict) -> None:
            self.call_from_thread(self.handle_discuss_event, event)

        try:
            result = self.agent.run_discuss(start, on_event=on_event)
        except OpenAIError as exc:
            self.call_from_thread(self.end_activity)
            self.call_from_thread(self.on_chat_error, format_api_error(exc))
            return
        except Exception as exc:
            self.call_from_thread(self.end_activity)
            self.call_from_thread(self.on_chat_error, f"错误: {exc}")
            return
        self.call_from_thread(self.on_discuss_success, result.text)

    @work(thread=True, exclusive=True)
    def run_assign(self, start: AssignStart) -> None:
        def on_event(event: dict) -> None:
            self.call_from_thread(self.handle_assign_event, event)

        try:
            result = self.agent.run_assign(start, on_event=on_event)
        except OpenAIError as exc:
            self.call_from_thread(self.end_activity)
            self.call_from_thread(self.on_chat_error, format_api_error(exc))
            return
        except Exception as exc:
            self.call_from_thread(self.end_activity)
            self.call_from_thread(self.on_chat_error, f"错误: {exc}")
            return
        self.call_from_thread(self.on_assign_success, result.text)

    def handle_assign_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "assign_start":
            self.begin_activity()
            panel = self.query_one("#activity", ActivityPanel)
            panel.set_status("流水线执行…")
            for warning in event.get("warnings") or []:
                self.write_system(str(warning))
            return
        if event_type == "assign_round_start":
            round_num = event.get("round")
            max_rounds = event.get("max_rounds")
            note = "（修改轮）" if event.get("is_revision") else ""
            self.write_system(f"=== 流水线第 {round_num}/{max_rounds} 轮 {note}===")
            return
        if event_type == "assign_revision":
            profile = str(event.get("profile") or "")
            feedback = str(event.get("feedback") or "")
            round_num = event.get("round")
            self.write_system(
                f"[{profile}] 第 {round_num} 轮审查发现问题，意见已交回执行者修改:\n{feedback}"
            )
            return
        if event_type == "assign_pipeline_step":
            step = event.get("step")
            total = event.get("total")
            profile = str(event.get("profile") or "")
            base_url = str(event.get("base_url") or "")
            round_num = event.get("round")
            round_note = f" · 第 {round_num} 轮" if round_num else ""
            url_note = f" @ {base_url}" if base_url else ""
            self.write_system(f"--- 流水线 步骤 {step}/{total}{round_note}: {profile}{url_note} ---")
            return
        if event_type == "assign_worker_start":
            profile = str(event.get("profile") or "")
            model = str(event.get("model") or "")
            base_url = str(event.get("base_url") or "")
            task = str(event.get("task") or "")
            role = "执行" if event.get("role") == "executor" else "审查"
            endpoint = f"{model} @ {base_url}" if model and base_url else profile
            self.write_system(f"[{profile}] {role}: {task}\n  API: {endpoint}")
            return
        if event_type == "assign_progress":
            profile = str(event.get("profile") or "")
            content = str(event.get("content") or "")
            turn = event.get("turn")
            self.write_assign(profile, f"进度 #{turn}\n{content}")
            panel = self.query_one("#activity", ActivityPanel)
            panel.set_status(f"{profile} 执行中…")
            panel.set_content(content[:500])
            return
        if event_type == "assign_tool":
            profile = str(event.get("profile") or "")
            title = str(event.get("title") or event.get("name") or "工具")
            detail = str(event.get("detail") or "")
            panel = self.query_one("#activity", ActivityPanel)
            panel.set_status(f"{profile} · {title}")
            if detail:
                panel.set_tool(title, detail)
            return
        if event_type == "assign_worker_done":
            profile = str(event.get("profile") or "")
            summary = str(event.get("summary") or "")
            outcome = str(event.get("outcome") or "")
            if outcome == "reject":
                label = "要求修改"
            elif outcome == "approved":
                label = "审查通过"
            else:
                label = "完成"
            self.write_assign(profile, f"{label}\n{summary}")
            return

    def on_assign_success(self, result: str) -> None:
        prompt = self.query_one("#prompt", CommandInput)
        prompt.focus()
        self.end_activity()
        self.write_system(result)
        self.refresh_status()

    def handle_discuss_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "discuss_start":
            self.begin_activity()
            panel = self.query_one("#activity", ActivityPanel)
            panel.set_status("多模型讨论…")
            return
        if event_type == "discuss_round":
            self.write_system(f"--- 第 {event.get('round')} 轮 ---")
            return
        if event_type == "discuss_turn":
            profile = str(event.get("profile") or "")
            status = str(event.get("status") or "")
            content = str(event.get("content") or "")
            round_num = event.get("round")
            self.write_discuss(
                profile,
                f"第 {round_num} 轮 · {status}\n{content}",
            )
            panel = self.query_one("#activity", ActivityPanel)
            panel.set_status(f"{profile} 发言中…")
            panel.set_content(content[:500])
            return
        if event_type == "discuss_tool":
            profile = str(event.get("profile") or "")
            title = str(event.get("title") or event.get("name") or "工具")
            detail = str(event.get("detail") or "")
            panel = self.query_one("#activity", ActivityPanel)
            panel.set_status(f"{profile} · {title}")
            if detail:
                panel.set_tool(title, detail)
            return
        if event_type == "discuss_consensus":
            self.write_system("全员 CONSENSUS，已达成一致。")
            return
        if event_type == "discuss_need_user":
            message = str(event.get("message") or "")
            if message:
                self.write_system(message)
            self.refresh_status()
            return
        if event_type == "discuss_user_supplement":
            answer = str(event.get("answer") or "")
            self.write_system(f"已记录您的补充信息，讨论继续。\n\n{answer}")
            self.refresh_status()
            return
        if event_type == "discuss_final":
            profile = str(event.get("profile") or "")
            content = str(event.get("content") or "")
            self.write_discuss(profile, f"最终方案\n{content}")
            return

    def on_discuss_success(self, result: str) -> None:
        prompt = self.query_one("#prompt", CommandInput)
        prompt.focus()
        self.end_activity()
        self.write_system(result)
        self.refresh_status()

    @work(thread=True, exclusive=True)
    def run_chat(self, user_input: str) -> None:
        def on_event(event: dict) -> None:
            self.call_from_thread(self.handle_stream_event, event)

        try:
            reply = self.agent.chat(user_input, on_event=on_event)
        except OpenAIError as exc:
            self.call_from_thread(self.end_activity)
            self.call_from_thread(self.on_chat_error, format_api_error(exc))
            return
        except Exception as exc:
            self.call_from_thread(self.end_activity)
            self.call_from_thread(self.on_chat_error, f"错误: {exc}")
            return
        self.call_from_thread(self.on_chat_success, reply)

    def on_chat_success(self, reply: str) -> None:
        prompt = self.query_one("#prompt", CommandInput)
        prompt.disabled = False
        prompt.focus()
        self.end_activity()
        self.write_agent(reply)
        self.refresh_status()

    def on_chat_error(self, message: str) -> None:
        prompt = self.query_one("#prompt", CommandInput)
        prompt.disabled = False
        prompt.focus()
        self.end_activity()
        self.write_error(message)

    def refresh_status(self) -> None:
        footer = self.query_one("#footer-bar", StatusFooter)
        footer.update(self._footer_text(), layout=False)


def run_tui(
    agent: Agent,
    *,
    gateway_active: bool = False,
    web_url: str = "",
) -> None:
    app = AgentApp(agent, gateway_active=gateway_active, web_url=web_url)
    app.run()
