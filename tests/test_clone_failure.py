from pathlib import Path

import pytest

from config import load_config
from router import Router


ROOT = Path(__file__).resolve().parents[1]


class FailingLLM:
    def __init__(self):
        self.calls = 0

    def call_agent(self, role, state, recent_turns, current_task, clone_name=None, angle=None):
        del state, recent_turns, current_task, clone_name, angle
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("boom from clone two")
        return {
            "role": role,
            "new_attacks": [
                {
                    "claim": "first clone attack",
                    "severity": "high",
                    "why_it_matters": "it matters",
                    "evidence_needed": "evidence",
                }
            ],
            "strongest_attack": "first clone attack",
            "state_patch": {"strongest_attack": "first clone attack"},
        }


def test_clone_group_failure_is_recorded(tmp_path):
    router = Router(
        base_dir=ROOT,
        db_path=tmp_path / "killme.sqlite",
        config=load_config(ROOT, environ={"KILLME_MOCK_LLM": "1"}),
    )
    router.llm = FailingLLM()
    router.handle_line("/start failure recovery test")
    session_id = router.storage.load_latest_state()["session_id"]

    with pytest.raises(RuntimeError, match="boom from clone two"):
        router.handle_line("/spawn executioner 2")

    runs = router.storage.clone_runs_for_session(session_id)
    assert len(runs) == 1
    assert runs[0]["role"] == "executioner"
    assert runs[0]["status"] == "failed"
    assert "boom from clone two" in runs[0]["error"]

    turns = router.storage.recent_turns(session_id, limit=20)
    system_turns = [turn for turn in turns if turn["speaker"] == "system"]
    assert len(system_turns) == 1
    assert system_turns[0]["metadata"]["status"] == "failed"
    assert system_turns[0]["metadata"]["clone_group_id"] == runs[0]["clone_group_id"]

    latest_state = router.storage.load_latest_state(session_id)
    assert latest_state["strongest_attack"] == ""
