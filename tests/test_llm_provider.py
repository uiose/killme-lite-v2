import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from config import load_config
from llm import LLMClient, LLMError


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, content: str):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(
            {"choices": [{"message": {"content": self.content}}]},
        ).encode("utf-8")


def make_config(tmp_path, mode: str):
    return load_config(
        tmp_path,
        environ={
            "KILLME_LLM_MODE": mode,
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_BASE_URL": "https://openai.test/v1",
            "OPENAI_MODEL": "openai-model",
            "OPENAI_REASONING_EFFORT": "xhigh",
            "DEEPSEEK_API_KEY": "deepseek-key",
            "DEEPSEEK_BASE_URL": "https://deepseek.test",
            "DEEPSEEK_MODEL": "deepseek-v4-pro",
            "DEEPSEEK_REASONING_EFFORT": "high",
            "DEEPSEEK_THINKING": "enabled",
        },
    )


def request_body(req: urllib.request.Request):
    assert req.data is not None
    return json.loads(req.data.decode("utf-8"))


def test_deepseek_mode_uses_deepseek_provider(tmp_path, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        del timeout
        calls.append((req.full_url, request_body(req)))
        return FakeResponse("deepseek-ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LLMClient(ROOT, config=make_config(tmp_path, "deepseek"))

    output = client.chat_completion([{"role": "user", "content": "hello"}])

    assert output == "deepseek-ok"
    assert calls[0][0] == "https://deepseek.test/chat/completions"
    assert calls[0][1]["model"] == "deepseek-v4-pro"
    assert calls[0][1]["thinking"] == {"type": "enabled"}
    assert "temperature" not in calls[0][1]


def test_per_command_timeout_switches_to_deepseek_then_resets(tmp_path, monkeypatch):
    calls = []
    openai_timeouts = 0

    def fake_urlopen(req, timeout):
        nonlocal openai_timeouts
        del timeout
        body = request_body(req)
        provider = "deepseek" if "deepseek.test" in req.full_url else "openai"
        calls.append((provider, body["model"]))
        if provider == "openai" and openai_timeouts == 0:
            openai_timeouts += 1
            raise TimeoutError("timed out")
        return FakeResponse(f"{provider}-ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LLMClient(ROOT, config=make_config(tmp_path, "openai_then_deepseek_per_command"))

    client.begin_command()
    try:
        assert client.chat_completion([{"role": "user", "content": "first"}]) == "deepseek-ok"
        assert calls == [("openai", "openai-model"), ("deepseek", "deepseek-v4-pro")]
        assert client.last_fallback_used is True

        assert client.chat_completion([{"role": "user", "content": "second"}]) == "deepseek-ok"
        assert calls[-1] == ("deepseek", "deepseek-v4-pro")
    finally:
        client.end_command()

    client.begin_command()
    try:
        assert client.chat_completion([{"role": "user", "content": "third"}]) == "openai-ok"
        assert calls[-1] == ("openai", "openai-model")
    finally:
        client.end_command()


def test_http_errors_do_not_fallback_to_deepseek(tmp_path, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        del timeout
        provider = "deepseek" if "deepseek.test" in req.full_url else "openai"
        calls.append(provider)
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "bad request",
            {},
            io.BytesIO(b"bad request"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LLMClient(ROOT, config=make_config(tmp_path, "openai_then_deepseek_per_command"))

    client.begin_command()
    try:
        with pytest.raises(LLMError, match="LLM HTTP error 400"):
            client.chat_completion([{"role": "user", "content": "bad"}])
    finally:
        client.end_command()

    assert calls == ["openai"]
