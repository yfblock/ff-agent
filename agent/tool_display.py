from __future__ import annotations

import json
from typing import Any


def try_parse_tool_args(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    except json.JSONDecodeError:
        return {"_raw": text}


def format_tool_title(name: str, args: dict[str, Any]) -> str:
    if name == "run_command":
        command = str(args.get("command") or args.get("_raw") or "").strip()
        return f"Shell · {command or '…'}"
    if name == "read_file":
        path = str(args.get("path") or args.get("_raw") or "").strip()
        return f"Read · {path or '…'}"
    if name == "write_file":
        path = str(args.get("path") or "").strip()
        return f"Write · {path or '…'}"
    if name == "list_directory":
        path = str(args.get("path") or ".").strip() or "."
        return f"List · {path}"
    if name == "create_plan":
        title = str(args.get("title") or "计划").strip()
        return f"Plan · {title}"
    if name == "update_plan_step":
        step_id = str(args.get("step_id") or "").strip()
        status = str(args.get("status") or "").strip()
        return f"Plan step · {step_id} → {status or '…'}"
    if name == "complete_plan":
        return "Plan · complete"
    if name == "save_memory":
        return "Memory · save"
    if name == "search_memory":
        query = str(args.get("query") or "").strip()
        return f"Memory · search {query or '…'}"
    if name == "list_memories":
        return "Memory · list"
    if name == "list_roles":
        return "Role · list"
    if name == "save_role":
        role_name = str(args.get("name") or args.get("title") or "").strip()
        return f"Role · save {role_name or '…'}"
    if name == "send_attachment":
        path = str(args.get("path") or "").strip()
        return f"Send · {path or '…'}"
    if name == "get_workspace":
        return "Workspace · 查看"
    if name == "set_workspace":
        path = str(args.get("path") or "").strip()
        return f"Workspace · {path or '…'}"
    return name.replace("_", " ")


def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(已截断)"


def format_tool_detail(name: str, args: dict[str, Any]) -> str:
    if name == "run_command":
        command = str(args.get("command") or args.get("_raw") or "").strip()
        cwd = str(args.get("cwd") or "").strip()
        if cwd:
            return f"$ {command}\ncwd: {cwd}"
        return f"$ {command}"

    if name == "write_file":
        path = str(args.get("path") or "").strip()
        content = str(args.get("content") or "")
        if not content and args.get("_raw"):
            return str(args["_raw"])
        preview = _truncate(content, 600)
        if preview:
            return f"{path}\n```\n{preview}\n```"
        return path

    if name == "read_file":
        return str(args.get("path") or args.get("_raw") or "")

    if name == "list_directory":
        return str(args.get("path") or ".")

    if name == "create_plan":
        goal = str(args.get("goal") or "").strip()
        steps = args.get("steps") or []
        lines = [goal] if goal else []
        for index, step in enumerate(steps[:8], start=1):
            if isinstance(step, dict):
                desc = str(step.get("description") or step.get("id") or "").strip()
                lines.append(f"{index}. {desc}")
        return "\n".join(lines)

    if args.get("_raw"):
        return _truncate(str(args["_raw"]), 400)

    if args:
        return _truncate(json.dumps(args, ensure_ascii=False, indent=2), 400)
    return ""


def format_tool_result_block(name: str, args: dict[str, Any], result: str) -> str:
    title = format_tool_title(name, args)
    detail = format_tool_detail(name, args)
    summary = summarize_tool_result(name, result)
    lines = [f"▎{title}"]
    if detail:
        lines.append(detail)
    if summary:
        lines.append(summary)
    return "\n".join(lines)


def summarize_tool_result(name: str, result: str) -> str:
    try:
        payload: Any = json.loads(result or "{}")
    except json.JSONDecodeError:
        return _truncate(result, 300)

    if isinstance(payload, list):
        if name in {"search_memory", "list_memories"}:
            if not payload:
                return "✓ 无匹配记忆" if name == "search_memory" else "✓ 记忆为空"
            preview = "; ".join(
                _truncate(str(item.get("content") if isinstance(item, dict) else item), 40)
                for item in payload[:3]
            )
            suffix = f" 等 {len(payload)} 条" if len(payload) > 3 else f" ({len(payload)} 条)"
            return f"✓ {preview}{suffix}"
        if name == "list_roles":
            if not payload:
                return "✓ 无 role"
            names = [
                str(item.get("name") if isinstance(item, dict) else item)
                for item in payload[:5]
            ]
            suffix = f" 等 {len(payload)} 个" if len(payload) > 5 else ""
            return f"✓ {', '.join(names)}{suffix} ({len(payload)} 个)"
        return f"✓ {len(payload)} 项"

    if not isinstance(payload, dict):
        return "✓ 完成"

    if not payload.get("ok", True) and payload.get("error"):
        return f"✗ {payload['error']}"

    if name == "run_command":
        code = payload.get("exit_code", 0)
        stdout = str(payload.get("stdout") or "").strip()
        stderr = str(payload.get("stderr") or "").strip()
        mark = "✓" if code == 0 else "✗"
        lines = [f"{mark} exit {code}"]
        if stdout:
            lines.append(_truncate(stdout, 400))
        if stderr:
            lines.append(_truncate(stderr, 200))
        return "\n".join(lines)

    if name == "read_file":
        content = str(payload.get("content") or "")
        if content:
            return _truncate(content, 400)
        return "✓ 已读取"

    if name == "write_file":
        path = payload.get("path") or ""
        return f"✓ 已写入 {path}" if path else "✓ 已写入"

    if name == "list_directory":
        entries = payload.get("entries") or []
        if entries:
            preview = ", ".join(str(item) for item in entries[:12])
            suffix = " …" if len(entries) > 12 else ""
            return f"✓ {preview}{suffix}"
        return "✓ 空目录"

    if name in {"create_plan", "update_plan_step", "complete_plan", "get_plan"}:
        return "✓ 计划已更新"

    if name == "save_memory":
        saved = payload.get("saved")
        return f"✓ 已保存: {saved}" if saved else "✓ 已保存"

    if name == "save_role":
        role_name = payload.get("name") or ""
        return f"✓ role: {role_name}" if role_name else "✓ role 已更新"

    if name == "send_attachment":
        path = payload.get("path") or ""
        if payload.get("sent_to_wechat"):
            return f"✓ 已发送到微信 {path}" if path else "✓ 已发送到微信"
        if payload.get("queued"):
            return f"✓ 已排队发送 {path}" if path else "✓ 已排队发送"
        return f"✓ 已排队发送 {path}" if path else "✓ 附件已排队"

    if name == "set_workspace":
        display = payload.get("display") or payload.get("path") or ""
        return f"✓ 已切换: {display}"

    if name == "get_workspace":
        display = payload.get("display") or payload.get("path") or ""
        suffix = " (默认)" if payload.get("is_default") else ""
        return f"✓ {display}{suffix}"

    return "✓ 完成"
