import copy
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List


DEFAULT_STATE = {
    "session_id": "",
    "chair_mode": "manual",
    "agenda_mode": "decision",
    "round": 0,
    "core_claim": "",
    "current_major_question": "",
    "exploration_focus": "",
    "exploration_status": "paused",
    "exploration_clone_mode": "independent",
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
    "evidence_items": [],
    "evidence_requests": [],
    "hypotheses": [],
    "research_threads": [],
    "findings": [],
    "coverage_gaps": [],
    "decision_candidates": [],
    "exploration_nodes": [],
    "exploration_edges": [],
    "anomalies": [],
    "pending_next_question": "",
    "requires_user_intervention": False,
}

LIST_FIELDS = {
    "open_questions",
    "killed_arguments",
    "surviving_arguments",
    "evidence_items",
    "evidence_requests",
    "hypotheses",
    "research_threads",
    "findings",
    "coverage_gaps",
    "decision_candidates",
    "exploration_nodes",
    "exploration_edges",
    "anomalies",
}
PATCH_LIST_FIELDS = {
    "open_questions",
    "killed_arguments",
    "surviving_arguments",
    "evidence_requests",
    "hypotheses",
    "research_threads",
    "findings",
    "coverage_gaps",
    "decision_candidates",
    "exploration_nodes",
    "exploration_edges",
    "anomalies",
}
HISTORY_FIELD = "major_question_history"
MUTABLE_SCALAR_FIELDS = {
    "chair_mode",
    "agenda_mode",
    "current_major_question",
    "exploration_focus",
    "exploration_status",
    "exploration_clone_mode",
    "strongest_attack",
    "strongest_defense",
    "best_redesign",
    "judge_verdict",
    "pending_next_question",
    "requires_user_intervention",
}
VALID_VERDICTS = {"undecided", "KILL", "REDESIGN", "TEST", "BUILD"}
VALID_CHAIR_MODES = {"manual", "auto"}
VALID_AGENDA_MODES = {"decision", "exploration"}
VALID_EXPLORATION_STATUSES = {"open", "paused", "synthesized"}
VALID_EXPLORATION_CLONE_MODES = {"independent", "visible"}
MAX_LIST_ITEMS = 120
MAX_HISTORY_ITEMS = 50
MAX_USER_POSITION_CHARS = 1200
MAX_CLONE_LIMIT = 5


INITIAL_USER_POSITION = "用户刚启动审议，尚未补充额外约束。"
INITIAL_MAJOR_QUESTION = "围绕这个想法，最需要先澄清的核心定义、适用边界和可证伪标准是什么？"
INITIAL_EXPLORATION_QUESTION = "围绕这个开放问题，先展开哪些定义、相邻问题、竞争性假设和资料路径，才能避免过早收敛？"


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


