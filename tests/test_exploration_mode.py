from pathlib import Path

from config import load_config
from merger import merge_clone_outputs
from router import Router
from state import new_state


ROOT = Path(__file__).resolve().parents[1]


def make_router(tmp_path: Path) -> Router:
    return Router(
        base_dir=ROOT,
        db_path=tmp_path / "killme.sqlite",
        config=load_config(ROOT, environ={"KILLME_MOCK_LLM": "1"}),
    )


def test_explore_creates_exploration_session_with_map_fields(tmp_path: Path) -> None:
    router = make_router(tmp_path)

    output = router.handle_line("/explore 这个项目如何更适合开放探究性问题")
    state = router.storage.load_latest_state()

    assert state is not None
    assert "exploration session" in output
    assert "Exploration frame" in output
    assert state["agenda_mode"] == "exploration"
    assert state["exploration_focus"]
    assert state["exploration_focus"] == "这个项目如何更适合开放探究性问题"
    assert "这个项目如何更适合开放探究性问题" in state["current_major_question"]
    assert isinstance(state["hypotheses"], list)
    assert isinstance(state["research_threads"], list)
    assert isinstance(state["findings"], list)
    assert isinstance(state["coverage_gaps"], list)
    assert state["judge_verdict"] == "undecided"

    summary = router.handle_line("/summary")
    assert "- agenda_mode: exploration" in summary
    assert "## Exploration map" in summary


def test_exploration_auto_expands_hypotheses_without_verdict(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.handle_line("/explore 开放研究问题如何避免过早收敛")

    output = router.handle_line("/auto 1")
    state = router.storage.load_latest_state()

    assert state is not None
    assert state["agenda_mode"] == "exploration"
    assert state["hypotheses"]
    assert state["judge_verdict"] == "undecided"
    assert state["strongest_attack"] == ""
    assert "### defender result" in output or "### Merger result" in output
    assert "exploration_updates" in output


def test_exploration_close_synthesizes_without_judge_guard(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.handle_line("/explore 什么探索模式适合开放资料发现")

    output = router.handle_line("/close")
    state = router.storage.load_latest_state()

    assert state is not None
    assert "Chair exploration synthesis" in output
    assert "当前 Judge 还未裁决" not in output
    assert state["major_question_history"]
    closed = state["major_question_history"][-1]
    assert closed["judge_verdict"] == "undecided"
    assert closed["closed_without_judgement"] is False
    assert state["agenda_mode"] == "exploration"


def test_mode_command_switches_between_exploration_and_decision(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.handle_line("/explore 先扫描研究空间")

    decision_output = router.handle_line("/mode decision")
    state = router.storage.load_latest_state()

    assert state is not None
    assert "-> decision" in decision_output
    assert state["agenda_mode"] == "decision"
    assert state["hypotheses"] == [] or isinstance(state["hypotheses"], list)

    exploration_output = router.handle_line("/mode exploration")
    state = router.storage.load_latest_state()

    assert state is not None
    assert "-> exploration" in exploration_output
    assert state["agenda_mode"] == "exploration"


def test_merger_preserves_exploration_map_without_decision_scalar() -> None:
    state = new_state(ROOT, "merge exploration", agenda_mode="exploration")
    outputs = [
        {
            "new_attacks": [{"claim": "premature closure", "severity": "high"}],
            "coverage_gaps": ["missing source map"],
            "research_threads": ["compare exploration workflows"],
            "state_patch": {
                "strongest_attack": "do not write in exploration",
                "coverage_gaps": ["missing source map"],
            },
        },
        {
            "new_attacks": [{"claim": "premature closure", "severity": "medium"}],
            "coverage_gaps": ["missing source map", "missing counter-hypotheses"],
            "research_threads": ["collect examples"],
            "state_patch": {
                "coverage_gaps": ["missing counter-hypotheses"],
                "research_threads": ["collect examples"],
            },
        },
    ]

    result = merge_clone_outputs("executioner", state, outputs)

    assert result["state_patch"].get("strongest_attack", "") == ""
    assert result["state_patch"]["coverage_gaps"] == [
        "missing source map",
        "missing counter-hypotheses",
    ]
    assert result["state_patch"]["research_threads"] == [
        "compare exploration workflows",
        "collect examples",
    ]
