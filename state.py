import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List


DEFAULT_STATE = {
    "session_id": "",
    "chair_mode": "manual",
    "round": 0,
    "core_claim": "",
    "current_major_question": "",
    "major_question_history": [],
    "user_position": "",
    "strongest_attack": "",
    "strongest_defense": "",
    "best_redesign": "",
    "judge_verdict": "undecided",
    "clone_limits": {
        "executioner": 3,
        "defender": 2,
        "builder": 2,
        "judge": 1,
    },
    "open_questions": [],
    "killed_arguments": [],
    "surviving_arguments": [],
    "pending_next_question": "",
    "requires_user_intervention": False,
}

LIST_FIELDS = {"open_questions", "killed_arguments", "surviving_arguments"}
HISTORY_FIELD = "major_question_history"
MUTABLE_SCALAR_FIELDS = {
    "chair_mode",
    "current_major_question",
    "strongest_attack",
    "strongest_defense",
    "best_redesign",
    "judge_verdict",
    "pending_next_question",
    "requires_user_intervention",
}
VALID_VERDICTS = {"undecided", "KILL", "REDESIGN", "TEST", "BUILD"}
VALID_CHAIR_MODES = {"manual", "auto"}
MAX_LIST_ITEMS = 80
MAX_HISTORY_ITEMS = 50
MAX_USER_POSITION_CHARS = 1200
MAX_CLONE_LIMIT = 5


INITIAL_USER_POSITION = "用户刚启动审议，尚未补充额外约束。"
INITIAL_MAJOR_QUESTION = "围绕这个想法，最需要先澄清的核心定义、适用边界和可证伪标准是什么？"


def load_state_template(base_dir: Path) -> Dict[str, Any]:
    candidates = [
        base_dir / "state_schema.json",
        base_dir.parent / "state_schema.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return copy.deepcopy(DEFAULT_STATE)


def new_session_id() -> str:
    return f"session-{uuid.uuid4().hex[:12]}"


def new_state(base_dir: Path, idea: str) -> Dict[str, Any]:
    state = load_state_template(base_dir)
    state["session_id"] = new_session_id()
    state["chair_mode"] = "manual"
    state["round"] = 0
    state["core_claim"] = idea.strip()
    state["current_major_question"] = INITIAL_MAJOR_QUESTION
    state["major_question_history"] = []
    state["user_position"] = INITIAL_USER_POSITION
    state["judge_verdict"] = "undecided"
    state["pending_next_question"] = ""
    state["requires_user_intervention"] = False
    return ensure_shape(state)


def ensure_shape(state: Dict[str, Any]) -> Dict[str, Any]:
    shaped = copy.deepcopy(DEFAULT_STATE)
    shaped.update(state or {})

    clone_limits = copy.deepcopy(DEFAULT_STATE["clone_limits"])
    incoming_limits = shaped.get("clone_limits") or {}
    if isinstance(incoming_limits, dict):
        for role, default_limit in clone_limits.items():
            try:
                clone_limits[role] = max(1, min(MAX_CLONE_LIMIT, int(incoming_limits.get(role, default_limit))))
            except (TypeError, ValueError):
                clone_limits[role] = default_limit
    shaped["clone_limits"] = clone_limits

    for field in LIST_FIELDS:
        shaped[field] = normalize_list(shaped.get(field))[-MAX_LIST_ITEMS:]

    shaped[HISTORY_FIELD] = normalize_history(shaped.get(HISTORY_FIELD))[-MAX_HISTORY_ITEMS:]

    try:
        shaped["round"] = int(shaped.get("round") or 0)
    except (TypeError, ValueError):
        shaped["round"] = 0

    shaped["requires_user_intervention"] = bool(shaped.get("requires_user_intervention", False))

    if shaped.get("chair_mode") not in VALID_CHAIR_MODES:
        shaped["chair_mode"] = "manual"

    if shaped.get("judge_verdict") not in VALID_VERDICTS:
        shaped["judge_verdict"] = "undecided"

    for key in (
        "session_id",
        "core_claim",
        "current_major_question",
        "user_position",
        "strongest_attack",
        "strongest_defense",
        "best_redesign",
        "pending_next_question",
    ):
        value = shaped.get(key)
        if value is None:
            shaped[key] = ""
        elif not isinstance(value, str):
            shaped[key] = str(value)

    if len(shaped.get("user_position", "")) > MAX_USER_POSITION_CHARS:
        shaped["user_position"] = shaped["user_position"][:MAX_USER_POSITION_CHARS].rstrip() + "..."

    return shaped


def normalize_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "", [], {})]
    return [str(value)] if value not in (None, "") else []


def normalize_history(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append(item)
        elif item not in (None, ""):
            result.append({"closing_statement": str(item)})
    return result


def unique_extend(existing: List[Any], incoming: Iterable[Any]) -> List[Any]:
    result = list(existing or [])
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in result}
    for item in incoming or []:
        if item in (None, ""):
            continue
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result[-MAX_LIST_ITEMS:]


