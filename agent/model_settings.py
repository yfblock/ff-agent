from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from typing import Mapping


def default_thinking_mode(model: str) -> bool:
    model_lower = model.lower()
    return "reasoner" in model_lower


MODEL_COMMAND_HELP = """模型命令:
  /model              查看当前模型与服务商
  /model list         列出可切换的模型配置
  /model <名称>       切换到指定配置（名称见 list）
  /model default      恢复 .env 默认配置

配置示例（`.env`）:
  MODEL_PROFILES=deepseek,openai
  MODEL_PROFILE_deepseek=deepseek-chat,deepseek-reasoner|https://api.deepseek.com
  MODEL_PROFILE_openai=gpt-4o,gpt-4o-mini|https://api.openai.com/v1|sk-openai-key

单个 profile 可配置多个模型（逗号分隔）；可选别名: chat:deepseek-chat,reasoner:deepseek-reasoner
多模型时切换名称为 `profile/别名`，单模型时仍用 profile 名。
格式: `模型[,模型...]|API地址|API密钥`，后两项可省略。
兼容: `OPENAI_MODEL_PRESETS=deepseek-chat,deepseek-reasoner`（默认服务商）"""


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    base_url: str
    api_key: str
    provider: str | None = None

    def label(self) -> str:
        if self.provider and self.provider != self.name:
            return f"[{self.provider}] {self.model} @ {self.base_url}"
        return f"{self.model} @ {self.base_url}"


def parse_model_entries(models_raw: str) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for item in models_raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            alias, model = item.split(":", 1)
            alias = alias.strip()
            model = model.strip()
            if not alias or not model:
                raise ValueError("模型别名格式应为: 别名:模型名")
            entries.append((alias, model))
        else:
            entries.append((item, item))
    if not entries:
        raise ValueError("至少需要一个模型")
    return tuple(entries)


def parse_profile_value(
    raw: str,
    *,
    default_base_url: str,
    default_api_key: str,
) -> tuple[tuple[tuple[str, str], ...], str, str]:
    parts = [part.strip() for part in raw.split("|")]
    if not parts or not parts[0]:
        raise ValueError("配置格式应为: 模型名|API地址|API密钥")
    models = parse_model_entries(parts[0])
    base_url = parts[1] if len(parts) > 1 and parts[1] else default_base_url
    api_key = parts[2] if len(parts) > 2 and parts[2] else default_api_key
    if not base_url:
        raise ValueError("API 地址不能为空")
    if not api_key:
        raise ValueError("API 密钥不能为空")
    return models, base_url, api_key


