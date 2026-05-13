import json
import re
from typing import Any, Dict, List, Optional


VALID_ROLES = {"executioner", "defender", "builder", "judge"}
VALID_DECISION_TYPES = {"call_role", "spawn_clones", "judge", "close_question", "wait_user"}
VALID_VERDICTS = {"KILL", "REDESIGN", "TEST", "BUILD", "undecided"}
VALID_CONFIDENCE = {"low", "medium", "high"}
LIST_PATCH_FIELDS = {"open_questions", "killed_arguments", "surviving_arguments", "evidence_requests"}
SCALAR_PATCH_FIELDS = {
    "current_major_question",
    "strongest_attack",
    "strongest_defense",
    "best_redesign",
    "judge_verdict",
    "pending_next_question",
    "requires_user_intervention",
}
PATCH_FIELDS_BY_ROLE = {
    "chair": {
        "current_major_question",
        "pending_next_question",
        "requires_user_intervention",
        "evidence_requests",
    },
    "executioner": {"strongest_attack", "killed_arguments", "open_questions", "evidence_requests"},
    "defender": {"strongest_defense", "surviving_arguments", "open_questions", "evidence_requests"},
    "builder": {"best_redesign", "surviving_arguments", "open_questions", "evidence_requests"},
    "judge": {
        "judge_verdict",
        "open_questions",
        "surviving_arguments",
        "killed_arguments",
        "evidence_requests",
    },
    "merger": {
        "strongest_attack",
        "strongest_defense",
        "best_redesign",
        "judge_verdict",
        "open_questions",
        "killed_arguments",
        "surviving_arguments",
        "evidence_requests",
    },
}
MAX_TEXT = 1600
MAX_LIST_ITEMS = 20


class ValidationIssue(str):
    pass