def apply_state_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a small, validated state patch.

    LLM outputs are not allowed to mutate identity, round number, clone limits,
    user position, or major question history. Those are controlled by local code.
    """
    state = ensure_shape(copy.deepcopy(state))
    if not isinstance(patch, dict) or not patch:
        return state

    for key, value in patch.items():
        if key in LIST_FIELDS:
            if isinstance(value, list):
                state[key] = unique_extend(state.get(key, []), value)
            elif value not in (None, ""):
                state[key] = unique_extend(state.get(key, []), [value])
            continue

        if key in MUTABLE_SCALAR_FIELDS and value not in (None, ""):
            if key == "requires_user_intervention":
                state[key] = bool(value)
            elif key == "judge_verdict":
                verdict = str(value).strip().upper()
                state[key] = verdict if verdict in VALID_VERDICTS else "undecided"
            elif key == "chair_mode":
                mode = str(value).strip().lower()
                if mode in VALID_CHAIR_MODES:
                    state[key] = mode
            else:
                state[key] = str(value).strip()

    return ensure_shape(state)


def increment_round(state: Dict[str, Any]) -> Dict[str, Any]:
    state = ensure_shape(copy.deepcopy(state))
    state["round"] = int(state.get("round", 0)) + 1
    return state


def reset_for_next_major_question(state: Dict[str, Any], next_question: str) -> Dict[str, Any]:
    state = ensure_shape(copy.deepcopy(state))
    state["current_major_question"] = next_question.strip()
    state["strongest_attack"] = ""
    state["strongest_defense"] = ""
    state["best_redesign"] = ""
    state["judge_verdict"] = "undecided"
    state["pending_next_question"] = ""
    state["requires_user_intervention"] = False
    return state


def merge_user_position(existing: str, user_text: str) -> str:
    """Compress user inputs into a durable position instead of overwriting it."""
    new_clause = _clean_clause(user_text)
    if not new_clause:
        return existing or INITIAL_USER_POSITION

    existing = (existing or "").strip()
    if not existing or existing == INITIAL_USER_POSITION:
        return _clip_user_position(f"用户当前约束/立场：{new_clause}")

    clauses = _extract_position_clauses(existing)
    normalized = {_normalize_for_compare(item) for item in clauses}
    if _normalize_for_compare(new_clause) not in normalized:
        clauses.append(new_clause)

    # Keep early foundational constraints and recent constraints. This avoids the
    # last user message erasing earlier architectural boundaries.
    if len(clauses) > 10:
        clauses = clauses[:4] + clauses[-6:]

    return _clip_user_position("用户当前约束/立场：" + "；".join(clauses))


def append_major_question_history(
    state: Dict[str, Any],
    major_question: str,
    closing_statement: str,
    closed_without_judgement: bool = False,
) -> Dict[str, Any]:
    state = ensure_shape(copy.deepcopy(state))
    entry = {
        "round": state.get("round", 0),
        "major_question": major_question or state.get("current_major_question", ""),
        "closing_statement": closing_statement or "",
        "what_survived": list(state.get("surviving_arguments") or [])[-8:],
        "what_was_killed": list(state.get("killed_arguments") or [])[-8:],
        "main_unresolved_tension": state.get("strongest_attack", ""),
        "core_claim": state.get("core_claim", ""),
        "user_position": state.get("user_position", ""),
        "strongest_attack": state.get("strongest_attack", ""),
        "strongest_defense": state.get("strongest_defense", ""),
        "best_redesign": state.get("best_redesign", ""),
        "judge_verdict": state.get("judge_verdict", "undecided"),
        "next_major_question": state.get("pending_next_question", ""),
        "mode_recommendation": "MANUAL" if state.get("requires_user_intervention", True) else "AUTO",
        "closed_without_judgement": bool(closed_without_judgement),
    }

    history = list(state.get("major_question_history") or [])
    if history:
        last = history[-1]
        if (
            last.get("major_question") == entry["major_question"]
            and last.get("closing_statement") == entry["closing_statement"]
        ):
            state["major_question_history"] = history[-MAX_HISTORY_ITEMS:]
            return state

    history.append(entry)
    state["major_question_history"] = history[-MAX_HISTORY_ITEMS:]
    return ensure_shape(state)


def pretty_state(state: Dict[str, Any]) -> str:
    return json.dumps(ensure_shape(state), ensure_ascii=False, indent=2)


def _extract_position_clauses(text: str) -> List[str]:
    text = text.strip()
    text = re.sub(r"^用户当前约束/立场[:：]\s*", "", text)
    text = re.sub(r"^用户立场[:：]\s*", "", text)
    raw_parts = re.split(r"[；;\n]+", text)
    clauses = []
    for part in raw_parts:
        cleaned = _clean_clause(part)
        if cleaned and cleaned != INITIAL_USER_POSITION:
            clauses.append(cleaned)
    return clauses


def _clean_clause(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:240].strip()


def _normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _clip_user_position(text: str) -> str:
    if len(text) <= MAX_USER_POSITION_CHARS:
        return text
    return text[:MAX_USER_POSITION_CHARS].rstrip() + "..."
