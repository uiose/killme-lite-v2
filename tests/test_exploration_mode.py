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


def test_exploration_auto_blocks_chair_patch_to_decision_judge(tmp_path: Path) -> None:
    class PatchSwitchingChair:
        def __init__(self) -> None:
            self.judge_called = False

        def begin_command(self) -> None:
            pass

        def end_command(self) -> None:
            pass

        def call_agent(self, role, state, recent_turns, current_task, **kwargs):
            if role == "chair":
                return {
                    "decision_type": "judge",
                    "role_to_call": "judge",
                    "reason": "bad automatic convergence",
                    "state_patch": {"agenda_mode": "decision"},
                }
            self.judge_called = True
            return {
                "verdict": "BUILD",
                "confidence": "high",
                "reasoning_summary": "Judge should not run from exploration auto.",
            }

    router = make_router(tmp_path)
    router.handle_line("/explore 开放研究问题不应自动收敛")
    fake_llm = PatchSwitchingChair()
    router.llm = fake_llm

    output = router.handle_line("/auto 1")
    state = router.storage.load_latest_state()

    assert state is not None
    assert fake_llm.judge_called is False
    assert state["agenda_mode"] == "exploration"
    assert state["judge_verdict"] == "undecided"
    assert state["requires_user_intervention"] is True
    assert "exploration_auto_agenda_mode_patch_ignored" in output
    assert "exploration_auto_judge_close_blocked" in output


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
    assert state["exploration_status"] == "paused"
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


def test_exploration_auto_allows_deeper_scan_and_records_graph(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.handle_line("/explore LLM 智能体记忆机制有哪些设计范式")

    output = router.handle_line("/auto 4")
    state = router.storage.load_latest_state()

    assert state is not None
    assert state["agenda_mode"] == "exploration"
    assert state["judge_verdict"] == "undecided"
    assert state["exploration_nodes"]
    assert state["exploration_edges"]
    assert state["anomalies"]
    assert state["decision_candidates"]
    assert "超过当前" not in output


def test_decision_auto_limit_stays_short_but_exploration_limit_is_higher(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.handle_line("/start 一个需要裁决的产品想法")
    decision_output = router.handle_line("/auto 4")
    assert "超过当前 decision mode 上限 3" in decision_output

    router.handle_line("/explore 一个开放研究主题")
    exploration_output = router.handle_line("/auto 4")
    assert "超过当前 exploration mode" not in exploration_output


def test_clone_mode_visible_records_visible_context_metadata(tmp_path: Path) -> None:
    router = make_router(tmp_path)
    router.handle_line("/explore 开放问题如何多假设探索")
    router.handle_line("/clone-mode visible")
    router.handle_line("/spawn defender 2")
    state = router.storage.load_latest_state()

    assert state is not None
    assert state["exploration_clone_mode"] == "visible"
    turns = router.storage.recent_turns(state["session_id"], limit=20)
    clone_turns = [turn for turn in turns if turn.get("metadata", {}).get("clone_context_mode") == "visible"]
    assert clone_turns
    assert any(turn.get("metadata", {}).get("sibling_outputs_visible") for turn in clone_turns)


def test_merger_prioritizes_anomaly_without_majority_support() -> None:
    state = new_state(ROOT, "merge exploration anomalies", agenda_mode="exploration")
    outputs = [
        {"coverage_gaps": [{"claim": "common gap", "severity": "medium"}]},
        {"coverage_gaps": [{"claim": "common gap", "severity": "medium"}]},
        {"anomalies": [{"claim": "unexpected edge case opens a new branch", "severity": "low"}]},
    ]

    result = merge_clone_outputs("executioner", state, outputs)

    assert result["merged_points"][0] == "unexpected edge case opens a new branch"
    assert result["state_patch"]["anomalies"] == [
        {"claim": "unexpected edge case opens a new branch", "severity": "low"}
    ]