def _raw_state_patch(data: Dict[str, Any]) -> Any:
    if not isinstance(data, dict):
        return {}
    for key in ("state_patch", "state_update", "patch"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def validate_chair_decision(raw: Any, state: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[str] = []
    data = raw if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        issues.append("chair_output_not_object")

    decision_type = normalize_decision_type(data.get("decision_type") or data.get("next_action") or data.get("action"))
    if decision_type not in VALID_DECISION_TYPES:
        issues.append("invalid_decision_type")
        decision_type = "wait_user"

    role_to_call = normalize_role(data.get("role_to_call") or data.get("agent") or data.get("role"))
    if decision_type == "judge":
        role_to_call = "judge"

    if decision_type in {"call_role", "spawn_clones", "judge"} and role_to_call not in VALID_ROLES:
        issues.append("invalid_role_to_call")
        decision_type = "wait_user"
        role_to_call = None

    clone_count = _to_int(data.get("clone_count", data.get("count", data.get("clones", 0))), 0)
    clone_tasks: List[Dict[str, str]] = []

    if decision_type == "spawn_clones":
        limit = _clone_limit(state, role_to_call)
        if clone_count < 1:
            issues.append("clone_count_missing_or_low")
            clone_count = 1
        if clone_count > limit:
            issues.append("clone_count_clamped_to_limit")
            clone_count = limit
        clone_tasks = sanitize_clone_tasks(data.get("clone_tasks"), role_to_call or "agent", clone_count)
    elif decision_type in {"call_role", "judge"}:
        clone_count = 1
        clone_tasks = []
    else:
        clone_count = 0
        clone_tasks = []

    requires_user_intervention = _to_bool(data.get("requires_user_intervention"), False)
    if decision_type in {"wait_user", "close_question"}:
        requires_user_intervention = True

    raw_patch = _raw_state_patch(data)
    state_patch = sanitize_state_patch(raw_patch, state, source_role="chair")
    if isinstance(raw_patch, dict):
        allowed = PATCH_FIELDS_BY_ROLE["chair"]
        for key in raw_patch:
            if key not in allowed:
                issues.append(f"state_patch_field_not_allowed:{key}")
    if requires_user_intervention:
        state_patch["requires_user_intervention"] = True

    closing_statement: Optional[str]
    if decision_type == "close_question":
        closing_statement = _markdown(data.get("closing_statement")) or None
    else:
        closing_statement = None

    return {
        "role": "chair",
        "decision_type": decision_type,
        "role_to_call": role_to_call,
        "clone_count": clone_count,
        "clone_tasks": clone_tasks,
        "reason": _text(data.get("reason")),
        "requires_user_intervention": requires_user_intervention,
        "message_to_user": _text(data.get("message_to_user") or data.get("message")),
        "state_patch": state_patch,
        "closing_statement": closing_statement,
        "validation_warnings": issues,
    }


def validate_agent_output(role: str, raw: Any, state: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[str] = []
    data = raw if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        issues.append("agent_output_not_object")

    role = normalize_role(role) or role
    patch = sanitize_state_patch(_raw_state_patch(data), state, source_role=role)
    evidence_requests = _list(data.get("evidence_requests"))
    if evidence_requests and "evidence_requests" not in patch:
        patch["evidence_requests"] = evidence_requests

    if role == "executioner":
        strongest = _text(data.get("strongest_attack"))
        killed = _list(data.get("killed_arguments"))
        open_questions = _list(data.get("open_questions"))
        if strongest and "strongest_attack" not in patch:
            patch["strongest_attack"] = strongest
        if killed and "killed_arguments" not in patch:
            patch["killed_arguments"] = killed
        if open_questions and "open_questions" not in patch:
            patch["open_questions"] = open_questions
        return {
            "role": "executioner",
            "task_understood": _text(data.get("task_understood")),
            "new_attacks": _list(data.get("new_attacks")),
            "strongest_attack": strongest,
            "killed_arguments": killed,
            "open_questions": open_questions,
            "evidence_requests": evidence_requests,
            "state_patch": patch,
            "validation_warnings": issues,
        }

    if role == "defender":
        strongest = _text(data.get("strongest_defense"))
        surviving = _list(data.get("surviving_arguments"))
        open_questions = _list(data.get("open_questions"))
        if strongest and "strongest_defense" not in patch:
            patch["strongest_defense"] = strongest
        if surviving and "surviving_arguments" not in patch:
            patch["surviving_arguments"] = surviving
        if open_questions and "open_questions" not in patch:
            patch["open_questions"] = open_questions
        return {
            "role": "defender",
            "task_understood": _text(data.get("task_understood")),
            "honest_defenses": _list(data.get("honest_defenses")),
            "strongest_defense": strongest,
            "surviving_arguments": surviving,
            "open_questions": open_questions,
            "evidence_requests": evidence_requests,
            "state_patch": patch,
            "validation_warnings": issues,
        }

    if role == "builder":
        best = _text(data.get("best_redesign"))
        surviving = _list(data.get("surviving_arguments"))
        open_questions = _list(data.get("open_questions"))
        if best and "best_redesign" not in patch:
            patch["best_redesign"] = best
        if surviving and "surviving_arguments" not in patch:
            patch["surviving_arguments"] = surviving
        if open_questions and "open_questions" not in patch:
            patch["open_questions"] = open_questions
        return {
            "role": "builder",
            "task_understood": _text(data.get("task_understood")),
            "redesign_options": _list(data.get("redesign_options")),
            "best_redesign": best,
            "surviving_arguments": surviving,
            "open_questions": open_questions,
            "evidence_requests": evidence_requests,
            "state_patch": patch,
            "validation_warnings": issues,
        }

    if role == "judge":
        verdict = normalize_verdict(data.get("verdict"), default="undecided")
        confidence = str(data.get("confidence") or "medium").strip().lower()
        if confidence not in VALID_CONFIDENCE:
            issues.append("invalid_confidence_normalized")
            confidence = "medium"
        if verdict != "undecided":
            patch["judge_verdict"] = verdict
        return {
            "role": "judge",
            "verdict": verdict,
            "confidence": confidence,
            "reasoning_summary": _text(data.get("reasoning_summary")),
            "what_would_change_the_verdict": _text(data.get("what_would_change_the_verdict")),
            "next_validation_action": _text(data.get("next_validation_action")),
            "evidence_requests": evidence_requests,
            "state_patch": patch,
            "validation_warnings": issues,
        }

    issues.append("unknown_role")
    return {
        "role": str(role),
        "strongest_point": _text(data.get("strongest_point")),
        "state_patch": patch,
        "validation_warnings": issues,
    }


def sanitize_state_patch(raw: Any, state: Optional[Dict[str, Any]] = None, source_role: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = PATCH_FIELDS_BY_ROLE.get(source_role or "", LIST_PATCH_FIELDS | SCALAR_PATCH_FIELDS)
    patch: Dict[str, Any] = {}
    for key, value in raw.items():
        if key not in allowed:
            continue
        if key in LIST_PATCH_FIELDS:
            patch[key] = _list(value)
            continue
        if key not in SCALAR_PATCH_FIELDS:
            continue
        if key == "requires_user_intervention":
            patch[key] = _to_bool(value, False)
        elif key == "judge_verdict":
            verdict = normalize_verdict(value, default="undecided")
            if verdict != "undecided":
                patch[key] = verdict
        else:
            text = _text(value)
            if text:
                patch[key] = text
    return patch


def sanitize_clone_tasks(raw: Any, role: str, count: int) -> List[Dict[str, str]]:
    tasks: List[Dict[str, str]] = []
    if isinstance(raw, list):
        for index, item in enumerate(raw[:count]):
            if isinstance(item, dict):
                name = _text(item.get("name")) or f"{role.capitalize()}-{index + 1}"
                angle = _text(item.get("angle")) or f"independent angle {index + 1}"
                task = _text(item.get("task")) or f"Evaluate from angle: {angle}"
            else:
                name = f"{role.capitalize()}-{index + 1}"
                angle = _text(item) or f"independent angle {index + 1}"
                task = f"Evaluate from angle: {angle}"
            tasks.append({"name": name, "angle": angle, "task": task})

    while len(tasks) < count:
        index = len(tasks)
        angle = f"independent angle {index + 1}"
        tasks.append(
            {
                "name": f"{role.capitalize()}-{index + 1}",
                "angle": angle,
                "task": f"Evaluate from angle: {angle}",
            }
        )

    seen = set()
    for index, task in enumerate(tasks):
        key = task["angle"].strip().lower()
        if key in seen:
            task["angle"] = f"{task['angle']} #{index + 1}"
        seen.add(task["angle"].strip().lower())

    return tasks[:count]


def normalize_role(value: Any) -> Optional[str]:
    if value is None:
        return None
    role = str(value).strip().lower()
    aliases = {
        "exec": "executioner",
        "attack": "executioner",
        "attacker": "executioner",
        "defense": "defender",
        "defend": "defender",
        "build": "builder",
        "redesign": "builder",
        "裁判": "judge",
    }
    role = aliases.get(role, role)
    return role if role in VALID_ROLES else None


def normalize_decision_type(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in VALID_DECISION_TYPES:
        return raw
    if "clone" in raw or "spawn" in raw:
        return "spawn_clones"
    if "judge" in raw:
        return "judge"
    if "close" in raw or "closing" in raw or "收束" in raw:
        return "close_question"
    if "wait" in raw or "user" in raw or "manual" in raw or "等待" in raw:
        return "wait_user"
    if "call" in raw or "role" in raw:
        return "call_role"
    return "wait_user"


def normalize_verdict(value: Any, default: str = "undecided") -> str:
    if value is None:
        return default
    raw = str(value).strip()
    upper = raw.upper()
    if upper in VALID_VERDICTS:
        return upper
    lowered = raw.lower()
    if lowered in {"undecided", "unknown", "none", "n/a"}:
        return "undecided"
    if "kill" in lowered or "停止" in raw or "杀" in raw:
        return "KILL"
    if "redesign" in lowered or "改" in raw or "重设" in raw or "重构" in raw:
        return "REDESIGN"
    if "build" in lowered or "构建" in raw or "开建" in raw:
        return "BUILD"
    if "test" in lowered or "验证" in raw or "测试" in raw:
        return "TEST"
    return default


def _clone_limit(state: Dict[str, Any], role: Optional[str]) -> int:
    if role is None:
        return 1
    try:
        return max(1, int((state.get("clone_limits") or {}).get(role, 1)))
    except (TypeError, ValueError):
        return 1


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "manual", "wait"}:
        return True
    if text in {"false", "0", "no", "n", "auto"}:
        return False
    return default



def _markdown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.strip()
    if len(text) > MAX_TEXT * 4:
        return text[: MAX_TEXT * 4].rstrip() + "..."
    return text

def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT].rstrip() + "..."
    return text


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: List[Any] = []
    for item in items[:MAX_LIST_ITEMS]:
        if item in (None, "", [], {}):
            continue
        if isinstance(item, dict):
            # Keep small structured claims from agents, but clip every string field.
            result.append({str(k): _text(v) for k, v in item.items()})
        else:
            text = _text(item)
            if text:
                result.append(text)
    return result
