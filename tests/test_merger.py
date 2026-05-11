from pathlib import Path

from merger import merge_clone_outputs
from state import new_state


ROOT = Path(__file__).resolve().parents[1]


def test_merger_removes_exact_duplicates():
    state = new_state(ROOT, "merge test")
    outputs = [
        {
            "new_attacks": [{"claim": "same claim", "severity": "medium"}],
            "state_patch": {"open_questions": ["q1"]},
        },
        {
            "new_attacks": [{"claim": "same claim", "severity": "medium"}],
            "state_patch": {"open_questions": ["q1"]},
        },
    ]

    result = merge_clone_outputs("executioner", state, outputs)

    assert result["merged_points"] == ["same claim"]
    assert result["duplicates_removed"] == ["same claim"]
    assert result["state_patch"]["open_questions"] == ["q1"]


def test_merger_chooses_strongest_by_severity_not_first_item():
    state = new_state(ROOT, "merge test")
    outputs = [
        {
            "new_attacks": [
                {
                    "claim": "low severity first",
                    "severity": "low",
                    "confidence": "high",
                }
            ],
            "state_patch": {},
        },
        {
            "new_attacks": [
                {
                    "claim": "critical severity second",
                    "severity": "critical",
                    "confidence": "medium",
                    "evidence_needed": "real users fail to complete sessions",
                    "why_it_matters": "it invalidates the runtime",
                }
            ],
            "state_patch": {},
        },
    ]

    result = merge_clone_outputs("executioner", state, outputs)

    assert result["strongest_point"] == "critical severity second"
    assert result["state_patch"]["strongest_attack"] == "critical severity second"


def test_merger_detects_verdict_and_direction_conflicts():
    state = new_state(ROOT, "merge test")
    outputs = [
        {"verdict": "KILL", "confidence": "high", "reasoning_summary": "too complex"},
        {"verdict": "BUILD", "confidence": "high", "reasoning_summary": "light enough"},
    ]

    result = merge_clone_outputs("judge", state, outputs)
    conflict_types = {conflict["type"] for conflict in result["conflicts"]}

    assert "verdict_conflict" in conflict_types
    assert "complexity_conflict" in conflict_types
    assert result["state_patch"]["judge_verdict"] == "KILL"
