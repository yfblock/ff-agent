import os

from agent.model_settings import (
    ModelProfile,
    build_model_profiles,
    find_model_profile,
    parse_model_entries,
    resolve_model_profile,
    suggest_profile_names,
)


def test_parse_multiple_models_in_profile() -> None:
    entries = parse_model_entries("chat:deepseek-chat,reasoner:deepseek-reasoner")
    assert entries == (("chat", "deepseek-chat"), ("reasoner", "deepseek-reasoner"))


def test_build_multi_model_profile(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("MODEL_PROFILE_"):
            monkeypatch.delenv(key, raising=False)

    profiles = build_model_profiles(
        default_model="deepseek-chat",
        default_base_url="https://api.deepseek.com",
        default_api_key="sk-default",
        profiles_spec="deepseek",
        legacy_presets=None,
        environ={
            "MODEL_PROFILE_deepseek": "chat:deepseek-chat,reasoner:deepseek-reasoner|https://api.deepseek.com",
        },
    )
    by_name = {profile.name: profile for profile in profiles}
    assert "deepseek/chat" in by_name
    assert "deepseek/reasoner" in by_name
    assert by_name["deepseek/chat"].model == "deepseek-chat"
    assert by_name["deepseek/reasoner"].base_url == "https://api.deepseek.com"


def test_single_model_profile_keeps_provider_name() -> None:
    profiles = build_model_profiles(
        default_model="deepseek-chat",
        default_base_url="https://api.deepseek.com",
        default_api_key="sk-default",
        profiles_spec="deepseek",
        legacy_presets=None,
        environ={
            "MODEL_PROFILE_deepseek": "deepseek-chat|https://api.deepseek.com",
        },
    )
    by_name = {profile.name: profile for profile in profiles}
    assert "deepseek" in by_name
    assert "deepseek/chat" not in by_name


def test_resolve_provider_and_model() -> None:
    profiles = (
        ModelProfile("default", "deepseek-chat", "https://api.deepseek.com", "sk-a"),
        ModelProfile(
            "deepseek/chat",
            "deepseek-chat",
            "https://api.deepseek.com",
            "sk-a",
            provider="deepseek",
        ),
        ModelProfile(
            "deepseek/reasoner",
            "deepseek-reasoner",
            "https://api.deepseek.com",
            "sk-a",
            provider="deepseek",
        ),
    )
    default = profiles[0]
    assert resolve_model_profile("deepseek/reasoner", profiles=profiles, default=default).model == "deepseek-reasoner"
    assert resolve_model_profile("deepseek", profiles=profiles, default=default).name == "deepseek/chat"
    assert resolve_model_profile("deepseek-reasoner", profiles=profiles, default=default).name == "deepseek/reasoner"


def test_resolve_multiple_model_matches_prefers_non_default() -> None:
    profiles = (
        ModelProfile("default", "gpt-4o", "https://default.example.com", "sk-a"),
        ModelProfile(
            "openai",
            "gpt-4o",
            "https://api.openai.com/v1",
            "sk-openai",
            provider="openai",
        ),
    )
    default = profiles[0]
    resolved = resolve_model_profile("gpt-4o", profiles=profiles, default=default)
    assert resolved.base_url == "https://api.openai.com/v1"


def test_is_adhoc_model_profile() -> None:
    from agent.model_settings import is_adhoc_model_profile

    profiles = (
        ModelProfile("default", "deepseek-chat", "https://api.deepseek.com", "sk-a"),
        ModelProfile("openai", "gpt-4o", "https://api.openai.com/v1", "sk-b", provider="openai"),
    )
    adhoc = resolve_model_profile("unknown-model", profiles=profiles, default=profiles[0])
    assert is_adhoc_model_profile("unknown-model", adhoc, profiles=profiles)
    assert not is_adhoc_model_profile("openai", profiles[1], profiles=profiles)


def test_find_model_profile_case_insensitive() -> None:
    profiles = (
        ModelProfile("default", "deepseek-chat", "https://api.deepseek.com", "sk-a"),
        ModelProfile(
            "mimo/mimo-v2.5",
            "mimo-v2.5",
            "https://token-plan.example.com/v1",
            "sk-b",
            provider="mimo",
        ),
    )
    found = find_model_profile("mimo/MiMo-V2.5", profiles=profiles)
    assert found is not None
    assert found.name == "mimo/mimo-v2.5"
    assert found.base_url == "https://token-plan.example.com/v1"


def test_suggest_profile_names_for_typo() -> None:
    profiles = (
        ModelProfile("default", "deepseek-chat", "https://api.deepseek.com", "sk-a"),
        ModelProfile(
            "mimo/mimo-v2.5",
            "mimo-v2.5",
            "https://token-plan.example.com/v1",
            "sk-b",
            provider="mimo",
        ),
        ModelProfile(
            "mimo/mimo-v2.5-pro",
            "mimo-v2.5-pro",
            "https://token-plan.example.com/v1",
            "sk-b",
            provider="mimo",
        ),
    )
    suggestions = suggest_profile_names("mimo/mino-v2.5", profiles=profiles)
    assert "mimo/mimo-v2.5" in suggestions
