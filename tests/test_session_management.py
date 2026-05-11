import json
from pathlib import Path

from config import load_config
from router import Router


def make_router(tmp_path: Path) -> Router:
    return Router(
        base_dir=tmp_path,
        db_path=tmp_path / "killme.sqlite",
        config=load_config(tmp_path, environ={"KILLME_MOCK_LLM": "1"}),
    )


def start(router: Router, idea: str) -> str:
    router.handle_line(f"/start {idea}")
    state = router.storage.load_latest_state(router.storage.get_active_session_id())
    assert state is not None
    return state["session_id"]


def test_start_sets_active_session_and_sessions_lists_marker(tmp_path: Path) -> None:
    router = make_router(tmp_path)

    first = start(router, "first idea")
    second = start(router, "second idea")

    output = router.handle_line("/sessions")

    assert router.storage.get_active_session_id() == second
    assert f"| * | {second} |" in output
    assert f"|  | {first} |" in output


def test_use_switches_active_session_and_summary_reads_it(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    first = start(router, "first idea")
    second = start(router, "second idea")

    output = router.handle_line(f"/use {first}")
    summary = router.handle_line("/summary")

    assert router.storage.get_active_session_id() == first
    assert f"已切换 active session：{first}" in output
    assert f"- session_id: {first}" in summary
    assert "first idea" in summary
    assert second not in summary


def test_use_missing_session_is_rejected(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    start(router, "first idea")

    output = router.handle_line("/use session-missing")

    assert "未找到 session：session-missing" in output


def test_export_active_session_writes_json_and_markdown(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    session_id = start(router, "export this idea")
    router.handle_line("/checkpoint")

    output = router.handle_line("/export")
    json_path = tmp_path / "exports" / f"{session_id}.json"
    markdown_path = tmp_path / "exports" / f"{session_id}.md"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert str(json_path) in output
    assert str(markdown_path) in output
    assert data["schema_version"] == 1
    assert data["session"]["session_id"] == session_id
    assert data["latest_state"]["session_id"] == session_id
    assert data["turns"]
    assert data["state_snapshots"]
    assert "clone_runs" in data
    assert metadata_is_object(data)
    assert f"# killme-lite session export: {session_id}" in markdown
    assert "## Turns" in markdown


def test_export_named_session_and_export_all(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    first = start(router, "first export")
    second = start(router, "second export")

    named_output = router.handle_line(f"/export {first}")
    all_output = router.handle_line("/export-all")
    first_json = tmp_path / "exports" / f"{first}.json"
    second_json = tmp_path / "exports" / f"{second}.json"

    assert f"已导出 session：{first}" in named_output
    assert first_json.exists()
    assert second_json.exists()
    assert "已导出 2 个 session" in all_output
    assert json.loads(first_json.read_text(encoding="utf-8"))["session"]["session_id"] == first
    assert json.loads(second_json.read_text(encoding="utf-8"))["session"]["session_id"] == second


def test_reset_clears_active_session_pointer(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    start(router, "reset idea")

    router.handle_line("/reset")

    assert router.storage.get_active_session_id() is None
    assert "暂无 sessions" in router.handle_line("/sessions")


def metadata_is_object(data: dict) -> bool:
    return all(isinstance(turn.get("metadata"), dict) for turn in data["turns"])
