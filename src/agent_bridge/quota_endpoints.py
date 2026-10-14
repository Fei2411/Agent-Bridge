"""Eligibility for account quota: only the CLI's default first-party service.

These checks read configuration, never credential files. Keep the same check
before cached results and direct provider calls so custom services cannot reuse
an official account's token or an earlier successful quota reading.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_bridge.claude_meta import apply_claude_gateway_env, claude_config_home
from agent_bridge.codex_exec import default_codex_home
from agent_bridge.config import AgentConfig
from agent_bridge.grok_observe import grok_home
from agent_bridge.kimi_observe import kimi_home

KIMI_API = "https://api.kimi.com/coding/v1"
KIMI_AUTH = "https://auth.kimi.com"
GROK_API = "https://cli-chat-proxy.grok.com/v1"
GROK_AUTH = "https://auth.x.ai"
GROK_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
GROK_AUTH_SCOPE = f"{GROK_AUTH}::{GROK_CLIENT_ID}"
CUSTOM_ENDPOINT = "quota lookup is not supported for custom endpoints"


def official_url(value: Any, expected: str) -> bool:
    """Compare the full endpoint, including path; host suffixes are not trust."""
    if not isinstance(value, str):
        return False
    actual, default = urlsplit(value.strip()), urlsplit(expected)
    return (
        actual.scheme.lower() == "https"
        and actual.hostname == default.hostname
        and actual.port in (None, 443)
        and not actual.username and not actual.password
        and not actual.query and not actual.fragment
        and actual.path.rstrip("/") == default.path.rstrip("/")
    )


def _object(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("configuration table is not an object")
    return value


def _read(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return _object(json.loads(text) if path.suffix == ".json" else tomllib.loads(text))


def _custom(values: Mapping[str, Any], defaults: Mapping[str, str]) -> bool:
    return any(values.get(key) and not official_url(values[key], default) for key, default in defaults.items())


def _option(args: list[str], *flags: str) -> str | None:
    result = None
    for index, arg in enumerate(args):
        for flag in flags:
            if arg == flag:
                if index + 1 == len(args):
                    raise ValueError("missing CLI option value")
                result = args[index + 1]
            elif arg.startswith(flag + "="):
                result = arg[len(flag) + 1:]
            elif len(flag) == 2 and arg.startswith(flag) and len(arg) > 2:
                result = arg[2:]
    return result


def _codex(cfg: AgentConfig, env: Mapping[str, str]) -> str | None:
    if _custom(env, {"OPENAI_BASE_URL": "https://api.openai.com/v1"}):
        return CUSTOM_ENDPOINT
    if any(arg.split("=", 1)[0] in ("--oss", "--local-provider") for arg in cfg.command):
        return CUSTOM_ENDPOINT
    override = _option(cfg.command, "-c", "--config")
    if override is not None or cfg.config_overrides:
        # CLI overrides are arbitrary TOML paths. Do not build a second Codex
        # configuration resolver merely to guess which account they select.
        return "quota lookup is unsupported with Codex CLI configuration overrides"
    profile = _option(cfg.command, "-p", "--profile")
    for path in (default_codex_home(env) / "config.toml", Path(cfg.cwd or Path.cwd()) / ".codex" / "config.toml"):
        config = dict(_read(path))
        selected = profile or config.get("profile")
        if selected:
            profiles = _object(config.get("profiles"))
            if selected in profiles:
                config.update(_object(profiles[selected]))
            elif path.parent == default_codex_home(env):
                return "quota lookup cannot verify the selected Codex profile"
        if config.get("model_provider", "openai") != "openai":
            return CUSTOM_ENDPOINT
        if _custom(config, {"chatgpt_base_url": "https://chatgpt.com/backend-api"}):
            return CUSTOM_ENDPOINT
        provider = _object(_object(config.get("model_providers")).get("openai"))
        if _custom(provider, {"base_url": "https://api.openai.com/v1"}):
            return CUSTOM_ENDPOINT
    return None


def _kimi(cfg: AgentConfig, env: Mapping[str, str]) -> str | None:
    if _custom(env, {
        "KIMI_CODE_BASE_URL": KIMI_API, "KIMI_BASE_URL": KIMI_API,
        "KIMI_CODE_OAUTH_HOST": KIMI_AUTH, "KIMI_OAUTH_HOST": KIMI_AUTH,
        "KIMI_MODEL_BASE_URL": KIMI_API,
    }):
        return CUSTOM_ENDPOINT
    if env.get("KIMI_MODEL_NAME"):
        return "quota lookup is unsupported for an environment-defined Kimi API-key model"
    if _option(cfg.command, "--config", "--config-file") is not None:
        return "quota lookup is unsupported with Kimi CLI configuration overrides"
    home = kimi_home(Path(env["KIMI_CODE_HOME"]) if env.get("KIMI_CODE_HOME") else None)
    config = _read(home / "config.toml")
    selected = _option(cfg.command, "-m", "--model") or config.get("default_model")
    if not selected:
        return None if not config.get("providers") else "quota lookup cannot verify the selected Kimi provider"
    model = _object(_object(config.get("models")).get(selected))
    if _custom(model, {"base_url": KIMI_API}):
        return CUSTOM_ENDPOINT
    provider_name = model.get("provider")
    if not provider_name:
        return None if str(selected).startswith("kimi-code/") else "quota lookup cannot verify the selected Kimi provider"
    provider = _object(_object(config.get("providers")).get(provider_name))
    provider_env = _object(provider.get("env"))
    base = provider.get("base_url") or provider_env.get("KIMI_BASE_URL")
    if provider.get("type") not in (None, "kimi") or not official_url(base, KIMI_API):
        return CUSTOM_ENDPOINT
    oauth = _object(provider.get("oauth"))
    if provider.get("api_key") or provider_env.get("KIMI_API_KEY") or not oauth:
        return "quota lookup is unsupported for a Kimi API-key provider"
    if oauth.get("storage") != "file" or oauth.get("key") not in ("kimi-code", "oauth/kimi-code"):
        return "quota lookup is unsupported for a non-default Kimi OAuth credential slot"
    return None


def _grok(cfg: AgentConfig, env: Mapping[str, str]) -> str | None:
    if _custom(env, {
        "GROK_CLI_CHAT_PROXY_BASE_URL": GROK_API, "GROK_XAI_API_BASE_URL": "https://api.x.ai/v1",
        "GROK_OIDC_ISSUER": GROK_AUTH, "GROK_OAUTH2_ISSUER": GROK_AUTH,
    }) or env.get("GROK_MODELS_BASE_URL") or env.get("GROK_MODELS_LIST_URL"):
        return CUSTOM_ENDPOINT
    if any(env.get(key) for key in ("GROK_AUTH_PROVIDER_COMMAND", "GROK_MANAGED_CONFIG_URL")):
        return "quota lookup is unsupported for externally managed Grok authentication/configuration"
    if str(env.get("GROK_LOCAL_AUTH", "")).lower() not in ("", "0", "false"):
        return CUSTOM_ENDPOINT
    if any(env.get(key) and env[key] != GROK_CLIENT_ID for key in ("GROK_OIDC_CLIENT_ID", "GROK_OAUTH2_CLIENT_ID")):
        return "quota lookup is unsupported for a non-default Grok OAuth client"
    if _option(cfg.command, "--config", "--config-file", "--base-url", "--cli-chat-proxy-base-url") is not None:
        return "quota lookup is unsupported with Grok CLI endpoint/configuration overrides"
    home = grok_home(Path(env["GROK_HOME"]) if env.get("GROK_HOME") else None)
    config = _read(home / "config.toml")
    endpoints = _object(config.get("endpoints"))
    if _custom(endpoints, {"cli_chat_proxy_base_url": GROK_API, "xai_api_base_url": "https://api.x.ai/v1"}):
        return CUSTOM_ENDPOINT
    if any(endpoints.get(key) for key in ("models_base_url", "models_list_url", "models_endpoint", "managed_config_url")):
        return CUSTOM_ENDPOINT
    models = _object(config.get("models"))
    selected = _option(cfg.command, "-m", "--model") or models.get("default")
    if selected:
        model = _object(_object(config.get("model")).get(selected))
        if model.get("base_url"):
            return CUSTOM_ENDPOINT
    grok_config = _object(config.get("grok_com_config"))
    if grok_config.get("auth_provider_command") or grok_config.get("preferred_method") == "api_key":
        return "quota lookup is unsupported for external/API-key Grok authentication"
    for kind in ("oidc", "oauth2"):
        auth = _object(grok_config.get(kind))
        if _custom(auth, {"issuer": GROK_AUTH}):
            return CUSTOM_ENDPOINT
        if auth.get("client_id") and auth["client_id"] != GROK_CLIENT_ID:
            return "quota lookup is unsupported for a non-default Grok OAuth client"
    return None


def _claude(cfg: AgentConfig, env: Mapping[str, str]) -> str | None:
    if _option(cfg.command, "--settings", "--settings-sources") is not None:
        return "quota lookup is unsupported with Claude CLI settings overrides"
    resolved: dict[str, Any] = {}
    home = claude_config_home(env)
    project = Path(cfg.cwd or Path.cwd()) / ".claude"
    for path in (home / "settings.json", project / "settings.json", project / "settings.local.json"):
        resolved.update(_object(_read(path).get("env")))
    resolved.update(env)
    if _custom(resolved, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}):
        return CUSTOM_ENDPOINT
    if any(str(resolved.get(key, "")).lower() in ("1", "true") for key in (
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    )):
        return CUSTOM_ENDPOINT
    if _custom(apply_claude_gateway_env(resolved), {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}):
        return CUSTOM_ENDPOINT
    return None


def quota_block_reason(cfg: AgentConfig, env: Mapping[str, str]) -> str | None:
    """Return a safe reason without URLs/secrets; unverified config fails closed."""
    product = "codex" if cfg.protocol == "codex" else cfg.name
    try:
        # A fallback can select a different endpoint/profile when the primary
        # executable is missing. Reject ambiguous candidates before cache access.
        for command in (cfg.command, *cfg.fallback_commands):
            candidate = cfg.model_copy(update={"command": command})
            reason = None
            if product == "codex":
                reason = _codex(candidate, env)
            elif product == "kimi":
                reason = _kimi(candidate, env)
            elif product == "grok":
                reason = _grok(candidate, env)
            elif product == "claude":
                reason = _claude(candidate, env)
            if reason:
                return reason
    except (OSError, UnicodeError, ValueError, TypeError):
        return "quota lookup cannot verify the CLI endpoint configuration"
    return None
