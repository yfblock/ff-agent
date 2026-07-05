from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from agent.model_settings import ModelProfile, build_model_profiles, default_thinking_mode


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    model: str
    model_profiles: tuple[ModelProfile, ...]
    thinking_mode_locked: bool
    agent_name: str
    skills_dirs: tuple[Path, ...]
    roles_dir: Path
    default_role: str
    memory_path: Path
    max_memory_items: int
    chat_history_path: Path
    sessions_dir: Path
    max_chat_messages: int
    persist_chat_history: bool
    shared_chat_session: bool
    auto_start_gateway: bool
    auto_start_web: bool
    web_host: str
    web_port: int
    thinking_mode: bool
    workspace_dir: Path
    allow_shell: bool
    max_plan_steps: int
    command_timeout: int
    max_discuss_rounds: int
    max_assign_turns: int
    max_assign_tool_steps: int
    max_assign_rounds: int
    max_chat_steps: int


@dataclass(frozen=True)
class WeChatOfficialConfig:
    enabled: bool
    app_id: str
    app_secret: str
    token: str
    encoding_aes_key: str | None
    webhook_path: str
    allowed_openids: tuple[str, ...]


@dataclass(frozen=True)
class WeChatILinkConfig:
    enabled: bool
    credentials_path: Path
    bot_agent: str
    allowed_user_ids: tuple[str, ...]
    cdn_base_url: str
    media_dir: Path


@dataclass(frozen=True)
class ChannelConfig:
    gateway_host: str
    gateway_port: int
    enabled: tuple[str, ...]
    wechat_mode: str
    wechat_official: WeChatOfficialConfig
    wechat_ilink: WeChatILinkConfig


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_skills_dirs(
    skills_dir: str | None = None,
    skills_dirs: str | None = None,
) -> tuple[Path, ...]:
    raw_parts: list[str] = []
    for value in (skills_dir, skills_dirs):
        if value and value.strip():
            raw_parts.extend(re.split(r"[:,]", value))

    if not raw_parts:
        return (Path("./skills").expanduser(),)

    dirs: list[Path] = []
    seen: set[str] = set()
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        path = Path(part).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)

    return tuple(dirs) if dirs else (Path("./skills").expanduser(),)


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value or not value.strip():
        return ()
    parts: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,:\s]+", value.strip()):
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        parts.append(item)
    return tuple(parts)


def load_channel_config(env_path: str | Path | None = None) -> ChannelConfig:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    enabled = _parse_csv(os.getenv("CHANNELS", ""))
    wechat_requested = "wechat" in enabled or _parse_bool(os.getenv("WECHAT_ENABLED"), False)
    wechat_mode = (os.getenv("WECHAT_MODE", "ilink") or "ilink").strip().lower()

    wechat_official_enabled = wechat_requested and wechat_mode == "official"
    wechat_ilink_enabled = wechat_requested and wechat_mode != "official"

    credentials_path = Path(
        os.getenv("WECHAT_ILINK_CREDENTIALS", "./data/channels/wechat_ilink.json")
    ).expanduser()

    wechat_official = WeChatOfficialConfig(
        enabled=wechat_official_enabled,
        app_id=os.getenv("WECHAT_APP_ID", "").strip(),
        app_secret=os.getenv("WECHAT_APP_SECRET", "").strip(),
        token=os.getenv("WECHAT_TOKEN", "").strip(),
        encoding_aes_key=os.getenv("WECHAT_ENCODING_AES_KEY", "").strip() or None,
        webhook_path=os.getenv("WECHAT_WEBHOOK_PATH", "/channels/wechat/webhook").strip()
        or "/channels/wechat/webhook",
        allowed_openids=_parse_csv(os.getenv("WECHAT_ALLOWED_OPENIDS")),
    )

    wechat_ilink = WeChatILinkConfig(
        enabled=wechat_ilink_enabled,
        credentials_path=credentials_path,
        bot_agent=os.getenv("WECHAT_BOT_AGENT", "ff-agent/0.1.0").strip() or "ff-agent/0.1.0",
        allowed_user_ids=_parse_csv(os.getenv("WECHAT_ALLOWED_USER_IDS")),
        cdn_base_url=os.getenv(
            "WECHAT_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c"
        ).strip()
        or "https://novac2c.cdn.weixin.qq.com/c2c",
        media_dir=Path(
            os.getenv("WECHAT_MEDIA_DIR", "./data/channels/wechat/media")
        ).expanduser(),
    )

    normalized_enabled: list[str] = []
    if wechat_official.enabled or wechat_ilink.enabled:
        normalized_enabled.append("wechat")

    return ChannelConfig(
        gateway_host=os.getenv("CHANNEL_GATEWAY_HOST", "0.0.0.0").strip() or "0.0.0.0",
        gateway_port=int(os.getenv("CHANNEL_GATEWAY_PORT", "8787")),
        enabled=tuple(normalized_enabled),
        wechat_mode=wechat_mode,
        wechat_official=wechat_official,
        wechat_ilink=wechat_ilink,
    )