def new_state(base_dir: Path, idea: str, agenda_mode: str = "decision") -> Dict[str, Any]:
    state = load_state_template(base_dir)
    agenda_mode = str(agenda_mode or "decision").strip().lower()
    if agenda_mode not in VALID_AGENDA_MODES:
        agenda_mode = "decision"

    state["session_id"] = new_session_id()
    state["chair_mode"] = "manual"
    state["agenda_mode"] = agenda_mode
    state["round"] = 0
    state["core_claim"] = idea.strip()
    state["exploration_focus"] = idea.strip() if agenda_mode == "exploration" else ""
    state["exploration_status"] = "open" if agenda_mode == "exploration" else "paused"
    state["exploration_clone_mode"] = "independent"
    state["current_major_question"] = (
        INITIAL_EXPLORATION_QUESTION if agenda_mode == "exploration" else INITIAL_MAJOR_QUESTION
    )
    state["major_question_history"] = []
    state["user_position"] = INITIAL_USER_POSITION
    state["judge_verdict"] = "undecided"
    state["pending_next_question"] = ""
    state["requires_user_intervention"] = False
    state["hypotheses"] = []
    state["research_threads"] = []
    state["findings"] = []
    state["coverage_gaps"] = []
    state["decision_candidates"] = []
    state["exploration_nodes"] = []
    state["exploration_edges"] = []
    state["anomalies"] = []
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
        if field == "exploration_nodes":
            shaped[field] = normalize_exploration_nodes(shaped.get(field))[-MAX_LIST_ITEMS:]
        elif field == "exploration_edges":
            shaped[field] = normalize_exploration_edges(shaped.get(field))[-MAX_LIST_ITEMS:]
        else:
            shaped[field] = normalize_list(shaped.get(field))[-MAX_LIST_ITEMS:]

    shaped[HISTORY_FIELD] = normalize_history(shaped.get(HISTORY_FIELD))[-MAX_HISTORY_ITEMS:]

    try:
        shaped["round"] = int(shaped.get("round") or 0)
    except (TypeError, ValueError):
        shaped["round"] = 0

    shaped["requires_user_intervention"] = bool(shaped.get("requires_user_intervention", False))

    if shaped.get("chair_mode") not in VALID_CHAIR_MODES:
        shaped["chair_mode"] = "manual"

    if shaped.get("agenda_mode") not in VALID_AGENDA_MODES:
        shaped["agenda_mode"] = "decision"

    if shaped.get("exploration_status") not in VALID_EXPLORATION_STATUSES:
        shaped["exploration_status"] = "open" if shaped.get("agenda_mode") == "exploration" else "paused"

    if shaped.get("exploration_clone_mode") not in VALID_EXPLORATION_CLONE_MODES:
        shaped["exploration_clone_mode"] = "independent"

    if shaped.get("judge_verdict") not in VALID_VERDICTS:
        shaped["judge_verdict"] = "undecided"

    for key in (
        "session_id",
        "core_claim",
        "current_major_question",
        "exploration_focus",
        "exploration_status",
        "exploration_clone_mode",
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


def normalize_exploration_nodes(value: Any) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for item in normalize_list(value):
        if isinstance(item, dict):
            label = _first_nonempty(
                item.get("label"),
                item.get("title"),
                item.get("claim"),
                item.get("summary"),
                item.get("text"),
                item.get("query"),
            )
            if not label:
                label = json.dumps(item, ensure_ascii=False, sort_keys=True)
            node_type = str(item.get("type") or item.get("node_type") or "note").strip().lower()
            node = {
                "id": _safe_id(item.get("id"), prefix="n", seed=f"{node_type}:{label}"),
                "type": node_type,
                "label": _clip_text(label, 240),
            }
            for key in ("summary", "status", "source_role", "source", "tags", "confidence"):
                if item.get(key) not in (None, "", [], {}):
                    node[key] = item[key]
            nodes.append(node)
        elif item not in (None, ""):
            label = str(item).strip()
            nodes.append({"id": _safe_id(None, prefix="n", seed=f"note:{label}"), "type": "note", "label": _clip_text(label, 240)})
    return _dedupe_by_key(nodes, key="id")


def normalize_exploration_edges(value: Any) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for item in normalize_list(value):
        if isinstance(item, dict):
            source = _first_nonempty(item.get("source"), item.get("from"), item.get("source_id"))
            target = _first_nonempty(item.get("target"), item.get("to"), item.get("target_id"))
            relation = str(item.get("relation") or item.get("type") or "related_to").strip().lower()
            summary = _first_nonempty(item.get("summary"), item.get("claim"), item.get("reason"), item.get("label"))
            if not source and summary:
                source = _safe_id(None, prefix="n", seed=f"edge-source:{summary}")
            if not target and summary:
                target = _safe_id(None, prefix="n", seed=f"edge-target:{summary}")
            if not source or not target:
                continue
            edge = {
                "id": _safe_id(item.get("id"), prefix="e", seed=f"{source}:{relation}:{target}:{summary}"),
                "source": str(source),
                "target": str(target),
                "relation": relation,
            }
            for key in ("summary", "confidence", "source_role"):
                value_for_key = summary if key == "summary" else item.get(key)
                if value_for_key not in (None, "", [], {}):
                    edge[key] = _clip_text(value_for_key, 360) if isinstance(value_for_key, str) else value_for_key
            edges.append(edge)
    return _dedupe_by_key(edges, key="id")


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
    user position, major question history, or evidence_items. Those are controlled
    by local code and explicit evidence commands.
    """
    state = ensure_shape(copy.deepcopy(state))
    if not isinstance(patch, dict) or not patch:
        return state

    for key, value in patch.items():
        if key in PATCH_LIST_FIELDS:
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
            elif key == "agenda_mode":
                mode = str(value).strip().lower()
                if mode in VALID_AGENDA_MODES:
                    state[key] = mode
                    if mode == "exploration" and state.get("exploration_status") == "paused":
                        state["exploration_status"] = "open"
            elif key == "exploration_status":
                status = str(value).strip().lower()
                if status in VALID_EXPLORATION_STATUSES:
                    state[key] = status
            elif key == "exploration_clone_mode":
                mode = normalize_clone_mode(value)
                if mode:
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


def normalize_clone_mode(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "shared": "visible",
        "collaborative": "visible",
        "collab": "visible",
        "sequential": "visible",
        "visible_context": "visible",
        "可见": "visible",
        "协作": "visible",
        "independent": "independent",
        "isolated": "independent",
        "parallel": "independent",
        "独立": "independent",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in VALID_EXPLORATION_CLONE_MODES else ""


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
        "agenda_mode": state.get("agenda_mode", "decision"),
        "major_question": major_question or state.get("current_major_question", ""),
        "closing_statement": closing_statement or "",
        "what_survived": list(state.get("surviving_arguments") or [])[-8:],
        "what_was_killed": list(state.get("killed_arguments") or [])[-8:],
        "main_unresolved_tension": state.get("strongest_attack", ""),
        "core_claim": state.get("core_claim", ""),
        "exploration_focus": state.get("exploration_focus", ""),
        "user_position": state.get("user_position", ""),
        "strongest_attack": state.get("strongest_attack", ""),
        "strongest_defense": state.get("strongest_defense", ""),
        "best_redesign": state.get("best_redesign", ""),
        "judge_verdict": state.get("judge_verdict", "undecided"),
        "next_major_question": state.get("pending_next_question", ""),
        "mode_recommendation": "MANUAL" if state.get("requires_user_intervention", True) else "AUTO",
        "closed_without_judgement": bool(closed_without_judgement),
        "exploration_map_snapshot": {
            "hypotheses": list(state.get("hypotheses") or [])[-12:],
            "research_threads": list(state.get("research_threads") or [])[-12:],
            "findings": list(state.get("findings") or [])[-12:],
            "coverage_gaps": list(state.get("coverage_gaps") or [])[-12:],
            "decision_candidates": list(state.get("decision_candidates") or [])[-12:],
            "anomalies": list(state.get("anomalies") or [])[-12:],
            "nodes": list(state.get("exploration_nodes") or [])[-20:],
            "edges": list(state.get("exploration_edges") or [])[-20:],
        },
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


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _safe_id(value: Any, prefix: str, seed: str) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", candidate):
            return candidate
    digest = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _clip_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _dedupe_by_key(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        item_key = item.get(key)
        if not item_key or item_key in seen:
            continue
        result.append(item)
        seen.add(item_key)
    return result
