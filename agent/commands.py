from __future__ import annotations

from agent.assign import ASSIGN_COMMAND_HELP, AssignStart, parse_assign_start
from agent.core import Agent
from agent.discuss import DISCUSS_COMMAND_HELP, DiscussStart, parse_discuss_start
from agent.memory import MEMORY_COMMAND_HELP, format_memory_list
from agent.model_settings import MODEL_COMMAND_HELP
from agent.planner import PLAN_COMMAND_HELP, format_plan
from agent.roles import ROLE_COMMAND_HELP, format_roles_list
from agent.skills import format_skills_list
from agent.workspace import WORKSPACE_COMMAND_HELP, format_workspace_path

SLASH_COMMANDS: dict[str, str] = {
    "/exit": "退出程序",
    "/quit": "退出程序",
    "/reset": "清空当前会话（含对话历史），长期记忆保留",
    "/skills": "重新加载并列出所有 skills",
    "/role": "列出全部 role，或切换身份",
    "/role help": "显示 role 命令帮助",
    "/role reload": "重新加载 roles 目录",
    "/memory": "列出全部长期记忆",
    "/memory help": "显示记忆命令帮助",
    "/memory add": "手动添加记忆（后接内容）",
    "/memory delete": "删除指定序号记忆",
    "/plan": "查看当前计划与步骤进度",
    "/plan help": "显示计划命令帮助",
    "/plan clear": "取消当前计划",
    "/workspace": "查看或切换工作区",
    "/workspace help": "显示工作区命令帮助",
    "/workspace default": "恢复 .env 默认工作区",
    "/model": "查看或切换 LLM 模型",
    "/model help": "显示 model 命令帮助",
    "/model list": "列出预设模型",
    "/model default": "恢复 .env 默认模型",
    "/discuss": "多模型讨论并产出方案",
    "/discuss help": "显示 discuss 命令帮助",
    "/discuss stop": "停止进行中的讨论",
    "/discuss status": "查看讨论状态",
    "/assign": "多模型顺序流水线",
    "/assign help": "显示 assign 命令帮助",
    "/assign stop": "停止分工任务",
    "/assign status": "查看分工状态",
}


def filter_commands(prefix: str, agent: Agent | None = None) -> list[tuple[str, str]]:
    text = prefix.strip()
    if not text.startswith("/"):
        return []

    static = [(cmd, desc) for cmd, desc in SLASH_COMMANDS.items() if cmd.startswith(text)]

    if agent is None:
        return _sort_command_matches(static, text)

    merged: dict[str, str] = {cmd: desc for cmd, desc in static if cmd.startswith(text)}

    if text == "/role" or text.startswith("/role "):
        role_query = text[len("/role ") :].strip() if text.startswith("/role ") else ""
        for role in agent.roles:
            cmd = f"/role {role.name}"
            if not cmd.startswith(text):
                continue
            if role_query and not role.name.startswith(role_query):
                continue
            desc = role.description or role.title or role.name
            merged[cmd] = f"切换为: {desc}"

    if text == "/model" or text.startswith("/model "):
        model_query = text[len("/model ") :].strip() if text.startswith("/model ") else ""
        for name in agent.list_model_presets():
            cmd = f"/model {name}"
            if not cmd.startswith(text):
                continue
            if model_query and not name.startswith(model_query):
                continue
            profile = agent.get_model_profile(name)
            merged[cmd] = profile.label()

    return _sort_command_matches(list(merged.items()), text)


def _sort_command_matches(
    matches: list[tuple[str, str]], text: str
) -> list[tuple[str, str]]:
    def rank(item: tuple[str, str]) -> tuple[int, int, str]:
        cmd = item[0]
        if cmd == text:
            return (0, len(cmd), cmd)
        return (1, len(cmd), cmd)

    return sorted(matches, key=rank)


