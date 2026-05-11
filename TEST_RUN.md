# Test Run

Environment: mock LLM / local SQLite / uv-managed venv

Commands executed:

```bash
uv run python -m py_compile config.py main.py llm.py router.py state.py storage.py merger.py validation.py
uv run pytest -q
uv run ruff check .
KILLME_MOCK_LLM=1 python3 main.py --db /tmp/killme-smoke.sqlite --script <smoke-script>
```

Results:

```text
uv run pytest -q
16 passed

uv run ruff check .
All checks passed!

mock smoke flow
passed
```

Notes:

- Tests use deterministic mock LLM and cover runtime control flow, validation, merger, close guard, and clone failure recovery.
- Real LLM deliberation quality still requires manual smoke testing against an OpenAI-compatible endpoint.