def load_config(
    env_path: str | Path | None = None,
    *,
    require_api_key: bool = True,
) -> Config:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if require_api_key:
        if not api_key:
            raise ValueError("OPENAI_API_KEY 未设置，请在 .env 中配置")
        if api_key in {"your-api-key", "sk-your-api-key", "changeme"}:
            raise ValueError(
                "OPENAI_API_KEY 仍是占位值，请在 .env 中填入真实的 DeepSeek API Key"
            )

    skills_dirs = parse_skills_dirs(
        skills_dir=os.getenv("SKILLS_DIR"),
        skills_dirs=os.getenv("SKILLS_DIRS"),
    )
    memory_path = Path(os.getenv("MEMORY_PATH", "./data/memory.json")).expanduser()
    chat_history_path = Path(
        os.getenv("CHAT_HISTORY_PATH", "./data/chat_history.json")
    ).expanduser()
    sessions_dir = Path(os.getenv("SESSIONS_DIR", "./data/sessions")).expanduser()
    max_chat_messages = int(os.getenv("MAX_CHAT_MESSAGES", "200"))
    persist_chat_history = _parse_bool(os.getenv("PERSIST_CHAT_HISTORY"), default=True)
    shared_chat_session = _parse_bool(os.getenv("SHARED_CHAT_SESSION"), default=True)
    auto_start_gateway = _parse_bool(os.getenv("AUTO_START_GATEWAY"), default=True)
    auto_start_web = _parse_bool(os.getenv("AUTO_START_WEB"), default=True)
    web_host = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    web_port = int(os.getenv("WEB_PORT", "8080"))
    roles_dir = Path(os.getenv("ROLES_DIR", "./roles")).expanduser()
    default_role = os.getenv("DEFAULT_ROLE", "default").strip() or "default"
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    thinking_env = os.getenv("THINKING_MODE")
    thinking_mode_locked = thinking_env is not None and thinking_env.strip() != ""
    thinking_mode = _parse_bool(
        thinking_env,
        default=default_thinking_mode(model),
    )
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    model_profiles = build_model_profiles(
        default_model=model,
        default_base_url=base_url,
        default_api_key=api_key,
        profiles_spec=os.getenv("MODEL_PROFILES"),
        legacy_presets=os.getenv("OPENAI_MODEL_PRESETS"),
    )
    workspace_dir = Path(os.getenv("WORKSPACE_DIR", ".")).expanduser().resolve()
    allow_shell = _parse_bool(os.getenv("ALLOW_SHELL"), default=True)
    max_plan_steps = int(os.getenv("MAX_PLAN_STEPS", "12"))
    command_timeout = int(os.getenv("COMMAND_TIMEOUT", "60"))
    max_discuss_rounds = int(os.getenv("MAX_DISCUSS_ROUNDS", "10"))
    max_assign_turns = int(os.getenv("MAX_ASSIGN_TURNS", "24"))
    max_assign_tool_steps = int(os.getenv("MAX_ASSIGN_TOOL_STEPS", "40"))
    max_assign_rounds = int(os.getenv("MAX_ASSIGN_ROUNDS", "3"))
    max_chat_steps = int(os.getenv("MAX_CHAT_STEPS", "40"))

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        model_profiles=model_profiles,
        thinking_mode_locked=thinking_mode_locked,
        agent_name=os.getenv("AGENT_NAME", "ff-agent").strip(),
        skills_dirs=skills_dirs,
        roles_dir=roles_dir,
        default_role=default_role,
        memory_path=memory_path,
        max_memory_items=int(os.getenv("MAX_MEMORY_ITEMS", "50")),
        chat_history_path=chat_history_path,
        sessions_dir=sessions_dir,
        max_chat_messages=max_chat_messages,
        persist_chat_history=persist_chat_history,
        shared_chat_session=shared_chat_session,
        auto_start_gateway=auto_start_gateway,
        auto_start_web=auto_start_web,
        web_host=web_host,
        web_port=web_port,
        thinking_mode=thinking_mode,
        workspace_dir=workspace_dir,
        allow_shell=allow_shell,
        max_plan_steps=max_plan_steps,
        command_timeout=command_timeout,
        max_discuss_rounds=max_discuss_rounds,
        max_assign_turns=max_assign_turns,
        max_assign_tool_steps=max_assign_tool_steps,
        max_assign_rounds=max_assign_rounds,
        max_chat_steps=max_chat_steps,
    )