def execute_memory_command(agent: Agent, user_input: str) -> str | None:
    if user_input == "/memory":
        return format_memory_list(agent.memory.list_all())

    if user_input == "/memory help":
        return MEMORY_COMMAND_HELP

    if not user_input.startswith("/memory "):
        return None

    rest = user_input[len("/memory ") :].strip()
    if rest.startswith("add "):
        content = rest[4:].strip()
        if not content:
            return "用法: /memory add 要记住的内容"
        try:
            item = agent.memory.add(content, tags=["manual"])
            agent.refresh_system_prompt()
            return f"已写入长期记忆: {item.content}"
        except ValueError as exc:
            return f"错误: {exc}"

    if rest.startswith("delete ") or rest.startswith("del "):
        idx_text = rest.split(maxsplit=1)[1].strip() if " " in rest else ""
        if not idx_text:
            return "用法: /memory delete 序号"
        try:
            idx = int(idx_text)
            item = agent.memory.remove(idx)
            agent.refresh_system_prompt()
            return f"已删除长期记忆 [{idx}]: {item.content}"
        except ValueError:
            return "序号必须是数字，例如: /memory delete 0"
        except IndexError:
            return f"序号 {idx_text} 不存在，先用 /memory 查看列表。"

    return MEMORY_COMMAND_HELP


def execute_role_command(agent: Agent, user_input: str) -> str | None:
    if user_input == "/role":
        return format_roles_list(
            agent.roles,
            agent.current_role_name,
            agent.config.roles_dir,
        )

    if user_input == "/role help":
        return ROLE_COMMAND_HELP

    if user_input == "/role reload":
        count = agent.reload_roles()
        role = agent.current_role
        title = role.title if role else agent.current_role_name
        return f"已重新加载 {count} 个 role，当前: {title} ({agent.current_role_name})"

    if not user_input.startswith("/role "):
        return None

    name = user_input[len("/role ") :].strip()
    if not name:
        return ROLE_COMMAND_HELP

    try:
        role = agent.switch_role(name)
    except ValueError as exc:
        return str(exc)

    return (
        f"已切换 role 为 [{role.name}] {role.title}。"
        "聊天历史已保留，后续回复将按新身份与偏好进行。"
    )


def execute_plan_command(agent: Agent, user_input: str) -> str | None:
    if user_input == "/plan":
        return format_plan(agent.planner.current)

    if user_input == "/plan help":
        return PLAN_COMMAND_HELP

    if user_input == "/plan clear":
        cancelled = agent.planner.cancel()
        agent.refresh_system_prompt()
        agent._sync_plan_to_history()
        if cancelled is None:
            return "当前没有进行中的计划。"
        return f"已取消计划: {cancelled.title}"

    if user_input.startswith("/plan "):
        return PLAN_COMMAND_HELP

    return None


def execute_workspace_command(agent: Agent, user_input: str) -> str | None:
    if user_input == "/workspace":
        info = agent.get_workspace_info()
        default_note = (
            "（当前为默认）"
            if info["is_default"]
            else f"（默认: {info['default_display']}）"
        )
        return f"当前工作区: {info['display']}\n路径: {info['path']}\n{default_note}"

    if user_input == "/workspace help":
        return WORKSPACE_COMMAND_HELP

    if user_input == "/workspace default":
        try:
            resolved = agent.set_workspace("default")
        except ValueError as exc:
            return f"错误: {exc}"
        return f"已恢复默认工作区: {format_workspace_path(resolved)}"

    if not user_input.startswith("/workspace "):
        return None

    path = user_input[len("/workspace ") :].strip()
    if not path:
        return WORKSPACE_COMMAND_HELP

    try:
        resolved = agent.set_workspace(path)
    except ValueError as exc:
        return f"错误: {exc}"

    return (
        f"已切换工作区: {format_workspace_path(resolved)}\n"
        f"路径: {resolved}\n"
        "后续 read_file / write_file / run_command 均在此目录下执行。"
    )


def execute_model_command(agent: Agent, user_input: str) -> str | None:
    if user_input == "/model":
        info = agent.get_model_info()
        default_note = (
            "（当前为默认）"
            if info["is_default"]
            else f"（默认: {info['default_model']} @ {info['default_base_url']}）"
        )
        thinking = "开启" if info["thinking_mode"] else "关闭"
        locked = "（.env 固定）" if info["thinking_mode_locked"] else ""
        return (
            f"当前配置: {info['profile']}\n"
            f"模型: {info['model']}\n"
            f"API: {info['base_url']}\n"
            f"{default_note}\n"
            f"思考模式: {thinking}{locked}"
        )

    if user_input == "/model help":
        return MODEL_COMMAND_HELP

    if user_input == "/model list":
        current = agent.current_profile_name
        lines: list[str] = []
        last_provider: str | None = None
        for name in agent.list_model_presets():
            profile = agent.get_model_profile(name)
            if profile.provider and profile.provider != last_provider:
                if lines:
                    lines.append("")
                lines.append(f"[{profile.provider}]")
                last_provider = profile.provider
            marker = "  ← 当前" if name == current else ""
            lines.append(f"  {name:<22} {profile.model} @ {profile.base_url}{marker}")
        return "可切换模型:\n" + "\n".join(lines)

    if user_input == "/model default":
        try:
            profile = agent.set_model("default")
        except ValueError as exc:
            return f"错误: {exc}"
        return f"已恢复默认配置: {profile.label()}"

    if not user_input.startswith("/model "):
        return None

    name = user_input[len("/model ") :].strip()
    if not name:
        return MODEL_COMMAND_HELP

    try:
        profile = agent.set_model(name)
    except ValueError as exc:
        return f"错误: {exc}"

    thinking = "开启" if agent.llm.thinking_mode else "关闭"
    return f"已切换: {profile.name}\n{profile.label()}\n思考模式: {thinking}"