def build_model_profiles(
    *,
    default_model: str,
    default_base_url: str,
    default_api_key: str,
    profiles_spec: str | None,
    legacy_presets: str | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[ModelProfile, ...]:
    env = environ or os.environ
    profiles: dict[str, ModelProfile] = {}
    provider_order: dict[str, list[str]] = {}

    def add(
        name: str,
        model: str,
        base_url: str,
        api_key: str,
        *,
        provider: str | None = None,
    ) -> None:
        key = name.strip()
        if not key:
            return
        profiles[key] = ModelProfile(
            name=key,
            model=model.strip(),
            base_url=base_url.strip(),
            api_key=api_key.strip(),
            provider=provider,
        )
        if provider:
            provider_order.setdefault(provider, []).append(key)

    add("default", default_model, default_base_url, default_api_key)

    prefix = "MODEL_PROFILE_"
    for env_key, value in env.items():
        if not env_key.startswith(prefix):
            continue
        provider_name = env_key[len(prefix) :].strip()
        if not provider_name or not value.strip():
            continue
        model_entries, base_url, api_key = parse_profile_value(
            value,
            default_base_url=default_base_url,
            default_api_key=default_api_key,
        )
        if len(model_entries) == 1:
            alias, model_id = model_entries[0]
            add(
                provider_name,
                model_id,
                base_url,
                api_key,
                provider=provider_name,
            )
            continue
        for alias, model_id in model_entries:
            add(
                f"{provider_name}/{alias}",
                model_id,
                base_url,
                api_key,
                provider=provider_name,
            )

    for part in _split_csv(legacy_presets):
        if part not in profiles:
            add(part, part, default_base_url, default_api_key)

    ordered: list[ModelProfile] = []
    seen: set[str] = set()

    def append(name: str) -> None:
        profile = profiles.get(name)
        if profile is None or name in seen:
            return
        seen.add(name)
        ordered.append(profile)

    append("default")
    for name in _split_csv(profiles_spec):
        if name in provider_order:
            for profile_name in provider_order[name]:
                append(profile_name)
            continue
        append(name)
    for name in sorted(profiles):
        append(name)

    return tuple(ordered)


def profiles_by_name(profiles: tuple[ModelProfile, ...]) -> dict[str, ModelProfile]:
    return {profile.name: profile for profile in profiles}


def default_profile(profiles: tuple[ModelProfile, ...]) -> ModelProfile:
    if not profiles:
        raise ValueError("未配置任何模型")
    return profiles[0]


def profiles_for_provider(
    profiles: tuple[ModelProfile, ...], provider: str
) -> tuple[ModelProfile, ...]:
    provider_lower = provider.strip().lower()
    return tuple(
        profile
        for profile in profiles
        if profile.provider and profile.provider.lower() == provider_lower
    )


def find_model_profile(
    name: str,
    *,
    profiles: tuple[ModelProfile, ...],
) -> ModelProfile | None:
    text = (name or "").strip()
    if not text:
        return None

    by_name = profiles_by_name(profiles)
    if text in by_name:
        return by_name[text]

    text_lower = text.lower()
    for profile_name, profile in by_name.items():
        if profile_name.lower() == text_lower:
            return profile

    provider_matches = profiles_for_provider(profiles, text)
    if provider_matches:
        return provider_matches[0]

    if "/" in text:
        provider, alias = text.split("/", 1)
        provider_lower = provider.strip().lower()
        alias_lower = alias.strip().lower()
        candidate_lower = f"{provider_lower}/{alias_lower}"
        for profile_name, profile in by_name.items():
            if profile_name.lower() == candidate_lower:
                return profile
        for profile in profiles:
            if not profile.provider or profile.provider.lower() != provider_lower:
                continue
            name_parts = profile.name.split("/", 1)
            if len(name_parts) == 2 and name_parts[1].lower() == alias_lower:
                return profile
            if profile.model.lower() == alias_lower:
                return profile

    matches = [profile for profile in profiles if profile.model == text]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        non_default = [profile for profile in matches if profile.name != "default"]
        if len(non_default) == 1:
            return non_default[0]
        if non_default:
            return non_default[0]

    matches = [profile for profile in profiles if profile.model.lower() == text_lower]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        non_default = [profile for profile in matches if profile.name != "default"]
        if len(non_default) == 1:
            return non_default[0]
        if non_default:
            return non_default[0]

    return None


def suggest_profile_names(
    name: str,
    *,
    profiles: tuple[ModelProfile, ...],
    limit: int = 3,
) -> tuple[str, ...]:
    candidates = [profile.name for profile in profiles if profile.name != "default"]
    if not candidates:
        return ()

    suggestions: list[str] = []
    seen: set[str] = set()
    for match in difflib.get_close_matches(name.strip(), candidates, n=limit, cutoff=0.5):
        if match not in seen:
            seen.add(match)
            suggestions.append(match)

    if "/" in name and len(suggestions) < limit:
        provider_lower = name.split("/", 1)[0].strip().lower()
        for candidate in candidates:
            if not candidate.lower().startswith(f"{provider_lower}/"):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            suggestions.append(candidate)
            if len(suggestions) >= limit:
                break

    return tuple(suggestions[:limit])


def format_unknown_profile_error(
    name: str,
    *,
    profiles: tuple[ModelProfile, ...],
) -> str:
    suggestions = suggest_profile_names(name, profiles=profiles)
    if suggestions:
        hint = f"你是否想用的是: {', '.join(suggestions)}？"
    else:
        hint = "请运行 /model list 查看可用名称。"
    return f"未识别的模型配置「{name.strip()}」。{hint}"


def resolve_model_profile(
    name: str,
    *,
    profiles: tuple[ModelProfile, ...],
    default: ModelProfile,
) -> ModelProfile:
    text = (name or "").strip()
    if not text:
        raise ValueError("模型名称不能为空")
    if text.lower() in {"default", ".env", "reset"}:
        return default

    found = find_model_profile(text, profiles=profiles)
    if found is not None:
        return found

    return ModelProfile(
        name=text,
        model=text,
        base_url=default.base_url,
        api_key=default.api_key,
    )


PROFILE_REF_CURRENT = frozenset({"@current", "current", "."})


def is_profile_ref_current(name: str) -> bool:
    return (name or "").strip().lower() in PROFILE_REF_CURRENT


def is_adhoc_model_profile(
    name: str,
    profile: ModelProfile,
    *,
    profiles: tuple[ModelProfile, ...],
) -> bool:
    text = (name or "").strip()
    if not text or is_profile_ref_current(text):
        return False
    return find_model_profile(text, profiles=profiles) is None


def thinking_mode_for_model(model: str, *, locked: bool, locked_value: bool) -> bool:
    if locked:
        return locked_value
    return default_thinking_mode(model)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    parts: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,:\s]+", raw.strip()):
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        parts.append(item)
    return tuple(parts)
