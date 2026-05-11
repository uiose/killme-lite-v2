from pathlib import Path

from config import load_config
from router import Router


ROOT = Path(__file__).resolve().parents[1]


def make_router(tmp_path):
    return Router(
        base_dir=ROOT,
        db_path=tmp_path / "killme.sqlite",
        config=load_config(ROOT, environ={"KILLME_MOCK_LLM": "1"}),
    )


def test_close_refuses_when_judge_is_undecided(tmp_path):
    router = make_router(tmp_path)
    router.handle_line("/start close guard test")

    output = router.handle_line("/close")
    state = router.storage.load_latest_state()

    assert "当前 Judge 还未裁决" in output
    assert state["major_question_history"] == []


def test_force_close_marks_closed_without_judgement(tmp_path):
    router = make_router(tmp_path)
    router.handle_line("/start close force test")

    output = router.handle_line("/close --force")
    state = router.storage.load_latest_state()

    assert "closed_without_judgement: true" in output
    assert state["major_question_history"][-1]["closed_without_judgement"] is True
    assert state["major_question_history"][-1]["judge_verdict"] == "undecided"


def test_force_closed_question_can_advance_to_pending_next_question(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KILLME_MOCK_LLM", "1")
    router = Router(base_dir=ROOT, db_path=tmp_path / "killme.sqlite")
    router.handle_line("/start force advance test")
    router.handle_line("/close --force")
    before = router.storage.load_latest_state()
    assert before is not None
    pending = before["pending_next_question"]
    assert pending

    router.handle_line("/auto 1")
    after = router.storage.load_latest_state()

    assert after is not None
    assert after["current_major_question"] == pending
