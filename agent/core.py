from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.config import Config, load_config
from agent.assign import AssignResult, AssignRunner, AssignStart, AssignState
from agent.discuss import (
    DiscussResult,
    DiscussRunner,
    DiscussStart,
    DiscussState,
    build_workspace_context,
)
from agent.executor import WorkspaceExecutor
from agent.history import ChatHistoryStore, session_history_path
from agent.llm import LLMClient, serialize_assistant_message
from agent.memory import MemoryStore, build_memory_prompt
from agent.model_settings import (
    ModelProfile,
    default_profile,
    resolve_model_profile,
    thinking_mode_for_model,
)
from agent.messages_util import sanitize_messages_for_api
from agent.planner import PlanManager, format_plan_prompt
from agent.roles import (
    Role,
    build_role_prompt,
    delete_role_file,
    format_roles_list,
    get_role,
    load_roles,
    save_role_file,
)
from agent.skills import Skill, build_skills_prompt, load_skills
from agent.tool_display import (
    format_tool_detail,
    format_tool_result_block,
    format_tool_title,
    try_parse_tool_args,
)
from agent.tools import AGENT_TOOLS, DISCUSS_READ_TOOLS
from agent.workspace import format_workspace_path, resolve_workspace


def _emit(on_event: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if on_event:
        on_event(payload)


class Agent:
    def __init__(
        self,
        config: Config | None = None,
        *,
        session_key: str | None = "local",
        persist_history: bool | None = None,
    ):
        self.config = config or load_config()
        self.session_key = session_key
        self._model_profiles = self.config.model_profiles
        self._default_model_profile = default_profile(self._model_profiles)
        self._current_profile_name = self._default_model_profile.name
        self.llm = LLMClient(self.config)
        self.skills: list[Skill] = load_skills(self.config.skills_dirs)
        self.roles: list[Role] = load_roles(self.config.roles_dir)
        self.current_role_name = self._resolve_initial_role()
        self.memory = MemoryStore(
            self.config.memory_path,
            max_items=self.config.max_memory_items,
        )
        self.planner = PlanManager(max_steps=self.config.max_plan_steps)
        self.executor = WorkspaceExecutor(
            self.config.workspace_dir,
            allow_shell=self.config.allow_shell,
            command_timeout=self.config.command_timeout,
        )
        self.channel_id: str | None = None
        self._pending_attachments: list[str] = []
        self._outbound_attachment_handler: Callable[[str], None] | None = None
        self._display_listeners: list[Callable[[str, str], None]] = []
        self._chat_lock = threading.Lock()
        self._discuss_stop = threading.Event()
        self._discuss_user_ready = threading.Event()
        self._discuss_user_text = ""
        self._discuss_running = False
        self._discuss_waiting_user = False
        self._discuss_state: DiscussState | None = None
        self._assign_stop = threading.Event()
        self._assign_running = False
        self._assign_state: AssignState | None = None
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._build_system_prompt()}
        ]
        self.history = self._init_history(persist_history)
        self._restore_from_history()

    def _init_history(self, persist_history: bool | None) -> ChatHistoryStore | None:
        should_persist = (
            persist_history
            if persist_history is not None
            else self.config.persist_chat_history
        )
        if not should_persist or not self.session_key:
            return None
        path = session_history_path(
            self.config.chat_history_path,
            self.config.sessions_dir,
            self.session_key,
        )
        return ChatHistoryStore(
            path,
            max_messages=self.config.max_chat_messages,
        )

    def _restore_from_history(self) -> None:
        if self.history is None:
            return

        stored_role = self.history.current_role_name
        if stored_role and get_role(self.roles, stored_role):
            self.current_role_name = stored_role

        if self.history.workspace_dir:
            try:
                self.executor.set_workspace(
                    resolve_workspace(
                        self.history.workspace_dir,
                        default=self.config.workspace_dir,
                    )
                )
            except ValueError:
                self.executor.set_workspace(self.config.workspace_dir)

        stored_messages = sanitize_messages_for_api(self.history.messages)
        if stored_messages:
            self.messages = [
                {"role": "system", "content": self._build_system_prompt()}
            ] + stored_messages
        elif stored_role and get_role(self.roles, stored_role):
            self.refresh_system_prompt()

        self._restore_plan_from_history()
        self._restore_model_from_history()

    def _restore_model_from_history(self) -> None:
        if self.history is None or not self.history.current_model:
            return
        try:
            self.set_model(self.history.current_model, persist=False)
        except ValueError:
            self._apply_model_profile(self._default_model_profile, persist=False)

    def _apply_model_profile(self, profile: ModelProfile, *, persist: bool) -> None:
        self.llm.apply_settings(
            model=profile.model,
            base_url=profile.base_url,
            api_key=profile.api_key,
            thinking_mode=thinking_mode_for_model(
                profile.model,
                locked=self.config.thinking_mode_locked,
                locked_value=self.config.thinking_mode,
            ),
        )
        self._current_profile_name = profile.name
        if persist and self.history is not None:
            stored = profile.name if profile.name != "default" else None
            self.history.set_current_model(stored)

    def _restore_plan_from_history(self) -> None:
        if self.history is None:
            return
        self.planner.restore_active_plan(self.history.active_plan)

    def _sync_plan_to_history(self) -> None:
        if self.history is None:
            return
        self.history.set_active_plan(self.planner.export_active_plan())

    @property
    def current_model(self) -> str:
        return self.llm.model

    @property
    def current_profile_name(self) -> str:
        return self._current_profile_name

    def get_model_profile(self, name: str) -> ModelProfile:
        return resolve_model_profile(
            name,
            profiles=self._model_profiles,
            default=self._default_model_profile,
        )

    def resolve_profile_ref(self, name: str) -> ModelProfile:
        from agent.model_settings import is_profile_ref_current

        text = (name or "").strip()
        if is_profile_ref_current(text):
            return self.get_model_profile(self._current_profile_name)
        return self.get_model_profile(text)

    def resolve_profile_ref_strict(self, name: str) -> ModelProfile:
        from agent.model_settings import (
            find_model_profile,
            format_unknown_profile_error,
            is_profile_ref_current,
        )

        text = (name or "").strip()
        if is_profile_ref_current(text):
            return self.get_model_profile(self._current_profile_name)
        if find_model_profile(text, profiles=self._model_profiles) is None:
            raise ValueError(
                format_unknown_profile_error(text, profiles=self._model_profiles)
            )
        return self.get_model_profile(text)

    def profile_resolution_warning(self, ref: str, profile: ModelProfile) -> str | None:
        from agent.model_settings import is_adhoc_model_profile

        if is_adhoc_model_profile(
            ref,
            profile,
            profiles=self._model_profiles,
        ):
            return (
                f"未识别的配置名「{ref.strip()}」，已回退到默认 API ({profile.base_url})。"
                "请使用 /model list 中的名称，或用 @current 引用当前 /model 选择。"
            )
        return None

    def list_model_presets(self) -> list[str]:
        return [profile.name for profile in self._model_profiles]

    def get_model_info(self) -> dict[str, str | bool]:
        current = self.get_model_profile(self._current_profile_name)
        default = self._default_model_profile
        return {
            "profile": current.name,
            "provider": current.provider or "",
            "model": current.model,
            "base_url": current.base_url,
            "default_profile": default.name,
            "default_model": default.model,
            "default_base_url": default.base_url,
            "is_default": current.name == default.name,
            "thinking_mode": self.llm.thinking_mode,
            "thinking_mode_locked": self.config.thinking_mode_locked,
        }

    def set_model(self, name: str, *, persist: bool = True) -> ModelProfile:
        profile = resolve_model_profile(
            name,
            profiles=self._model_profiles,
            default=self._default_model_profile,
        )
        self._apply_model_profile(profile, persist=persist)
        self.refresh_system_prompt()
        return profile

    def set_thinking_mode(self, enabled: bool) -> None:
        if self.config.thinking_mode_locked:
            raise ValueError("思考模式已在 .env 中固定，无法在线修改")
        self.llm.thinking_mode = bool(enabled)

    def get_public_messages(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for msg in self.messages:
            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    items.append({"role": str(role), "content": text})
                continue
            if isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and part.get("text"):
                        parts.append(str(part["text"]))
                text = "\n".join(parts).strip()
                if text:
                    items.append({"role": str(role), "content": text})
        return items

    def discuss_is_running(self) -> bool:
        return self._discuss_running

    def assign_is_running(self) -> bool:
        return self._assign_running

    def task_session_active(self) -> bool:
        return self.discuss_is_running() or self.assign_is_running()

    def discuss_is_waiting_user(self) -> bool:
        return self._discuss_waiting_user

    def _build_discuss_workspace_context(self) -> str:
        def list_root() -> list[str]:
            try:
                return self.executor.list_directory(".")
            except ValueError:
                return []

        info = self.get_workspace_info()
        return build_workspace_context(
            self.workspace_dir,
            list_root,
            display_name=str(info["display"]),
        )

    def _wait_for_discuss_user(self, question: str) -> str:
        self._discuss_waiting_user = True
        self._discuss_user_ready.clear()
        self._discuss_user_text = ""
        self._discuss_user_ready.wait()
        self._discuss_waiting_user = False
        return self._discuss_user_text

    def submit_discuss_input(self, text: str) -> bool:
        if not self._discuss_running or not self._discuss_waiting_user:
            return False
        self._discuss_user_text = text.strip()
        self._discuss_user_ready.set()
        return True

    def format_discuss_status(self) -> str | None:
        if not self._discuss_running or self._discuss_state is None:
            return None
        state = self._discuss_state
        names = ", ".join(profile.name for profile in state.profiles)
        rounds = state.transcript[-1].round_num if state.transcript else 0
        workspace_line = state.workspace_context.splitlines()[0] if state.workspace_context else "未知"
        waiting = (
            f"\n等待用户补充: {state.pending_question}"
            if state.waiting_for_user
            else ""
        )
        return (
            f"讨论进行中\n"
            f"主题: {state.topic}\n"
            f"工作区: {workspace_line}\n"
            f"参与者: {names}\n"
            f"已完成轮次: {rounds}\n"
            f"发言数: {len(state.transcript)}"
            f"{waiting}"
        )

    def request_discuss_stop(self, reason: str = "") -> bool:
        if not self._discuss_running:
            return False
        if self._discuss_state is not None:
            self._discuss_state.stop_reason = reason.strip()
        self._discuss_stop.set()
        if self._discuss_waiting_user:
            self._discuss_user_text = ""
            self._discuss_user_ready.set()
        return True

    def handle_discuss_tool_call(self, name: str, arguments: str) -> str:
        allowed = {"read_file", "list_directory", "get_workspace"}
        if name not in allowed:
            return json.dumps(
                {
                    "ok": False,
                    "error": "讨论模式仅允许 read_file、list_directory、get_workspace",
                },
                ensure_ascii=False,
            )
        args = json.loads(arguments or "{}")
        try:
            if name == "read_file":
                content = self.executor.read_file(args.get("path", ""))
                return json.dumps({"ok": True, "content": content}, ensure_ascii=False)
            if name == "list_directory":
                entries = self.executor.list_directory(args.get("path") or ".")
                return json.dumps({"ok": True, "entries": entries}, ensure_ascii=False)
            info = self.get_workspace_info()
            return json.dumps({"ok": True, **info}, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def handle_assign_tool_call(self, worker_index: int, name: str, arguments: str) -> str:
        read_allowed = {"read_file", "list_directory", "get_workspace"}
        if name in read_allowed:
            return self.handle_discuss_tool_call(name, arguments)
        if name == "write_file":
            if worker_index != 0:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "仅第一个模型可 write_file，请将修改建议写入 DONE 总结",
                    },
                    ensure_ascii=False,
                )
            args = json.loads(arguments or "{}")
            try:
                rel_path = self.executor.write_file(
                    args.get("path", ""),
                    args.get("content", ""),
                )
            except ValueError as exc:
                return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"ok": True, "path": rel_path}, ensure_ascii=False)
        return json.dumps(
            {"ok": False, "error": f"分工模式不允许工具: {name}"},
            ensure_ascii=False,
        )

    def format_assign_status(self) -> str | None:
        if not self._assign_running or self._assign_state is None:
            return None
        from agent.assign import format_assign_status

        return format_assign_status(self._assign_state)

    def request_assign_stop(self, reason: str = "") -> bool:
        if not self._assign_running:
            return False
        if self._assign_state is not None:
            self._assign_state.stop_reason = reason.strip()
        self._assign_stop.set()
        return True

    def run_assign(
        self,
        start: AssignStart,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> AssignResult:
        if self.task_session_active():
            raise RuntimeError("已有讨论或分工任务在进行中")
        self._assign_running = True
        self._assign_stop.clear()
        state = AssignState(
            jobs=start.jobs,
            workspace_context=self._build_discuss_workspace_context(),
        )
        self._assign_state = state
        try:
            runner = AssignRunner(
                config=self.config,
                state=state,
                stop_event=self._assign_stop,
                handle_tool=self.handle_assign_tool_call,
                resolve_profile=self.resolve_profile_ref,
                profile_warning=self.profile_resolution_warning,
                on_event=on_event,
                max_turns=self.config.max_assign_turns,
                max_tool_steps=self.config.max_assign_tool_steps,
                max_rounds=self.config.max_assign_rounds,
            )
            from agent.graph_assign import run_assign_graph

            return run_assign_graph(runner)
        finally:
            self._assign_running = False
            self._assign_state = None
            self._assign_stop.clear()

    def run_discuss(
        self,
        start: DiscussStart,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> DiscussResult:
        if self._discuss_running:
            raise RuntimeError("已有讨论在进行中")
        if self._assign_running:
            raise RuntimeError("已有分工任务在进行中")
        self._discuss_running = True
        self._discuss_stop.clear()
        self._discuss_user_ready.clear()
        self._discuss_waiting_user = False
        state = DiscussState(
            topic=start.topic,
            profiles=start.profiles,
            workspace_context=self._build_discuss_workspace_context(),
        )
        self._discuss_state = state
        try:
            runner = DiscussRunner(
                config=self.config,
                state=state,
                stop_event=self._discuss_stop,
                wait_for_user=self._wait_for_discuss_user,
                handle_tool=self.handle_discuss_tool_call,
                tools=DISCUSS_READ_TOOLS,
                on_event=on_event,
                max_rounds=self.config.max_discuss_rounds,
            )
            from agent.graph_discuss import run_discuss_graph

            return run_discuss_graph(runner)
        finally:
            self._discuss_running = False
            self._discuss_waiting_user = False
            self._discuss_state = None
            self._discuss_stop.clear()
            self._discuss_user_ready.set()

    def _persist_messages(self) -> None:
        if self.history is None:
            return
        non_system = [msg for msg in self.messages if msg.get("role") != "system"]
        self.history.current_role_name = self.current_role_name
        self.history.workspace_dir = str(self.workspace_dir)
        self.history.set_messages(non_system)

    @property
    def workspace_dir(self) -> Path:
        return self.executor.workspace

    def get_workspace_info(self) -> dict[str, str | bool]:
        default = self.config.workspace_dir.resolve()
        current = self.workspace_dir
        return {
            "path": str(current),
            "display": format_workspace_path(current),
            "default_path": str(default),
            "default_display": format_workspace_path(default),
            "is_default": current == default,
        }

    def set_workspace(self, path: str) -> Path:
        resolved = resolve_workspace(path, default=self.config.workspace_dir)
        self.executor.set_workspace(resolved)
        if self.history is not None:
            self.history.workspace_dir = str(resolved)
            self.history.touch_metadata()
        self.refresh_system_prompt()
        return resolved

    def record_display(self, entry_type: str, text: str) -> None:
        if not text.strip():
            return
        if self.history is not None:
            self.history.current_role_name = self.current_role_name
            self.history.workspace_dir = str(self.workspace_dir)
            self.history.record_display(entry_type, text)
        for listener in list(self._display_listeners):
            try:
                listener(entry_type, text)
            except Exception:
                pass

    def add_display_listener(self, listener: Callable[[str, str], None]) -> None:
        self._display_listeners.append(listener)

    def remove_display_listener(self, listener: Callable[[str, str], None]) -> None:
        if listener in self._display_listeners:
            self._display_listeners.remove(listener)

    def sync_from_history(self) -> list[dict[str, Any]]:
        """从磁盘重新加载历史，返回新增的展示条目。"""
        if self.history is None:
            return []

        previous_count = len(self.history.display)
        self.history.load()

        if self._chat_lock.locked():
            new_entries = self.history.display[previous_count:]
            return [{"type": item.type, "text": item.text} for item in new_entries]

        stored_role = self.history.current_role_name
        if stored_role and get_role(self.roles, stored_role):
            self.current_role_name = stored_role

        if self.history.workspace_dir:
            try:
                self.executor.set_workspace(
                    resolve_workspace(
                        self.history.workspace_dir,
                        default=self.config.workspace_dir,
                    )
                )
            except ValueError:
                self.executor.set_workspace(self.config.workspace_dir)

        stored_messages = sanitize_messages_for_api(self.history.messages)
        if stored_messages:
            self.messages = [
                {"role": "system", "content": self._build_system_prompt()}
            ] + stored_messages
        elif stored_role and get_role(self.roles, stored_role):
            self.refresh_system_prompt()

        self._restore_plan_from_history()
        self._restore_model_from_history()
        if stored_messages or self.planner.current:
            self.refresh_system_prompt()

        new_entries = self.history.display[previous_count:]
        return [{"type": item.type, "text": item.text} for item in new_entries]

    def _sanitize_messages_in_place(self) -> None:
        if not self.messages:
            return
        system = self.messages[0]
        rest = sanitize_messages_for_api(self.messages[1:])
        self.messages = [system, *rest]

    def get_display_entries(self) -> list[dict[str, Any]]:
        if self.history is None:
            return []
        return [
            {"type": item.type, "text": item.text}
            for item in self.history.display_items()
        ]

    def has_restored_history(self) -> bool:
        if self.history is None:
            return False
        return bool(self.history.messages or self.history.display)

    def set_channel(self, channel_id: str | None) -> None:
        self.channel_id = channel_id
        self.refresh_system_prompt()

    def set_outbound_attachment_handler(
        self,
        handler: Callable[[str], None] | None,
    ) -> None:
        self._outbound_attachment_handler = handler

    def pop_pending_attachments(self) -> list[str]:
        pending = list(self._pending_attachments)
        self._pending_attachments.clear()
        return pending

    def _available_tools(self) -> list[dict[str, Any]]:
        from agent.tools import AGENT_TOOLS, CHANNEL_TOOLS

        if self.channel_id == "wechat":
            return AGENT_TOOLS + CHANNEL_TOOLS
        return AGENT_TOOLS

    @property
    def current_role(self) -> Role | None:
        return get_role(self.roles, self.current_role_name)

    def _resolve_initial_role(self) -> str:
        if get_role(self.roles, self.config.default_role):
            return self.config.default_role
        if self.roles:
            return self.roles[0].name
        return self.config.default_role

    def _build_system_prompt(self, query: str = "") -> str:
        relevant = self.memory.search(query) if query else self.memory.list_all()[-8:]
        skills_text = build_skills_prompt(self.skills)
        memory_text = build_memory_prompt(relevant)
        role_text = build_role_prompt(self.current_role)
        role_name = self.current_role_name
        plan_text = format_plan_prompt(self.planner.current)
        channel_text = self._build_channel_prompt()

        return f"""你是 {self.config.agent_name}，一个支持 skills、角色切换、长期记忆、规划与执行的助手。
当前 role: {role_name}
工作区: {format_workspace_path(self.workspace_dir)}

## 行为准则
- 严格遵循下方「当前身份」中的风格与偏好。
- 优先遵循 skills 中的专项指令。
- 遇到用户偏好、长期事实、项目约定时，使用 save_memory 写入长期记忆。
- 用户要创建、修改、删除 role 时，必须使用 save_role / delete_role 工具直接写文件，不要只让用户手动粘贴。
- 用户要切换工作区、更换项目目录时，使用 set_workspace / get_workspace（见 workspace-manager skill）。
- 写入记忆或 role 后，在回复中简要告知用户已完成的操作。
- 需要回忆历史信息时，使用 search_memory 或 list_memories。
- 切换 role 后，继续基于已有聊天历史回答，但语气与侧重点应匹配新身份。

## 规划与执行
- 简单问答、单步操作：直接回答或使用单个工具，无需 create_plan。
- 复杂任务（多文件改动、多步排查、需命令验证）：先 create_plan 拆成 3–8 步，再逐步执行。
- 执行每一步时：update_plan_step(in_progress) → 使用 read_file / write_file / list_directory / run_command 等工具完成 → update_plan_step(completed, result=...)。
- 某步失败时标记 failed 并说明原因；可调整后续步骤或给出替代方案。
- 全部完成后调用 complete_plan(summary=...)，再向用户汇总结果。
- 文件与命令操作限定在工作区内；不要访问工作区外路径。
- 截图相关需求遵循 screenshot skill（用 run_command，无专用截图工具）。
{channel_text}
## 当前执行计划
{plan_text}

## 当前身份
{role_text}

## 可用 Skills
{skills_text}

## 相关长期记忆
{memory_text}
"""

    def _deliver_outbound_file(self, path: Path) -> dict[str, Any]:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"文件不存在: {path}")
        rel_path = str(resolved.relative_to(self.workspace_dir))
        path_str = str(resolved)

        if self._outbound_attachment_handler:
            self._outbound_attachment_handler(path_str)
            return {
                "path": rel_path,
                "absolute_path": path_str,
                "sent_to_wechat": True,
                "message": "已发送到微信，请勿重复调用 send_attachment",
            }

        if path_str not in self._pending_attachments:
            self._pending_attachments.append(path_str)
        return {
            "path": rel_path,
            "absolute_path": path_str,
            "sent_to_wechat": False,
            "queued": True,
            "message": "已排队，将在回复完成后发送",
        }

    def _build_channel_prompt(self) -> str:
        if self.channel_id != "wechat":
            return ""
        return """
## 微信消息渠道
- 你正在通过微信与用户对话，可以直接发送图片和文件。
- 发送附件：调用 **send_attachment(path=...)**；成功且 sent_to_wechat=true 时勿重复发送。
- 截图流程见 screenshot skill：run_command 保存图片 → send_attachment 发送。
- 用户发来的图片/文件路径会出现在消息中；图片若模型支持会自动识图。
- **禁止**声称「无法发送文件/附件」——需要发送时使用 send_attachment。
"""

    def refresh_system_prompt(self, query: str = "") -> None:
        if self.messages:
            self.messages[0] = {
                "role": "system",
                "content": self._build_system_prompt(query),
            }

    def reload_skills(self) -> int:
        self.skills = load_skills(self.config.skills_dirs)
        self.refresh_system_prompt()
        return len(self.skills)

    def reload_roles(self) -> int:
        self.roles = load_roles(self.config.roles_dir)
        if not get_role(self.roles, self.current_role_name):
            self.current_role_name = self._resolve_initial_role()
        self.refresh_system_prompt()
        return len(self.roles)

    def switch_role(self, name: str) -> Role:
        role = get_role(self.roles, name)
        if role is None:
            available = ", ".join(r.name for r in self.roles) or "(无)"
            raise ValueError(f"未知 role: {name}。可用: {available}")
        self.current_role_name = role.name
        self.refresh_system_prompt()
        return role

    def _handle_tool_call(
        self,
        name: str,
        arguments: str,
        saved_memories: list[str],
        role_changes: list[str],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        args = json.loads(arguments or "{}")
        _emit(
            on_event,
            {
                "type": "tool_start",
                "name": name,
                "arguments": args,
                "title": format_tool_title(name, args),
                "detail": format_tool_detail(name, args),
            },
        )

        def finish(payload: str) -> str:
            _emit(
                on_event,
                {
                    "type": "tool_end",
                    "name": name,
                    "arguments": args,
                    "result": payload,
                    "block": format_tool_result_block(name, args, payload),
                },
            )
            return payload

        if name == "save_memory":
            item = self.memory.add(args.get("content", ""), args.get("tags"))
            saved_memories.append(item.content)
            return finish(json.dumps({"ok": True, "saved": item.content}, ensure_ascii=False))

        if name == "search_memory":
            items = self.memory.search(
                args.get("query", ""),
                limit=int(args.get("limit") or 8),
            )
            return finish(
                json.dumps(
                [{"content": i.content, "tags": i.tags} for i in items],
                ensure_ascii=False,
                )
            )

        if name == "list_memories":
            items = self.memory.list_all()
            return finish(
                json.dumps(
                [{"content": i.content, "tags": i.tags} for i in items],
                ensure_ascii=False,
                )
            )

        if name == "get_workspace":
            info = self.get_workspace_info()
            return finish(json.dumps({"ok": True, **info}, ensure_ascii=False))

        if name == "set_workspace":
            try:
                resolved = self.set_workspace(args.get("path", ""))
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            info = self.get_workspace_info()
            return finish(
                json.dumps(
                    {
                        "ok": True,
                        "path": str(resolved),
                        "display": format_workspace_path(resolved),
                        "is_default": info["is_default"],
                    },
                    ensure_ascii=False,
                )
            )

        if name == "save_role":
            try:
                role = save_role_file(
                    self.config.roles_dir,
                    args.get("name", ""),
                    args.get("title", ""),
                    args.get("description", ""),
                    args.get("body", ""),
                )
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))

            self.reload_roles()
            switch_to = args.get("switch_to", True)
            if switch_to is not False:
                self.switch_role(role.name)

            role_changes.append(f"{role.title} ({role.name}) → {role.path}")
            return finish(
                json.dumps(
                {
                    "ok": True,
                    "name": role.name,
                    "title": role.title,
                    "path": str(role.path),
                    "switched": switch_to is not False,
                },
                ensure_ascii=False,
                )
            )

        if name == "delete_role":
            try:
                role = delete_role_file(self.config.roles_dir, args.get("name", ""))
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))

            self.reload_roles()
            role_changes.append(f"已删除 {role.title} ({role.name})")
            return finish(
                json.dumps(
                {"ok": True, "deleted": role.name},
                ensure_ascii=False,
                )
            )

        if name == "list_roles":
            roles = [
                {
                    "name": role.name,
                    "title": role.title,
                    "description": role.description,
                    "path": str(role.path),
                    "current": role.name == self.current_role_name,
                }
                for role in self.roles
            ]
            return finish(json.dumps(roles, ensure_ascii=False))

        if name == "create_plan":
            try:
                plan = self.planner.create(
                    args.get("title", ""),
                    args.get("goal", ""),
                    args.get("steps") or [],
                )
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            self.refresh_system_prompt()
            if on_event:
                on_event({"type": "plan_updated", "plan": plan.to_dict()})
            self._sync_plan_to_history()
            return finish(json.dumps({"ok": True, "plan": plan.to_dict()}, ensure_ascii=False))

        if name == "update_plan_step":
            try:
                step = self.planner.update_step(
                    args.get("step_id", ""),
                    args.get("status", ""),
                    args.get("result", ""),
                )
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            plan = self.planner.current
            self.refresh_system_prompt()
            if plan and on_event:
                on_event(
                    {
                        "type": "plan_step_updated",
                        "plan": plan.to_dict(),
                        "step_id": step.id,
                    }
                )
            self._sync_plan_to_history()
            return finish(json.dumps({"ok": True, "step": step.to_dict()}, ensure_ascii=False))

        if name == "complete_plan":
            try:
                plan = self.planner.complete(args.get("summary", ""))
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            self.refresh_system_prompt()
            if on_event:
                on_event({"type": "plan_completed", "plan": plan.to_dict()})
            self._sync_plan_to_history()
            return finish(json.dumps({"ok": True, "plan": plan.to_dict()}, ensure_ascii=False))

        if name == "get_plan":
            plan = self.planner.current
            payload = plan.to_dict() if plan else None
            return finish(json.dumps({"ok": True, "plan": payload}, ensure_ascii=False))

        if name == "read_file":
            try:
                content = self.executor.read_file(args.get("path", ""))
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return finish(json.dumps({"ok": True, "content": content}, ensure_ascii=False))

        if name == "write_file":
            try:
                rel_path = self.executor.write_file(
                    args.get("path", ""),
                    args.get("content", ""),
                )
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return finish(json.dumps({"ok": True, "path": rel_path}, ensure_ascii=False))

        if name == "list_directory":
            try:
                entries = self.executor.list_directory(args.get("path") or ".")
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return finish(json.dumps({"ok": True, "entries": entries}, ensure_ascii=False))

        if name == "run_command":
            try:
                result = self.executor.run_command(
                    args.get("command", ""),
                    cwd=args.get("cwd"),
                )
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            except subprocess.TimeoutExpired:
                return finish(
                    json.dumps(
                    {
                        "ok": False,
                        "error": f"命令超时（>{self.config.command_timeout}s）",
                    },
                    ensure_ascii=False,
                    )
                )
            return finish(json.dumps({"ok": True, **result}, ensure_ascii=False))

        if name == "send_attachment":
            if self.channel_id != "wechat" and not self._outbound_attachment_handler:
                return finish(
                    json.dumps(
                        {"ok": False, "error": "send_attachment 仅在微信渠道可用"},
                        ensure_ascii=False,
                    )
                )
            try:
                target = self.executor.resolve_file(args.get("path", ""))
            except ValueError as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            try:
                delivery = self._deliver_outbound_file(target)
            except Exception as exc:
                return finish(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return finish(json.dumps({"ok": True, **delivery}, ensure_ascii=False))

        return finish(json.dumps({"ok": False, "error": f"未知工具: {name}"}, ensure_ascii=False))

    def chat(
        self,
        user_input: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        user_content: str | list[dict[str, Any]] | None = None,
    ) -> str:
        with self._chat_lock:
            return self._chat_locked(user_input, on_event, user_content=user_content)

    @property
    def model_factory(self):
        """Lazily-built per-profile LLM engine factory."""
        factory = getattr(self, "_model_factory", None)
        if factory is None:
            from agent.lc_llm import ChatModelFactory

            factory = ChatModelFactory(self.config)
            self._model_factory = factory
        return factory

    def _chat_locked(
        self,
        user_input: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        user_content: str | list[dict[str, Any]] | None = None,
    ) -> str:
        self.refresh_system_prompt(user_input)
        content: str | list[dict[str, Any]] = (
            user_content if user_content is not None else user_input
        )
        self.messages.append({"role": "user", "content": content})
        self._sanitize_messages_in_place()

        saved_memories: list[str] = []
        role_changes: list[str] = []

        _emit(on_event, {"type": "turn_start"})

        graph = getattr(self, "_chat_graph", None)
        if graph is None:
            from agent.graph_chat import build_chat_graph

            graph = build_chat_graph(self)
            self._chat_graph = graph
        state = {
            "messages": self.messages,
            "user_query": user_input,
            "saved_memories": saved_memories,
            "role_changes": role_changes,
            "step_count": 0,
            "on_event": on_event,
        }
        result = graph.invoke(
            state,
            config={
                "recursion_limit": max(8, self.config.max_chat_steps * 2 + 4),
            },
        )
        # The graph mutates self.messages in place (dict-native state); keep the
        # returned list authoritative in case LangGraph replaced the reference.
        self.messages = result["messages"]
        saved_memories = result.get("saved_memories", saved_memories)
        role_changes = result.get("role_changes", role_changes)

        reply = ""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                reply = msg.get("content") or ""
                break

        notes: list[str] = []
        if saved_memories:
            notes.extend(f"【已写入长期记忆】{content}" for content in saved_memories)
        if role_changes:
            notes.extend(f"【已更新 role】{change}" for change in role_changes)
        if notes:
            block = "\n".join(notes)
            reply = f"{reply}\n\n{block}" if reply.strip() else block

        _emit(on_event, {"type": "turn_end", "reply": reply})
        self._persist_messages()
        return reply


    def reset(self) -> None:
        self._pending_attachments.clear()
        self._outbound_attachment_handler = None
        self.messages = [{"role": "system", "content": self._build_system_prompt()}]
        if self.history is not None:
            workspace = self.history.workspace_dir
            active_plan = self.history.active_plan
            current_model = self.history.current_model
            self.history.clear()
            self.history.workspace_dir = workspace
            self.history.active_plan = active_plan
            self.history.current_model = current_model
            self.history.touch_metadata()
            self._restore_plan_from_history()
            self._restore_model_from_history()
        self.refresh_system_prompt()
