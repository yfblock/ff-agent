from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from pydantic import BaseModel, Field

from agent.assign import AssignStart
from agent.config import load_config
from agent.discuss import DiscussStart
from agent.errors import format_api_error
from agent.session_hub import SessionHub

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class SettingsUpdate(BaseModel):
    model: str | None = None
    role: str | None = None
    workspace: str | None = None
    thinking_mode: bool | None = None


def _agent(hub: SessionHub):
    return hub.local_agent


def _serialize_settings(agent) -> dict[str, Any]:
    model_info = agent.get_model_info()
    workspace = agent.get_workspace_info()
    role = agent.current_role
    models = []
    for name in agent.list_model_presets():
        profile = agent.get_model_profile(name)
        models.append(
            {
                "name": name,
                "model": profile.model,
                "base_url": profile.base_url,
                "provider": profile.provider or "",
                "current": name == agent.current_profile_name,
            }
        )
    roles = [
        {
            "name": item.name,
            "title": item.title,
            "description": item.description,
            "current": item.name == agent.current_role_name,
        }
        for item in agent.roles
    ]
    return {
        "agent_name": agent.config.agent_name,
        "model": {
            "profile": model_info["profile"],
            "model": model_info["model"],
            "base_url": model_info["base_url"],
            "provider": model_info["provider"],
            "thinking_mode": model_info["thinking_mode"],
            "thinking_mode_locked": model_info["thinking_mode_locked"],
        },
        "role": {
            "name": agent.current_role_name,
            "title": role.title if role else agent.current_role_name,
            "description": role.description if role else "",
        },
        "workspace": workspace,
        "models": models,
        "roles": roles,
        "skills_count": len(agent.skills),
    }


def _refresh_agent_history(agent) -> None:
    if agent._chat_lock.locked():
        return
    agent.sync_from_history()


def _serialize_messages(agent, *, since: int = 0) -> dict[str, Any]:
    _refresh_agent_history(agent)
    entries = agent.get_display_entries()
    start = max(0, since)
    if start >= len(entries):
        return {"entries": [], "total": len(entries)}
    return {"entries": entries[start:], "total": len(entries)}


def _record_user_message(agent, text: str) -> None:
    agent.record_display("user", text)


def _record_assistant_message(agent, text: str) -> None:
    if text.strip():
        agent.record_display("assistant", text)


def _record_system_message(agent, text: str) -> None:
    if text.strip():
        agent.record_display("system", text)


def _handle_command(agent, user_input: str) -> dict[str, Any] | None:
    from agent.commands import execute_command

    result, _should_exit = execute_command(agent, user_input.strip())
    if result is None:
        return None
    if isinstance(result, DiscussStart):
        return {
            "type": "command",
            "reply": "多模型讨论请在 TUI 中使用 /discuss 命令。",
        }
    if isinstance(result, AssignStart):
        return {
            "type": "command",
            "reply": "流水线分工请在 TUI 中使用 /assign 命令。",
        }
    return {"type": "command", "reply": str(result)}


