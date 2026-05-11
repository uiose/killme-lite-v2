from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

try:  # python-dotenv is declared in pyproject, but keep source imports resilient.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only used when dependencies are not installed yet.
    load_dotenv = None  # type: ignore[assignment]


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    data_dir: Path
    db_path: Path
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    openai_reasoning_effort: str
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

    if environ is None and load_dotenv is not None:
        load_dotenv(env_path, override=False)
        process_env = dict(os.environ)

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

    data_dir = _resolve_path(base_dir, pick("KILLME_DATA_DIR", config_key="data_dir", default="./data"))
    db_path = _resolve_path(
        base_dir,
        pick("KILLME_DB_PATH", config_key="db_path", default=str(data_dir / "killme.sqlite")),
    )

    return AppConfig(
        base_dir=base_dir,
        data_dir=data_dir,
        db_path=db_path,
        openai_api_key=pick("OPENAI_API_KEY", "KILLME_LLM_API_KEY", "KILLME_API_KEY"),
        openai_base_url=pick(
            "OPENAI_BASE_URL",
            "KILLME_LLM_BASE_URL",
            "KILLME_API_BASE",
            config_key="base_url",
            default="https://api.openai.com/v1",
        ).rstrip("/"),
        openai_model=pick(
            "OPENAI_MODEL",
            "KILLME_LLM_MODEL",
            "KILLME_MODEL",
            config_key="model",
            default="gpt-5.5",
        ),
        openai_reasoning_effort=pick(
            "OPENAI_REASONING_EFFORT",
            "KILLME_REASONING_EFFORT",
            config_key="model_reasoning_effort",
            default="xhigh",
        ).strip().lower(),
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

    providers = raw.get("model_providers")
    if isinstance(providers, dict):
        provider = providers.get(provider_name)
        if isinstance(provider, dict):
            _set_scalar(values, "base_url", provider.get("base_url"))

    _set_scalar(values, "base_url", raw.get("openai_base_url"))
    for key in (
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
