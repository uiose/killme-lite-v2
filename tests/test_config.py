from pathlib import Path

from config import load_config
from llm import LLMClient


ROOT = Path(__file__).resolve().parents[1]


def test_env_file_can_be_loaded(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=test-key",
                "OPENAI_BASE_URL=https://example.test/v1",
                "OPENAI_MODEL=test-model",
                "OPENAI_REASONING_EFFORT=xhigh",
                "DEEPSEEK_API_KEY=deepseek-test-key",
                "KILLME_DATA_DIR=./runtime-data",
                "KILLME_DB_PATH=./runtime-data/test.sqlite",
                "KILLME_LLM_TIMEOUT=42",
                "KILLME_MOCK_LLM=1",
                "KILLME_LOG_LEVEL=debug",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path, environ={})

    assert config.openai_api_key == "test-key"
    assert config.openai_base_url == "https://example.test/v1"
    assert config.openai_model == "test-model"
    assert config.openai_reasoning_effort == "xhigh"
    assert config.llm_mode == "openai"
    assert config.model_providers["deepseek"].api_key == "deepseek-test-key"
    assert config.model_providers["deepseek"].model == "deepseek-v4-pro"
    assert config.data_dir == (tmp_path / "runtime-data").resolve()
    assert config.db_path == (tmp_path / "runtime-data" / "test.sqlite").resolve()
    assert config.llm_timeout == 42
    assert config.mock_llm is True
    assert config.log_level == "DEBUG"


def test_codex_style_config_toml_can_be_loaded(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                'model = "gpt-5.5"',
                'model_reasoning_effort = "xhigh"',
                'llm_mode = "openai_then_deepseek_per_command"',
                "",
                "[model_providers.openai]",
                'base_url = "https://example.test/v1"',
                'wire_api = "chat"',
                "",
                "[model_providers.deepseek]",
                'base_url = "https://api.deepseek.com"',
                'wire_api = "chat"',
                'model = "deepseek-v4-pro"',
                'model_reasoning_effort = "high"',
                'thinking = "enabled"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path, environ={})

    assert config.openai_api_key == "test-key"
    assert config.openai_base_url == "https://example.test/v1"
    assert config.openai_model == "gpt-5.5"
    assert config.openai_reasoning_effort == "xhigh"
    assert config.llm_mode == "openai_then_deepseek_per_command"
    assert config.model_providers["deepseek"].base_url == "https://api.deepseek.com"
    assert config.model_providers["deepseek"].model == "deepseek-v4-pro"
    assert config.model_providers["deepseek"].reasoning_effort == "high"
    assert config.model_providers["deepseek"].thinking == "enabled"


def test_process_env_overrides_config_toml(tmp_path):
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                'model = "gpt-5.5"',
                'model_reasoning_effort = "xhigh"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(
        tmp_path,
        environ={
            "OPENAI_MODEL": "test-env-model",
            "OPENAI_REASONING_EFFORT": "low",
            "KILLME_LLM_MODE": "deepseek",
            "DEEPSEEK_MODEL": "deepseek-env-model",
        },
    )

    assert config.openai_model == "test-env-model"
    assert config.openai_reasoning_effort == "low"
    assert config.llm_mode == "deepseek"
    assert config.model_providers["deepseek"].model == "deepseek-env-model"


def test_mock_mode_does_not_require_api_key(tmp_path):
    config = load_config(tmp_path, environ={"KILLME_MOCK_LLM": "1"})
    client = LLMClient(ROOT, config=config)

    output = client.call_agent(
        "executioner",
        state={"core_claim": "test idea", "current_major_question": "why fail?"},
        recent_turns=[],
        current_task="attack it",
    )

    assert output["role"] == "executioner"
    assert output["state_patch"]["strongest_attack"]
