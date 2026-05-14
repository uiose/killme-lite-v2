import json
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from state import apply_state_patch
from validation import normalize_verdict


SEVERITY_SCORE = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}
CONFIDENCE_SCORE = {
    "high": 3,
    "medium": 2,
    "low": 1,
}
VERDICT_TIEBREAK = {
    "KILL": 4,
    "REDESIGN": 3,
    "TEST": 2,
    "BUILD": 1,
    "undecided": 0,
}
EXPLORATION_LIST_FIELDS = ("hypotheses", "research_threads", "findings", "coverage_gaps")


def _nonempty(values: Iterable[Any]) -> List[Any]:
    return [v for v in values if v not in (None, "", [], {})]


def _dedupe_with_removed(items: Iterable[Any]) -> Tuple[List[Any], List[Any]]:
    result: List[Any] = []
    removed: List[Any] = []
    seen = set()
    for item in items:
        if item in (None, ""):
            continue
        key = _stable_key(item)
        if key in seen:
            removed.append(item)
            continue
        result.append(item)
        seen.add(key)
    return result, removed


def _stable_key(item: Any) -> str:
    if isinstance(item, (dict, list)):
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    return str(item).strip().lower()


def merge_clone_outputs(role: str, state: Dict[str, Any], outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    raw_points = _collect_structured_points(role, outputs)
    points, duplicates_removed = _merge_duplicate_points(raw_points)
    sorted_points = sorted(
        points,
        key=lambda item: (-item["score"], -item["support_count"], item["first_seen_index"]),
    )
    strongest_point = sorted_points[0]["claim"] if sorted_points else ""
    conflicts = _detect_conflicts(role, sorted_points, outputs)
    patches = [output.get("state_patch", {}) for output in outputs if isinstance(output, dict)]
    state_patch = _build_state_patch(role, state, sorted_points, patches, outputs)

    point_claims = [point["claim"] for point in sorted_points]
    return {
        "role": "merger",
        "source_role": role,
        "points": sorted_points,
        "merged_points": point_claims,
        "duplicates_removed": duplicates_removed,
        "strongest_point": strongest_point,
        "conflicts": conflicts,
        "state_patch": state_patch,
    }


def _collect_structured_points(role: str, outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    for output_index, output in enumerate(outputs):
        if not isinstance(output, dict):
            continue
        _append_exploration_items(points, output, role, output_index)
        if role == "executioner":
            _append_items(
                points,
                output.get("new_attacks"),
                role=role,
                output_index=output_index,
                fallback_claim=output.get("strongest_attack"),
            )
            _append_scalar(points, output.get("strongest_attack"), role, output_index)
        elif role == "defender":
            _append_items(
                points,
                output.get("honest_defenses"),
                role=role,
                output_index=output_index,
                fallback_claim=output.get("strongest_defense"),
            )
            _append_scalar(points, output.get("strongest_defense"), role, output_index)
        elif role == "builder":
            _append_items(
                points,
                output.get("redesign_options"),
                role=role,
                output_index=output_index,
                fallback_claim=output.get("best_redesign"),
            )
            _append_scalar(points, output.get("best_redesign"), role, output_index)
        elif role == "judge":
            verdict = normalize_verdict(output.get("verdict"), default="undecided")
            summary = _text(output.get("reasoning_summary"))
            claim = f"{verdict}: {summary}" if summary else verdict
            if verdict != "undecided" or summary:
                points.append(
                    _make_point(
                        claim=claim,
                        role=role,
                        output_index=output_index,
                        severity=_severity_from_verdict(verdict),
                        confidence=output.get("confidence"),
                        evidence_needed=output.get("what_would_change_the_verdict"),
                        why_it_matters=output.get("next_validation_action"),
                        verdict=verdict,
                    )
                )
    return [point for point in points if point["claim"]]


def _append_exploration_items(
    points: List[Dict[str, Any]],
    output: Dict[str, Any],
    role: str,
    output_index: int,
) -> None:
    for field in EXPLORATION_LIST_FIELDS:
        _append_items(points, output.get(field), role=role, output_index=output_index)


def _append_items(
    points: List[Dict[str, Any]],
    items: Any,
    role: str,
    output_index: int,
    fallback_claim: Any = None,
) -> None:
    if not isinstance(items, list):
        items = [items] if items not in (None, "", [], {}) else []
    for item in items:
        if isinstance(item, dict):
            claim = _text(
                item.get("claim")
                or item.get("proposal")
                or item.get("point")
                or item.get("summary")
                or fallback_claim
            )
            points.append(
                _make_point(
                    claim=claim,
                    role=role,
                    output_index=output_index,
                    severity=item.get("severity"),
                    confidence=item.get("confidence"),
                    evidence_needed=item.get("evidence_needed"),
                    why_it_matters=item.get("why_it_matters") or item.get("condition"),
                    raw=item,
                )
            )
        elif item not in (None, ""):
            points.append(_make_point(_text(item), role, output_index))


def _append_scalar(points: List[Dict[str, Any]], claim: Any, role: str, output_index: int) -> None:
    text = _text(claim)
    if text:
        points.append(_make_point(text, role, output_index))


def _make_point(
    claim: str,
    role: str,
    output_index: int,
    severity: Any = None,
    confidence: Any = None,
    evidence_needed: Any = None,
    why_it_matters: Any = None,
    verdict: Optional[str] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    severity_text = _normalize_label(severity) or "medium"
    confidence_text = _normalize_label(confidence) or "medium"
    evidence_text = _text(evidence_needed)
    why_text = _text(why_it_matters)
    score = _score_point(
        severity=severity_text,
        confidence=confidence_text,
        evidence_needed=evidence_text,
        why_it_matters=why_text,
        support_count=1,
    )
    return {
        "claim": _text(claim),
        "role": role,
        "severity": severity_text,
        "confidence": confidence_text,
        "evidence_needed": evidence_text,
        "why_it_matters": why_text,
        "verdict": verdict or _infer_verdict_from_text(claim),
        "support_count": 1,
        "score": score,
        "first_seen_index": output_index,
        "source_indexes": [output_index],
        "raw": raw or {},
    }


def _merge_duplicate_points(points: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    merged: Dict[str, Dict[str, Any]] = {}
    removed: List[str] = []
    for point in points:
        key = _normalize_claim(point["claim"])
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(point)
            continue

        existing = merged[key]
        removed.append(point["claim"])
        existing["support_count"] += 1
        existing["source_indexes"] = sorted(set(existing["source_indexes"] + point["source_indexes"]))
        if _severity_value(point["severity"]) > _severity_value(existing["severity"]):
            existing["severity"] = point["severity"]
        if _confidence_value(point["confidence"]) > _confidence_value(existing["confidence"]):
            existing["confidence"] = point["confidence"]
        existing["evidence_needed"] = existing["evidence_needed"] or point["evidence_needed"]
        existing["why_it_matters"] = existing["why_it_matters"] or point["why_it_matters"]
        existing["score"] = _score_point(
            severity=existing["severity"],
            confidence=existing["confidence"],
            evidence_needed=existing["evidence_needed"],
            why_it_matters=existing["why_it_matters"],
            support_count=existing["support_count"],
        )
    return list(merged.values()), removed


def _score_point(
    severity: str,
    confidence: str,
    evidence_needed: str,
    why_it_matters: str,
    support_count: int,
) -> int:
    score = _severity_value(severity) * 100
    score += _confidence_value(confidence) * 10
    score += 5 if evidence_needed else 0
    score += 5 if why_it_matters else 0
    score += max(0, support_count - 1) * 15
    return score


def _build_state_patch(
    role: str,
    state: Dict[str, Any],
    points: List[Dict[str, Any]],
    patches: List[Dict[str, Any]],
    outputs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    patch: Dict[str, Any] = {
        "open_questions": [],
        "killed_arguments": [],
        "surviving_arguments": [],
        "evidence_requests": [],
    }

    list_fields = (
        "open_questions",
        "killed_arguments",
        "surviving_arguments",
        "evidence_requests",
        *EXPLORATION_LIST_FIELDS,
    )

    def append_list_value(list_field: str, value: Any) -> None:
        if list_field not in patch:
            patch[list_field] = []
        if isinstance(value, list):
            patch[list_field].extend(value)
        elif value not in (None, "", [], {}):
            patch[list_field].append(value)

    for output in outputs:
        if not isinstance(output, dict):
            continue
        output_patch = output.get("state_patch", {})
        if not isinstance(output_patch, dict):
            output_patch = {}
        for list_field in list_fields:
            # Preserve clone order: direct reported discoveries first, then any
            # explicit state_patch items from the same clone. Deduping below
            # removes repeated wrapper copies without reordering discoveries.
            append_list_value(list_field, output.get(list_field, []))
            append_list_value(list_field, output_patch.get(list_field, []))

    if not outputs:
        for item in patches:
            if not isinstance(item, dict):
                continue
            for list_field in list_fields:
                append_list_value(list_field, item.get(list_field, []))

    for list_field in (
        "open_questions",
        "killed_arguments",
        "surviving_arguments",
        "evidence_requests",
        *EXPLORATION_LIST_FIELDS,
    ):
        if list_field not in patch:
            patch[list_field] = []
        patch[list_field], _ = _dedupe_with_removed(patch[list_field])

    strongest = points[0]["claim"] if points else ""
    exploration_mode = str(state.get("agenda_mode") or "decision") == "exploration"
    if not exploration_mode and role == "executioner" and strongest:
        patch["strongest_attack"] = strongest
    elif not exploration_mode and role == "defender" and strongest:
        patch["strongest_defense"] = strongest
    elif not exploration_mode and role == "builder" and strongest:
        patch["best_redesign"] = strongest
    elif role == "judge":
        verdict = _choose_verdict(points, outputs)
        if verdict and verdict != "undecided":
            patch["judge_verdict"] = verdict

    clean_patch = apply_state_patch(
        {
            "session_id": state.get("session_id", ""),
            "clone_limits": state.get("clone_limits", {}),
            "open_questions": [],
            "killed_arguments": [],
            "surviving_arguments": [],
            "evidence_requests": [],
            "hypotheses": [],
            "research_threads": [],
            "findings": [],
            "coverage_gaps": [],
        },
        patch,
    )
    allowed = {
        "strongest_attack",
        "strongest_defense",
        "best_redesign",
        "judge_verdict",
        "open_questions",
        "killed_arguments",
        "surviving_arguments",
        "evidence_requests",
        "hypotheses",
        "research_threads",
        "findings",
        "coverage_gaps",
    }
    return {key: value for key, value in clean_patch.items() if key in allowed}


def _choose_verdict(points: List[Dict[str, Any]], outputs: List[Dict[str, Any]]) -> str:
    verdicts = [point.get("verdict") for point in points if point.get("verdict")]
    if not verdicts:
        verdicts = [normalize_verdict(output.get("verdict"), default="undecided") for output in outputs]
    verdicts = [verdict for verdict in verdicts if verdict and verdict != "undecided"]
    if not verdicts:
        return "undecided"

    counts = Counter(verdicts)
    return sorted(
        counts,
        key=lambda verdict: (-counts[verdict], -VERDICT_TIEBREAK.get(verdict, 0), verdict),
    )[0]


def _detect_conflicts(
    role: str,
    points: List[Dict[str, Any]],
    outputs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    del role
    texts = [point["claim"] for point in points]
    conflicts: List[Dict[str, Any]] = []

    verdicts = {
        normalize_verdict(output.get("verdict"), default="undecided")
        for output in outputs
        if isinstance(output, dict)
    }
    verdicts.update(point.get("verdict", "undecided") for point in points)
    verdicts.discard("undecided")
    if "KILL" in verdicts and "BUILD" in verdicts:
        conflicts.append(
            {
                "type": "verdict_conflict",
                "summary": "At least one clone recommends KILL while another recommends BUILD.",
                "signals": sorted(verdicts),
            }
        )

    _add_keyword_conflict(
        conflicts,
        texts,
        conflict_type="complexity_conflict",
        summary="One clone says complexity is unacceptable while another says implementation is light enough.",
        left=("too complex", "overengineer", "unacceptable complexity", "复杂度不可接受", "过重"),
        right=("light enough", "lightweight", "simple enough", "足够轻", "轻量", "最轻"),
    )
    _add_keyword_conflict(
        conflicts,
        texts,
        conflict_type="scope_direction_conflict",
        summary="One clone pushes to expand scope while another pushes to shrink scope.",
        left=("expand", "扩大", "扩展", "加大范围"),
        right=("shrink", "narrow", "reduce scope", "缩小", "收窄", "降范围"),
    )
    _add_keyword_conflict(
        conflicts,
        texts,
        conflict_type="judge_vs_close_conflict",
        summary="One clone asks for Judge while another says the question can be closed.",
        left=("need judge", "needs judge", "run judge", "需要 judge", "需要裁决", "先裁决"),
        right=("can close", "ready to close", "close now", "可以 close", "可以收束", "关闭问题"),
    )
    return conflicts


def _add_keyword_conflict(
    conflicts: List[Dict[str, Any]],
    texts: List[str],
    conflict_type: str,
    summary: str,
    left: Tuple[str, ...],
    right: Tuple[str, ...],
) -> None:
    left_hits = [text for text in texts if _has_any(text, left)]
    right_hits = [text for text in texts if _has_any(text, right)]
    if left_hits and right_hits:
        conflicts.append(
            {
                "type": conflict_type,
                "summary": summary,
                "examples": [left_hits[0], right_hits[0]],
            }
        )


def _has_any(text: str, keywords: Tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _severity_from_verdict(verdict: str) -> str:
    if verdict == "KILL":
        return "critical"
    if verdict == "REDESIGN":
        return "high"
    if verdict == "TEST":
        return "medium"
    return "low"


def _infer_verdict_from_text(text: Any) -> str:
    raw = str(text or "")
    inferred = normalize_verdict(raw, default="undecided")
    return inferred


def _severity_value(value: Any) -> int:
    return SEVERITY_SCORE.get(_normalize_label(value), SEVERITY_SCORE["medium"])


def _confidence_value(value: Any) -> int:
    return CONFIDENCE_SCORE.get(_normalize_label(value), CONFIDENCE_SCORE["medium"])


def _normalize_label(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_claim(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return re.sub(r"\s+", " ", text).strip()
