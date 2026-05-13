from pathlib import Path

from state import new_state
from validation import PATCH_FIELDS_BY_ROLE, validate_agent_output, validate_chair_decision


ROOT = Path(__file__).resolve().parents[1]


def test_chair_state_patch_allowlist_matches_prompt_contract():
    assert PATCH_FIELDS_BY_ROLE["chair"] == {
        "current_major_question",
        "pending_next_question",
        "requires_user_intervention",
        "evidence_requests",
    }


def test_chair_disallowed_state_patch_fields_are_dropped():
    state = new_state(ROOT, "contract test")
    decision = validate_chair_decision(
        {
            "role": "chair",
            "decision_type": "wait_user",
            "state_patch": {
                "current_major_question": "allowed",
                "pending_next_question": "allowed next",
                "requires_user_intervention": True,
                "evidence_requests": [{"query": "allowed search"}],
                "strongest_attack": "forbidden",
                "strongest_defense": "forbidden",
                "best_redesign": "forbidden",
                "judge_verdict": "BUILD",
                "open_questions": ["forbidden"],
                "killed_arguments": ["forbidden"],
                "surviving_arguments": ["forbidden"],
                "evidence_items": [{"title": "forbidden"}],
            },
        },
        state,
    )

    assert decision["state_patch"] == {
        "current_major_question": "allowed",
        "pending_next_question": "allowed next",
        "requires_user_intervention": True,
        "evidence_requests": [{"query": "allowed search"}],
    }
    warnings = "\n".join(decision["validation_warnings"])
    assert "state_patch_field_not_allowed:strongest_attack" in warnings
    assert "state_patch_field_not_allowed:judge_verdict" in warnings
    assert "state_patch_field_not_allowed:evidence_items" in warnings


def test_agent_can_request_evidence_but_cannot_write_evidence_items():
    state = new_state(ROOT, "contract test")
    result = validate_agent_output(
        "executioner",
        {
            "role": "executioner",
            "strongest_attack": "needs external evidence",
            "evidence_requests": [{"query": "ToolBench quickstart", "reason": "check setup cost"}],
            "state_patch": {
                "strongest_attack": "needs external evidence",
                "evidence_items": [{"title": "forbidden"}],
            },
        },
        state,
    )

    assert result["state_patch"]["strongest_attack"] == "needs external evidence"
    assert result["state_patch"]["evidence_requests"] == [
        {"query": "ToolBench quickstart", "reason": "check setup cost"}
    ]
    assert "evidence_items" not in result["state_patch"]