def execute_discuss_command(agent: Agent, user_input: str) -> str | DiscussStart | None:
    if user_input in {"/discuss", "/discuss help"}:
        return DISCUSS_COMMAND_HELP

    if user_input == "/discuss status":
        status = agent.format_discuss_status()
        return status or "当前没有进行中的讨论。"

    if user_input == "/discuss stop":
        if agent.request_discuss_stop("用户发送 /discuss stop"):
            return "已请求停止讨论，等待当前发言结束后汇总各模型方案…"
        return "当前没有进行中的讨论。"

    if not user_input.startswith("/discuss"):
        return None

    if user_input.startswith("/discuss "):
        if agent.task_session_active():
            return "已有讨论或分工在进行中。发送 /discuss stop 或 /assign stop 可停止。"
        parsed = parse_discuss_start(user_input, agent.resolve_profile_ref_strict)
        if isinstance(parsed, str):
            return parsed
        return parsed

    return DISCUSS_COMMAND_HELP


def execute_assign_command(agent: Agent, user_input: str) -> str | AssignStart | None:
    if user_input in {"/assign", "/assign help"}:
        return ASSIGN_COMMAND_HELP

    if user_input == "/assign status":
        status = agent.format_assign_status()
        return status or "当前没有进行中的流水线。"

    if user_input == "/assign stop":
        if agent.request_assign_stop("用户发送 /assign stop"):
            return "已请求停止流水线，等待当前步骤结束后汇总…"
        return "当前没有进行中的流水线。"

    if not user_input.startswith("/assign"):
        return None

    if user_input.startswith("/assign "):
        if agent.task_session_active():
            return "已有讨论或分工在进行中。发送 /discuss stop 或 /assign stop 可停止。"
        parsed = parse_assign_start(user_input, agent.resolve_profile_ref_strict)
        if isinstance(parsed, str):
            return parsed
        return parsed

    return ASSIGN_COMMAND_HELP


def execute_command(agent: Agent, user_input: str) -> tuple[str | DiscussStart | AssignStart | None, bool]:
    text = user_input.strip()
    if not text:
        return None, False

    if text.lower() in {"exit", "quit", "q", "/exit", "/quit"}:
        return None, True

    if text == "/reset":
        agent.reset()
        role = agent.current_role
        title = role.title if role else agent.current_role_name
        return f"会话已重置。当前 role: {title} ({agent.current_role_name})", False

    if text == "/skills":
        agent.reload_skills()
        return format_skills_list(agent.skills, agent.config.skills_dirs), False

    role_result = execute_role_command(agent, text)
    if role_result is not None:
        return role_result, False

    memory_result = execute_memory_command(agent, text)
    if memory_result is not None:
        return memory_result, False

    plan_result = execute_plan_command(agent, text)
    if plan_result is not None:
        return plan_result, False

    workspace_result = execute_workspace_command(agent, text)
    if workspace_result is not None:
        return workspace_result, False

    model_result = execute_model_command(agent, text)
    if model_result is not None:
        return model_result, False

    discuss_result = execute_discuss_command(agent, text)
    if discuss_result is not None:
        return discuss_result, False

    assign_result = execute_assign_command(agent, text)
    if assign_result is not None:
        return assign_result, False

    if text.startswith("/"):
        return (
            "未知命令。输入 / 可实时补全，或输入 /role help、/memory help、/plan help、/workspace help、/model help、/discuss help、/assign help。",
            False,
        )

    return None, False