def create_web_app(hub: SessionHub) -> FastAPI:
    app = FastAPI(title="ff-agent", version="0.1.0")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "agent": hub.config.agent_name}

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return _serialize_settings(_agent(hub))

    @app.patch("/api/settings")
    def update_settings(body: SettingsUpdate) -> dict[str, Any]:
        agent = _agent(hub)
        changes: list[str] = []
        try:
            if body.model is not None:
                profile = agent.set_model(body.model.strip())
                changes.append(f"模型: {profile.name}")
            if body.role is not None:
                role = agent.switch_role(body.role.strip())
                if agent.history is not None:
                    agent.history.current_role_name = role.name
                    agent.history.touch_metadata()
                changes.append(f"身份: {role.title}")
            if body.workspace is not None:
                path = agent.set_workspace(body.workspace.strip())
                changes.append(f"工作区: {path}")
            if body.thinking_mode is not None:
                agent.set_thinking_mode(body.thinking_mode)
                label = "开启" if body.thinking_mode else "关闭"
                changes.append(f"思考模式: {label}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "changes": changes,
            "settings": _serialize_settings(agent),
        }

    @app.get("/api/messages")
    def get_messages(since: int = 0) -> dict[str, Any]:
        agent = _agent(hub)
        payload = _serialize_messages(agent, since=since)
        payload["settings"] = _serialize_settings(agent)
        return payload

    @app.post("/api/reset")
    def reset_chat() -> dict[str, Any]:
        agent = _agent(hub)
        agent.reset()
        role = agent.current_role
        title = role.title if role else agent.current_role_name
        message = f"会话已重置。当前 role: {title} ({agent.current_role_name})"
        _record_system_message(agent, message)
        return {
            "ok": True,
            "message": message,
            "entries": [],
            "total": len(agent.get_display_entries()),
        }

    @app.post("/api/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
        agent = _agent(hub)
        text = body.message.strip()
        command = _handle_command(agent, text)
        if command is not None:
            _record_system_message(agent, command["reply"])
            command["total"] = len(agent.get_display_entries())
            return command
        _record_user_message(agent, text)
        try:
            reply = agent.chat(text)
        except OpenAIError as exc:
            raise HTTPException(status_code=502, detail=format_api_error(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        _record_assistant_message(agent, reply)
        history = _serialize_messages(agent)
        return {
            "type": "chat",
            "reply": reply,
            "entries": history["entries"],
            "total": history["total"],
        }

    @app.post("/api/chat/stream")
    async def chat_stream(body: ChatRequest) -> StreamingResponse:
        agent = _agent(hub)
        text = body.message.strip()
        command = _handle_command(agent, text)
        if command is not None:
            _record_system_message(agent, command["reply"])

            async def command_stream():
                payload = json.dumps(command, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'reply': command['reply'], 'total': len(agent.get_display_entries())}, ensure_ascii=False)}\n\n"

            return StreamingResponse(command_stream(), media_type="text/event-stream")

        _record_user_message(agent, text)

        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()

        def worker() -> None:
            try:

                def on_event(event: dict[str, Any]) -> None:
                    event_queue.put(event)

                reply = agent.chat(text, on_event=on_event)
                _record_assistant_message(agent, reply)
                event_queue.put(
                    {
                        "type": "done",
                        "reply": reply,
                        "total": len(agent.get_display_entries()),
                    }
                )
            except OpenAIError as exc:
                event_queue.put({"type": "error", "message": format_api_error(exc)})
            except Exception as exc:
                event_queue.put({"type": "error", "message": str(exc)})
            finally:
                event_queue.put(None)

        threading.Thread(target=worker, daemon=True).start()

        async def event_generator():
            loop = asyncio.get_running_loop()
            while True:
                item = await loop.run_in_executor(None, event_queue.get)
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("type") in {"done", "error"}:
                    break

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


def _web_bind(config_host: str | None, config_port: int | None) -> tuple[str, int]:
    bind_host = (config_host or os.getenv("WEB_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    bind_port = int(config_port or os.getenv("WEB_PORT", "8080"))
    return bind_host, bind_port


def _format_web_url(host: str, port: int) -> str:
    display_host = host
    if host in {"0.0.0.0", "::"}:
        display_host = "127.0.0.1"
    return f"http://{display_host}:{port}/"


def start_web_server_background(
    env_path: str | None,
    hub: SessionHub,
) -> tuple[str, threading.Thread | None]:
    """在后台线程启动 Web 服务，与 TUI 共用 SessionHub。"""
    if not hub.config.auto_start_web:
        return "", None
    try:
        import uvicorn
    except ImportError:
        logger.warning("未安装 fastapi/uvicorn，Web 界面未启动")
        return "", None

    bind_host, bind_port = _web_bind(hub.config.web_host, hub.config.web_port)
    app = create_web_app(hub)
    url = _format_web_url(bind_host, bind_port)

    def _run() -> None:
        try:
            uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")
        except Exception:
            logger.exception("Web 服务后台线程异常退出")

    thread = threading.Thread(target=_run, name="web-server", daemon=True)
    thread.start()
    logger.info("Web 界面已在后台启动: %s", url)
    return url, thread


def run_web_server(
    env_path: str | None = None,
    *,
    hub: SessionHub | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    config = load_config(env_path)
    session_hub = hub or SessionHub(config)
    bind_host, bind_port = _web_bind(host or config.web_host, port or config.web_port)
    app = create_web_app(session_hub)

    import uvicorn

    url = _format_web_url(bind_host, bind_port)
    print(f"Web 界面: {url}")
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
