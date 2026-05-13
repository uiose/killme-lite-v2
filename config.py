from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}
LLM_MODE_ALIASES = {
    "chatgpt": "openai",
    "chatgpt_then_deepseek": "openai_then_deepseek_per_command",
    "chatgpt_then_deepseek_per_command": "openai_then_deepseek_per_command",
    "openai_then_deepseek": "openai_then_deepseek_per_command",
}
VALID_LLM_MODES = {"openai", "deepseek", "openai_then_deepseek_per_command"}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str
    wire_api: str
    thinking: str = ""


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    data_dir: Path
    db_path: Path
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    openai_reasoning_effort: str
    llm_mode: str
    model_providers: dict[str, ProviderConfig]
    llm_timeout: int
    mock_llm: bool
    log_level: str
    json_response_format: bool


def load_config(base_dir: Path, environ: Optional[Mapping[str, str]] = None) -> AppConfig:
    """Load runtime configuration from `config.toml`, `.env`, and process env.

    Process environment wins over project `config.toml`, which wins over `.env`.
    New OPENAI_* names are preferred, while older KILLME_* aliases remain
    supported to avoid breaking existing local setups.
    """
    base_dir = Path(base_dir).resolve()
    process_env = dict(os.environ if environ is None else environ)
    env_path = base_dir / ".env"
    toml_config = _read_toml_config(base_dir / "config.toml")

    file_env = _read_dotenv(env_path)
    merged_env = {**file_env, **process_env}

    def pick(*keys: str, config_key: str = "", default: str = "") -> str:
        for key in keys:
            value = process_env.get(key)
            if value not in (None, ""):
                return str(value)
        if config_key:
            value = toml_config.get(config_key)
            if value not in (None, ""):
                return str(value)
        for key in keys:
            value = merged_env.get(key)
            if value not in (None, ""):
                return str(value)
        return default

    def pick_provider(
        provider_name: str,
        provider_key: str,
        *env_keys: str,
        top_config_key: str = "",
        default: str = "",
    ) -> str:
        for key in env_keys:
            value = process_env.get(key)
            if value not in (None, ""):
                return str(value)

        providers = toml_config.get("model_providers")
        provider = providers.get(provider_name, {}) if isinstance(providers, dict) else {}
        if isinstance(provider, dict):
            value = provider.get(provider_key)
            if value not in (None, ""):
                return str(value)

        if top_config_key:
            value = toml_config.get(top_config_key)
            if value not in (None, ""):
                return str(value)

        for key in env_keys:
            value = merged_env.get(key)
            if value not in (None, ""):
                return str(value)
        return default

    data_dir = _resolve_path(base_dir, pick("KILLME_DATA_DIR", config_key="data_dir", default="./data"))
    db_path = _resolve_path(
        base_dir,
        pick("KILLME_DB_PATH", config_key="db_path", default=str(data_dir / "killme.sqlite")),
    )

    openai_provider = ProviderConfig(
        name="openai",
        api_key=pick_provider(
            "openai",
            "api_key",
            "OPENAI_API_KEY",
            "KILLME_LLM_API_KEY",
            "KILLME_API_KEY",
        ),
        base_url=pick_provider(
            "openai",
            "base_url",
            "OPENAI_BASE_URL",
            "KILLME_LLM_BASE_URL",
            "KILLME_API_BASE",
            top_config_key="base_url",
            default="https://api.openai.com/v1",
        ).rstrip("/"),
        model=pick_provider(
            "openai",
            "model",
            "OPENAI_MODEL",
            "KILLME_LLM_MODEL",
            "KILLME_MODEL",
            top_config_key="model",
            default="gpt-5.5",
        ),
        reasoning_effort=pick_provider(
            "openai",
            "model_reasoning_effort",
            "OPENAI_REASONING_EFFORT",
            "KILLME_REASONING_EFFORT",
            top_config_key="model_reasoning_effort",
            default="xhigh",
        )
        .strip()
        .lower(),
        wire_api=pick_provider("openai", "wire_api", default="chat").strip().lower(),
    )
    deepseek_provider = ProviderConfig(
        name="deepseek",
        api_key=pick_provider("deepseek", "api_key", "DEEPSEEK_API_KEY"),
        base_url=pick_provider(
            "deepseek",
            "base_url",
            "DEEPSEEK_BASE_URL",
            default="https://api.deepseek.com",
        ).rstrip("/"),
        model=pick_provider(
            "deepseek",
            "model",
            "DEEPSEEK_MODEL",
            default="deepseek-v4-pro",
        ),
        reasoning_effort=pick_provider(
            "deepseek",
            "model_reasoning_effort",
            "DEEPSEEK_REASONING_EFFORT",
            default="high",
        )
        .strip()
        .lower(),
        wire_api=pick_provider("deepseek", "wire_api", default="chat").strip().lower(),
        thinking=pick_provider(
            "deepseek",
            "thinking",
            "DEEPSEEK_THINKING",
            default="enabled",
        )
        .strip()
        .lower(),
    )
    model_providers = {"openai": openai_provider, "deepseek": deepseek_provider}
    llm_mode = _normalize_llm_mode(pick("KILLME_LLM_MODE", config_key="llm_mode", default="openai"))

    return AppConfig(
        base_dir=base_dir,
        data_dir=data_dir,
        db_path=db_path,
        openai_api_key=openai_provider.api_key,
        openai_base_url=openai_provider.base_url,
        openai_model=openai_provider.model,
        openai_reasoning_effort=openai_provider.reasoning_effort,
        llm_mode=llm_mode,
        model_providers=model_providers,
        llm_timeout=_parse_int(
            pick("KILLME_LLM_TIMEOUT", config_key="llm_timeout", default="120"),
            default=120,
            minimum=5,
        ),
        mock_llm=_parse_bool(
            pick("KILLME_MOCK_LLM", "KILLME_MOCK", config_key="mock_llm", default="0"),
            default=False,
        ),
        log_level=pick("KILLME_LOG_LEVEL", config_key="log_level", default="INFO").upper(),
        json_response_format=_parse_bool(
            pick("KILLME_JSON_RESPONSE_FORMAT", config_key="json_response_format", default="1"),
            default=True,
        ),
    )


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _parse_int(value: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _parse_bool(value: str, default: bool) -> bool:
    lowered = str(value).strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    return default


def _normalize_llm_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = LLM_MODE_ALIASES.get(normalized, normalized)
    if normalized not in VALID_LLM_MODES:
        valid = ", ".join(sorted(VALID_LLM_MODES))
        raise ValueError(f"Invalid llm_mode {value!r}; expected one of: {valid}")
    return normalized


def _read_toml_config(path: Path) -> dict[str, Any]:
    """Read the Codex-style TOML subset this app supports."""
    if not path.exists():
        return {}

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML config {path}: {exc}") from exc

    values: dict[str, Any] = {}

    provider_name = raw.get("model_provider")
    if not isinstance(provider_name, str) or not provider_name:
        provider_name = "openai"

    providers: dict[str, dict[str, Any]] = {}
    raw_providers = raw.get("model_providers")
    if isinstance(raw_providers, dict):
        for name, provider in raw_providers.items():
            if not isinstance(name, str) or not isinstance(provider, dict):
                continue
            provider_values: dict[str, Any] = {}
            for key in (
                "api_key",
                "base_url",
                "wire_api",
                "model",
                "model_reasoning_effort",
                "reasoning_effort",
                "thinking",
            ):
                _set_scalar(provider_values, key, provider.get(key))
            if "reasoning_effort" in provider_values and "model_reasoning_effort" not in provider_values:
                provider_values["model_reasoning_effort"] = provider_values["reasoning_effort"]
            providers[name] = provider_values

        provider = providers.get(provider_name)
        if isinstance(provider, dict):
            _set_scalar(values, "base_url", provider.get("base_url"))
    values["model_providers"] = providers

    _set_scalar(values, "base_url", raw.get("openai_base_url"))
    for key in (
        "llm_mode",
        "model",
        "model_reasoning_effort",
        "data_dir",
        "db_path",
        "llm_timeout",
        "mock_llm",
        "log_level",
        "json_response_format",
    ):
        _set_scalar(values, key, raw.get(key))

    return values


def _set_scalar(values: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, (str, int, float, bool)) and value != "":
        values[key] = value


def _read_dotenv(path: Path) -> dict[str, str]:
    """Small fallback parser for tests and early bootstrap before dependencies exist."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values
