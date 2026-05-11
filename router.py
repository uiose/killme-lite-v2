import copy
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import AppConfig, load_config
from llm import LLMClient, extract_json
from merger import merge_clone_outputs
from state import (
    MAX_CLONE_LIMIT,
    append_major_question_history,
    apply_state_patch,
    ensure_shape,
    increment_round,
    merge_user_position,
    new_state,
    pretty_state,
    reset_for_next_major_question,
)
from storage import Storage
from validation import (
    VALID_ROLES,
    normalize_role,
    sanitize_clone_tasks,
    validate_agent_output,
    validate_chair_decision,
)


ROLE_COMMANDS = {
    "/exec": "executioner",
    "/defend": "defender",
    "/build": "builder",
    "/judge": "judge",
}
AUTO_MAX_STEPS = 3
MAX_CONFIG_CLONES = MAX_CLONE_LIMIT


class Router:
    def __init__(
        self,
        base_dir: Path,
        db_path: Optional[Path] = None,
        config: Optional[AppConfig] = None,
        progress: Optional[Callable[[str], None]] = None,
    ):
        self.base_dir = Path(base_dir)
        self.config = config or load_config(self.base_dir)
        self.storage = Storage(db_path or self.config.db_path)
        self.llm = LLMClient(base_dir=self.base_dir, config=self.config)
        self.progress = progress

    def handle_line(self, line: str) -> str:
        if line.startswith("/"):
            return self.handle_command(line)
        return self.handle_user_message(line)

    def handle_command(self, line: str) -> str:
        command, arg = self._split_command(line)

        if command == "/start":
            return self.cmd_start(arg)

        if command == "/auto":
            return self.cmd_auto(arg)

        if command == "/manual":
            return self.cmd_manual()

        if command in ROLE_COMMANDS:
            return self.cmd_role(ROLE_COMMANDS[command])

        if command == "/spawn":
            return self.cmd_spawn(arg)

        if command == "/sessions":
            return self.cmd_sessions(arg)

        if command == "/use":
            return self.cmd_use(arg)

        if command == "/export":
            return self.cmd_export(arg)

        if command == "/export-all":
            return self.cmd_export_all()

        if command == "/state":
            return self.cmd_state()

        if command == "/summary":
            return self.cmd_summary()

        if command == "/checkpoint":
            return self.cmd_checkpoint()

        if command == "/config":
            return self.cmd_config(arg)

        if command == "/close":
            return self.cmd_close(arg)

        if command == "/reset":
            self.storage.reset_all()
            return "已重置本地 sessions、turns、state_snapshots、clone_runs。"

        return f"未知命令：{command}"

    def cmd_start(self, idea: str) -> str:
        idea = idea.strip()
        if not idea:
            return "用法：/start <idea>"

        state = new_state(self.base_dir, idea)
        self.storage.create_session(state["session_id"], title=idea[:80])
        self.storage.set_active_session_id(state["session_id"])
        self.storage.save_turn(state["session_id"], state["round"], "user", f"/start {idea}")
        state, question_source = self._bootstrap_initial_major_question(state)
        self.storage.save_snapshot(state["session_id"], state)

        return (
            f"已创建 session：{state['session_id']}\n"
            f"Core claim：{state['core_claim']}\n"
            f"Current major question：{state['current_major_question']}\n"
            f"Question source：{question_source}\n"
            f"建议下一步：/auto {AUTO_MAX_STEPS} 或 /exec"
        )

    def cmd_auto(self, arg: str) -> str:
        state = self._require_state()
        try:
            steps = int(arg.strip() or "1")
        except ValueError:
            return f"用法：/auto <n>，例如 /auto {AUTO_MAX_STEPS}"

        if steps < 1:
            return "/auto 的步数必须 >= 1"

        if steps > AUTO_MAX_STEPS:
            return (
                f"/auto 的 MVP 硬上限是 {AUTO_MAX_STEPS}。"
                "这是有限推进按钮，不是让 agent 自己聊完的放飞按钮。"
            )

        state["chair_mode"] = "auto"
        state["requires_user_intervention"] = False
        self._save_state(state)

        outputs: List[str] = []
        for step in range(steps):
            state = self._require_state()

            # If the previous major question was closed, an explicit /auto means
            # the user allows Chair to move into the pending next question.
            if state.get("pending_next_question") and (
                state.get("judge_verdict") != "undecided"
                or self._current_major_question_is_closed(state)
            ):
                state = reset_for_next_major_question(state, state["pending_next_question"])
                state = increment_round(state)
                self.storage.save_turn(
                    state["session_id"],
                    state["round"],
                    "chair",
                    f"Advance to next major question: {state['current_major_question']}",
                    {"auto_step": step + 1, "type": "advance_major_question"},
                )
                self.storage.save_snapshot(state["session_id"], state)

            result = self._chair_step(auto_step=step + 1)
            outputs.append(result)

            latest = self._require_state()
            if latest.get("requires_user_intervention"):
                break

        latest = self._require_state()
        latest["chair_mode"] = "manual"
        self._save_state(latest)
        outputs.append("Auto run stopped. 当前已回到 manual mode，等待用户。")
        return "\n\n---\n\n".join(outputs)

    def cmd_manual(self) -> str:
        state = self._require_state()
        state["chair_mode"] = "manual"
        state["requires_user_intervention"] = True
        self._save_state(state)
        return "已切换到 manual mode。Chair 不会擅自连续推进。"

    def cmd_role(self, role: str) -> str:
        result, updated = self._run_role(
            role,
            task=f"Manual command: call one {role}",
            clone_tasks=None,
        )
        updated["chair_mode"] = "manual"
        updated["requires_user_intervention"] = True
        self._save_state(updated)
        return self._format_agent_result(result)

    def cmd_spawn(self, arg: str) -> str:
        state = self._require_state()
        parts = arg.split()
        if len(parts) != 2:
            return "用法：/spawn <executioner|defender|builder|judge> <count>"

        role = normalize_role(parts[0])
        if role not in VALID_ROLES:
            return "role 必须是 executioner、defender、builder 或 judge"

        try:
            count = int(parts[1])
        except ValueError:
            return "count 必须是整数"

        if count < 1:
            return "count 必须 >= 1"

        limit = int((state.get("clone_limits") or {}).get(role, 1))
        if count > limit:
            return f"{role} 当前 clone 上限是 {limit}。请先用 /config {role} {count} 调整上限。"

        clone_tasks = self._default_clone_tasks(role, count)
        result, updated = self._run_role(
            role,
            task=f"Manual command: spawn {count} {role} clone(s)",
            clone_tasks=clone_tasks,
        )
        updated["chair_mode"] = "manual"
        updated["requires_user_intervention"] = True
        self._save_state(updated)
        return self._format_agent_result(result)

    def cmd_state(self) -> str:
        state = self._require_state()
        return pretty_state(state)

    def cmd_sessions(self, arg: str = "") -> str:
        raw_limit = arg.strip()
        if raw_limit:
            try:
                limit = int(raw_limit)
            except ValueError:
                return "用法：/sessions [limit]"
        else:
            limit = 20
        if limit < 1:
            return "limit 必须 >= 1"
        limit = min(limit, 100)

        sessions = self.storage.list_sessions(limit)
        if not sessions:
            return "暂无 sessions。请先运行 /start <idea>。"

        active_session_id = self._active_session_id()
        lines = [
            "## Sessions",
            f"- active_session_id: {active_session_id or '暂无'}",
            f"- shown: {len(sessions)}",
            "",
            "| active | session_id | round | turns | clone_runs | updated_at | title |",
            "|---|---|---:|---:|---:|---|---|",
        ]
        for session in sessions:
            marker = "*" if session["session_id"] == active_session_id else ""
            lines.append(
                "| {active} | {session_id} | {round} | {turns} | {clones} | {updated_at} | {title} |".format(
                    active=marker,
                    session_id=session["session_id"],
                    round=session.get("latest_round", 0),
                    turns=session.get("turn_count", 0),
                    clones=session.get("clone_run_count", 0),
                    updated_at=session.get("updated_at", ""),
                    title=self._escape_table_cell(str(session.get("title") or "")),
                )
            )
        return "\n".join(lines)

    def cmd_use(self, arg: str) -> str:
        session_id = arg.strip()
        if not session_id:
            return "用法：/use <session_id>"
        if not self.storage.session_exists(session_id):
            return f"未找到 session：{session_id}"

        self.storage.set_active_session_id(session_id)
        state = self.storage.load_latest_state(session_id)
        if state is None:
            return f"已切换 active session：{session_id}\n但该 session 暂无 state snapshot。"
        return f"已切换 active session：{session_id}\n\n" + self._format_summary(ensure_shape(state))

    def cmd_summary(self) -> str:
        state = self._require_state()
        return self._format_summary(state)

    def cmd_checkpoint(self) -> str:
        state = self._require_state()
        state = increment_round(state)
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            "/checkpoint",
            {"command": "checkpoint"},
        )
        self.storage.save_snapshot(state["session_id"], state)
        return "已保存 checkpoint。\n\n" + self._format_summary(state)

    def cmd_export(self, arg: str = "") -> str:
        session_id = arg.strip() or self._active_session_id()
        if not session_id:
            return "没有 active session 可导出。请先运行 /start <idea> 或 /use <session_id>。"
        if not self.storage.session_exists(session_id):
            return f"未找到 session：{session_id}"

        json_path, markdown_path = self._export_session(session_id)
        return (
            f"已导出 session：{session_id}\n"
            f"- JSON: {json_path}\n"
            f"- Markdown: {markdown_path}"
        )

    def cmd_export_all(self) -> str:
        session_ids = self.storage.all_session_ids()
        if not session_ids:
            return "暂无 sessions 可导出。"

        for session_id in session_ids:
            self._export_session(session_id)
        export_dir = self.base_dir / "exports"
        return f"已导出 {len(session_ids)} 个 session 到：{export_dir}"

    def cmd_config(self, arg: str) -> str:
        state = self._require_state()
        parts = arg.split()
        if len(parts) != 2:
            return "用法：/config <executioner|defender|builder|judge> <max_clones>"

        role, raw_limit = parts
        role = normalize_role(role)
        if role not in VALID_ROLES:
            return "role 必须是 executioner、defender、builder 或 judge"

        try:
            limit = int(raw_limit)
        except ValueError:
            return "max_clones 必须是整数"

        if limit < 1:
            return "max_clones 必须 >= 1"
        if limit > MAX_CONFIG_CLONES:
            return f"第一版 max_clones 上限是 {MAX_CONFIG_CLONES}，防止成本和噪音失控。"

        state["clone_limits"][role] = limit
        state = increment_round(state)
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            f"/config {role} {limit}",
            {"command": "config", "role": role, "limit": limit},
        )
        self.storage.save_snapshot(state["session_id"], state)
        return f"已设置 clone_limits.{role} = {limit}"

    def cmd_close(self, arg: str = "") -> str:
        option = arg.strip()
        if option and option != "--force":
            return "用法：/close 或 /close --force"

        force = option == "--force"
        state = self._require_state()
        if self._needs_judge_before_close(state) and not force:
            return self._close_refusal_message()

        closing, updated = self._close_question(state, force=force)
        updated["chair_mode"] = "manual"
        updated["requires_user_intervention"] = True
        self._save_state(updated)
        return closing

    def handle_user_message(self, text: str) -> str:
        state = self._require_state()
        text = text.strip()
        if not text:
            return ""
        if self._is_contextual_question(text):
            return self._handle_contextual_question(state, text)

        state = increment_round(state)
        previous_position = state.get("user_position", "")
        state["user_position"] = merge_user_position(previous_position, text)
        state["requires_user_intervention"] = False
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            text,
            {
                "type": "user_position_update",
                "previous_user_position": previous_position,
                "updated_user_position": state["user_position"],
            },
        )
        self.storage.save_snapshot(state["session_id"], state)
        return "已合并用户输入到 user_position，而不是覆盖旧约束。可继续 /auto 3、/exec、/spawn、/defend、/build、/judge、/summary 或 /close。"

    def _handle_contextual_question(self, state: Dict[str, Any], text: str) -> str:
        recent_before = self.storage.recent_turns(state["session_id"], limit=10)
        state = increment_round(state)
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            text,
            {"type": "contextual_question", "user_position_unchanged": True},
        )

        answer = self._answer_contextual_question(state, text, recent_before)
        state = increment_round(state)
        state["requires_user_intervention"] = True
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "assistant",
            answer,
            {"type": "contextual_answer", "user_position_unchanged": True},
        )
        self.storage.save_snapshot(state["session_id"], state)
        return answer + "\n\n（这次追问没有写入 user_position；如果你是在新增长期约束，可以直接说“我的约束是...”。）"

    def _chair_step(self, auto_step: int) -> str:
        state = self._require_state()
        recent = self.storage.recent_turns(state["session_id"], limit=8)
        self._progress(f"auto step {auto_step}: asking Chair for the next move")
        raw_decision = self.llm.call_agent(
            "chair",
            state=state,
            recent_turns=recent,
            current_task="Decide next deliberation step.",
        )
        decision = validate_chair_decision(raw_decision, state)

        state = increment_round(state)
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "chair",
            json.dumps(decision, ensure_ascii=False, indent=2),
            {"auto_step": auto_step, "type": "chair_decision"},
        )
        state = apply_state_patch(state, decision.get("state_patch", {}))
        self.storage.save_snapshot(state["session_id"], state)

        decision_type = decision["decision_type"]
        self._progress(f"auto step {auto_step}: Chair decided {decision_type}")
        self._emit_chair_decision(decision)
        if decision_type == "wait_user":
            state["requires_user_intervention"] = True
            self._save_state(state)
            return self._format_chair_decision(decision)

        if decision_type in {"call_role", "judge"}:
            role = decision.get("role_to_call") or ("judge" if decision_type == "judge" else None)
            if role not in VALID_ROLES:
                return f"Chair 决策无效：role_to_call={role}"
            self._progress(f"auto step {auto_step}: running {role}")
            result, _ = self._run_role(role, task=decision.get("message_to_user") or decision.get("reason") or "")
            return self._format_chair_decision(decision) + "\n\n" + self._format_agent_result(result)

        if decision_type == "spawn_clones":
            role = decision.get("role_to_call")
            if role not in VALID_ROLES:
                return f"Chair 决策无效：role_to_call={role}"
            limit = int(state.get("clone_limits", {}).get(role, 1))
            requested = int(decision.get("clone_count") or 1)
            count = max(1, min(requested, limit))
            clone_tasks = sanitize_clone_tasks(
                decision.get("clone_tasks") or self._default_clone_tasks(role, count),
                role,
                count,
            )
            self._progress(f"auto step {auto_step}: running {count} {role} clone(s)")
            result, _ = self._run_role(role, task=decision.get("reason") or "", clone_tasks=clone_tasks)
            return self._format_chair_decision(decision) + "\n\n" + self._format_agent_result(result)

        if decision_type == "close_question":
            if self._needs_judge_before_close(state):
                state["requires_user_intervention"] = True
                self._save_state(state)
                return self._close_refusal_message()
            self._progress(f"auto step {auto_step}: closing current major question")
            closing, _ = self._close_question(state, existing_decision=decision)
            return closing

        return f"Chair decision_type 未实现：{decision_type}\n{json.dumps(decision, ensure_ascii=False, indent=2)}"

    def _run_role(
        self,
        role: str,
        task: str,
        clone_tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        base_state = self._require_state()
        base_state_snapshot = ensure_shape(copy.deepcopy(base_state))
        recent_snapshot = self.storage.recent_turns(base_state_snapshot["session_id"], limit=8)

        if clone_tasks is None:
            clone_tasks = [{"name": role, "angle": "single instance", "task": task}]
        clone_tasks = sanitize_clone_tasks(clone_tasks, role, len(clone_tasks))
        self._progress(f"running {role}: {len(clone_tasks)} instance(s)")
        clone_group_id = ""
        if len(clone_tasks) > 1:
            clone_group_id = f"{role}-{base_state_snapshot.get('round', 0) + 1}-{uuid.uuid4().hex[:8]}"
            self.storage.start_clone_run(
                clone_group_id,
                base_state_snapshot["session_id"],
                role,
            )

        outputs: List[Dict[str, Any]] = []
        run_state = ensure_shape(copy.deepcopy(base_state_snapshot))

        try:
            for index, clone in enumerate(clone_tasks, start=1):
                # All clones receive the same state and recent-turn snapshot. No clone
                # sees state patches or turns produced by siblings in this batch.
                clone_name = clone.get("name") or role
                self._progress(f"calling {clone_name} ({index}/{len(clone_tasks)})")
                raw_output = self.llm.call_agent(
                    role,
                    state=copy.deepcopy(base_state_snapshot),
                    recent_turns=copy.deepcopy(recent_snapshot),
                    current_task=clone.get("task") or task,
                    clone_name=clone_name,
                    angle=clone.get("angle"),
                )
                self._progress(f"{clone_name} returned; saving turn")
                output = validate_agent_output(role, raw_output, base_state_snapshot)
                self._emit_agent_result(clone_name, output)
                outputs.append(output)

                run_state = increment_round(run_state)
                self.storage.save_turn(
                    run_state["session_id"],
                    run_state["round"],
                    clone.get("name") or role,
                    json.dumps(output, ensure_ascii=False, indent=2),
                    {
                        "role": role,
                        "angle": clone.get("angle"),
                        "clone_group_id": clone_group_id or None,
                        "clone_independent": len(clone_tasks) > 1,
                        "independent_snapshot": len(clone_tasks) > 1,
                        "input_state_round": base_state_snapshot.get("round"),
                        "state_snapshot_round": base_state_snapshot.get("round"),
                        "state_patch_applied": len(clone_tasks) == 1,
                    },
                )

            if len(outputs) > 1:
                self._progress(f"merging {len(outputs)} {role} clone output(s)")
                merged = merge_clone_outputs(role, base_state_snapshot, outputs)
                self._emit_agent_result("merger", merged)
                run_state = increment_round(run_state)
                self.storage.save_turn(
                    run_state["session_id"],
                    run_state["round"],
                    "merger",
                    json.dumps(merged, ensure_ascii=False, indent=2),
                    {
                        "source_role": role,
                        "clone_group_id": clone_group_id,
                        "merged_after_independent_clones": True,
                        "merged_independent_clones": True,
                        "input_state_round": base_state_snapshot.get("round"),
                        "state_snapshot_round": base_state_snapshot.get("round"),
                    },
                )
                updated = apply_state_patch(run_state, merged.get("state_patch", {}))
                if role == "judge" and updated.get("judge_verdict") in {"KILL", "REDESIGN", "BUILD"}:
                    updated["requires_user_intervention"] = True
                self.storage.save_snapshot(updated["session_id"], updated)
                self.storage.complete_clone_run(clone_group_id)
                self._progress(f"{role} clone group completed")
                return merged, updated

            updated = apply_state_patch(run_state, outputs[0].get("state_patch", {}))
            if role == "judge" and updated.get("judge_verdict") in {"KILL", "REDESIGN", "BUILD"}:
                updated["requires_user_intervention"] = True
            self.storage.save_snapshot(updated["session_id"], updated)
            self._progress(f"{role} completed")
            return outputs[0], updated

        except Exception as exc:
            if clone_group_id:
                error = f"{type(exc).__name__}: {exc}"
                self.storage.fail_clone_run(clone_group_id, error)
                run_state = increment_round(run_state)
                self.storage.save_turn(
                    run_state["session_id"],
                    run_state["round"],
                    "system",
                    f"clone_group_failed: {error}",
                    {
                        "clone_group_id": clone_group_id,
                        "role": role,
                        "status": "failed",
                        "error": error,
                        "outputs_completed": len(outputs),
                    },
                )
                self.storage.save_snapshot(run_state["session_id"], run_state)
            raise

    def _close_question(
        self,
        state: Dict[str, Any],
        existing_decision: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        state = ensure_shape(state)
        major_question = state.get("current_major_question", "")
        closed_without_judgement = self._needs_judge_before_close(state) and force
        recent = self.storage.recent_turns(state["session_id"], limit=8)

        if existing_decision is None:
            raw_decision = self.llm.call_agent(
                "chair",
                state=state,
                recent_turns=recent,
                current_task="Produce closing statement for current major question.",
            )
            decision = validate_chair_decision(raw_decision, state)
        else:
            decision = validate_chair_decision(existing_decision, state)

        closing = decision.get("closing_statement") or self._fallback_closing_statement(state)
        if closed_without_judgement and "closed_without_judgement: true" not in closing:
            closing = closing.rstrip() + "\n\nclosed_without_judgement: true"

        state = increment_round(state)
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "chair",
            closing,
            {
                "type": "closing_statement",
                "closed_without_judgement": closed_without_judgement,
            },
        )
        state = apply_state_patch(state, decision.get("state_patch", {}))
        state["requires_user_intervention"] = True
        state = append_major_question_history(
            state,
            major_question=major_question,
            closing_statement=closing,
            closed_without_judgement=closed_without_judgement,
        )
        self.storage.save_snapshot(state["session_id"], state)
        return closing, state

    def _needs_judge_before_close(self, state: Dict[str, Any]) -> bool:
        return ensure_shape(state).get("judge_verdict") == "undecided"

    def _bootstrap_initial_major_question(self, state: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        task = (
            "Bootstrap initial major question for a new session. "
            "Generate exactly one topic-specific current_major_question from state.core_claim. "
            "Do not call any role, do not attack, defend, judge, or close. "
            "The question must clarify definitions, boundaries, success/failure criteria, "
            "or the smallest falsifiable scenario."
        )
        fallback_question = self._fallback_initial_major_question(state.get("core_claim", ""))
        question = fallback_question
        source = "fallback"
        metadata: Dict[str, Any] = {"type": "initial_major_question", "source": source}
        content: Dict[str, Any] = {
            "current_major_question": question,
            "reason": "fallback_initial_major_question",
        }

        try:
            self._progress("start: asking Chair to generate the initial major question")
            raw_decision = self.llm.call_agent(
                "chair",
                state=state,
                recent_turns=self.storage.recent_turns(state["session_id"], limit=4),
                current_task=task,
            )
            decision = validate_chair_decision(raw_decision, state)
            candidate = self._clean_major_question(
                (decision.get("state_patch") or {}).get("current_major_question")
            )
            if candidate:
                question = candidate
                source = "chair"
                metadata = {
                    "type": "initial_major_question",
                    "source": source,
                    "validation_warnings": decision.get("validation_warnings") or [],
                }
                content = {
                    "current_major_question": question,
                    "reason": decision.get("reason") or "",
                    "message_to_user": decision.get("message_to_user") or "",
                }
            else:
                metadata["reason"] = "chair_returned_no_current_major_question"
        except Exception as exc:
            metadata["error"] = f"{type(exc).__name__}: {exc}"

        state["current_major_question"] = question
        state["requires_user_intervention"] = False
        state = increment_round(state)
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "chair",
            json.dumps(content, ensure_ascii=False, indent=2),
            metadata,
        )
        if source == "chair":
            self._emit_inline("chair", f"initial major question: {question}")
        else:
            self._emit_inline("chair", f"fallback initial major question: {question}")
        return state, source

    def _fallback_initial_major_question(self, idea: str) -> str:
        idea = self._clip_inline_text(idea.strip() or "这个想法", limit=80)
        return f"围绕「{idea}」，最需要先澄清的核心定义、适用边界和可证伪标准是什么？"

    def _is_contextual_question(self, text: str) -> bool:
        normalized = "".join(str(text or "").strip().lower().split())
        if not normalized:
            return False
        durable_markers = (
            "我的约束是",
            "我的目标是",
            "我的想法是",
            "我希望",
            "我不想",
            "我可以接受",
            "我不能接受",
            "场景是",
            "补充一个约束",
        )
        question_markers = ("?", "？")
        contextual_markers = (
            "这是什么意思",
            "什么意思",
            "什么的方案",
            "什么方案",
            "指什么",
            "具体指",
            "这里的",
            "上面",
            "上一条",
            "刚才",
            "这段",
            "其中",
            "为什么",
            "怎么理解",
            "解释",
            "展开",
            "举例",
            "说清楚",
            "哪",
        )
        if any(marker in normalized for marker in contextual_markers):
            return True
        if any(marker in text for marker in question_markers):
            return not any(marker in normalized for marker in durable_markers)
        return False

    def _answer_contextual_question(
        self,
        state: Dict[str, Any],
        question: str,
        recent_turns: List[Dict[str, Any]],
    ) -> str:
        fallback = self._fallback_contextual_answer(state, question, recent_turns)
        if self.config.mock_llm or not self.config.openai_api_key:
            return fallback

        self._progress("answering contextual follow-up from recent turns")
        payload = {
            "state": {
                "core_claim": state.get("core_claim", ""),
                "current_major_question": state.get("current_major_question", ""),
                "user_position": state.get("user_position", ""),
                "strongest_attack": state.get("strongest_attack", ""),
                "strongest_defense": state.get("strongest_defense", ""),
                "best_redesign": state.get("best_redesign", ""),
                "judge_verdict": state.get("judge_verdict", "undecided"),
            },
            "recent_turns": recent_turns[-8:],
            "user_question": question,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 killme-lite 的上下文解释器，不是新的审议角色。"
                    "用户正在追问最近某段输出是什么意思。"
                    "只基于 state 和 recent_turns 回答，不要更新长期用户立场，不要给出新的裁决，"
                    "不要把追问当成新需求。"
                    "说明用户问的是哪个角色/哪段输出、这段话指的是什么、不指什么、下一步可怎么操作。"
                    "只返回 JSON：{\"answer\":\"...\"}。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]
        try:
            raw = self.llm.chat_completion(messages)
            parsed = extract_json(raw)
            if isinstance(parsed, dict):
                answer = self._stringify_summary(parsed.get("answer"))
                if answer:
                    return answer
        except Exception as exc:
            self._progress(f"contextual follow-up fell back locally: {type(exc).__name__}: {exc}")
        return fallback

    def _fallback_contextual_answer(
        self,
        state: Dict[str, Any],
        question: str,
        recent_turns: List[Dict[str, Any]],
    ) -> str:
        target_turn = self._last_substantive_turn(recent_turns)
        if not target_turn:
            return (
                "你这句是在追问上下文，但我没有找到可解释的上一条角色输出。"
                "可以先运行 /summary 看当前状态，或指出你要问的是哪一段。"
            )

        parsed = self._parse_turn_content(target_turn.get("content", ""))
        speaker = target_turn.get("speaker", "上一条输出")
        role = parsed.get("role") if isinstance(parsed, dict) else ""
        summary = self._summarize_result(parsed) if isinstance(parsed, dict) else ""
        if not summary:
            summary = self._clip_inline_text(str(target_turn.get("content", "")), limit=260)
        if summary.startswith("strongest: "):
            summary = summary.removeprefix("strongest: ")

        display_role = role or speaker
        if role == "builder" or "builder" in speaker.lower():
            claim_context = " ".join(
                [
                    str(state.get("core_claim") or ""),
                    str(state.get("current_major_question") or ""),
                    summary,
                ]
            )
            full_build = "完整客服 Agent" if "客服" in claim_context else "完整系统"
            return (
                "你问的是上一条 Builder 输出里的“方案”。这里的方案不是最终产品方案，"
                "而是针对当前 major question 的最小验证/证伪方案。\n\n"
                f"它要解决的上下文是：{state.get('current_major_question')}\n\n"
                f"核心意思是：{summary}\n\n"
                f"换句话说，Builder 不是说现在就做{full_build}，而是在建议先用更小的样本、"
                "mock 或离线验证和明确指标判断这个方向是否值得继续。"
            )

        return (
            f"你问的是上一条 {display_role} 输出。\n\n"
            f"它对应的当前问题是：{state.get('current_major_question')}\n\n"
            f"核心意思是：{summary}\n\n"
            "这类追问只用于解释上下文，不会被当成新的长期用户约束。"
        )

    def _last_substantive_turn(self, turns: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for turn in reversed(turns):
            speaker = str(turn.get("speaker") or "").lower()
            metadata = turn.get("metadata") or {}
            if speaker in {"user", "system", "assistant"}:
                continue
            if metadata.get("type") == "chair_decision":
                continue
            return turn
        for turn in reversed(turns):
            speaker = str(turn.get("speaker") or "").lower()
            if speaker not in {"user", "system", "assistant"}:
                return turn
        return None

    def _parse_turn_content(self, content: str) -> Any:
        try:
            return json.loads(content or "")
        except json.JSONDecodeError:
            return {}

    def _export_session(self, session_id: str) -> Tuple[Path, Path]:
        data = self.storage.export_session_data(session_id)
        export_dir = self.base_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        json_path = export_dir / f"{session_id}.json"
        markdown_path = export_dir / f"{session_id}.md"
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(self._format_export_markdown(data), encoding="utf-8")
        return json_path, markdown_path

    def _format_export_markdown(self, data: Dict[str, Any]) -> str:
        session = data.get("session") or {}
        state = data.get("latest_state") or {}
        history = state.get("major_question_history") or []
        turns = data.get("turns") or []
        clone_runs = data.get("clone_runs") or []

        lines = [
            f"# killme-lite session export: {session.get('session_id', '')}",
            "",
            "## Session",
            "",
            f"- title: {session.get('title', '')}",
            f"- session_id: {session.get('session_id', '')}",
            f"- created_at: {session.get('created_at', '')}",
            f"- updated_at: {session.get('updated_at', '')}",
            f"- closed_at: {session.get('closed_at') or 'null'}",
            f"- exported_at: {data.get('exported_at', '')}",
            "",
            "## Latest State",
            "",
            f"- round: {state.get('round', 0)}",
            f"- core_claim: {state.get('core_claim', '')}",
            f"- current_major_question: {state.get('current_major_question', '')}",
            f"- user_position: {state.get('user_position', '')}",
            f"- strongest_attack: {state.get('strongest_attack') or '暂无'}",
            f"- strongest_defense: {state.get('strongest_defense') or '暂无'}",
            f"- best_redesign: {state.get('best_redesign') or '暂无'}",
            f"- judge_verdict: {state.get('judge_verdict', 'undecided')}",
            f"- pending_next_question: {state.get('pending_next_question') or '暂无'}",
            "",
            "## Major Question History",
            "",
        ]

        if history:
            for index, item in enumerate(history, start=1):
                lines.extend(
                    [
                        f"### {index}. {item.get('major_question', '')}",
                        "",
                        f"- round: {item.get('round', item.get('round_closed', ''))}",
                        f"- judge_verdict: {item.get('judge_verdict', '')}",
                        f"- next_major_question: {item.get('next_major_question') or '暂无'}",
                        f"- closed_without_judgement: {item.get('closed_without_judgement', False)}",
                        "",
                        "Closing statement:",
                        "",
                        self._markdown_block(item.get("closing_statement", "")),
                        "",
                    ]
                )
        else:
            lines.extend(["暂无", ""])

        lines.extend(["## Turns", ""])
        if turns:
            for turn in turns:
                lines.extend(
                    [
                        f"### Round {turn.get('round')} - {turn.get('speaker')}",
                        "",
                        f"- id: {turn.get('id')}",
                        f"- created_at: {turn.get('created_at')}",
                        "",
                    ]
                )
                metadata = turn.get("metadata") or {}
                if metadata:
                    lines.extend(["Metadata:", "", self._json_block(metadata), ""])
                lines.extend(["Content:", "", self._markdown_block(turn.get("content", "")), ""])
        else:
            lines.extend(["暂无", ""])

        lines.extend(["## Clone Runs", ""])
        if clone_runs:
            lines.extend(
                [
                    "| clone_group_id | role | status | started_at | completed_at | error |",
                    "|---|---|---|---|---|---|",
                ]
            )
            for run in clone_runs:
                lines.append(
                    "| {group} | {role} | {status} | {started} | {completed} | {error} |".format(
                        group=self._escape_table_cell(str(run.get("clone_group_id", ""))),
                        role=self._escape_table_cell(str(run.get("role", ""))),
                        status=self._escape_table_cell(str(run.get("status", ""))),
                        started=self._escape_table_cell(str(run.get("started_at", ""))),
                        completed=self._escape_table_cell(str(run.get("completed_at") or "")),
                        error=self._escape_table_cell(str(run.get("error") or "")),
                    )
                )
        else:
            lines.append("暂无")

        return "\n".join(lines).rstrip() + "\n"

    def _json_block(self, value: Any) -> str:
        return "~~~json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n~~~"

    def _markdown_block(self, value: Any) -> str:
        return "~~~text\n" + str(value or "") + "\n~~~"

    def _escape_table_cell(self, value: str) -> str:
        return " ".join(str(value or "").replace("|", "\\|").split())

    def _clean_major_question(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        question = " ".join(value.strip().split())
        if len(question) < 8:
            return ""
        question = self._clip_inline_text(question, limit=260)
        if not question.endswith(("?", "？")):
            question += "？"
        return question

    def _clip_inline_text(self, value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _current_major_question_is_closed(self, state: Dict[str, Any]) -> bool:
        state = ensure_shape(state)
        history = state.get("major_question_history") or []
        if not history:
            return False
        return history[-1].get("major_question") == state.get("current_major_question")

    def _close_refusal_message(self) -> str:
        return "当前 Judge 还未裁决。请先运行 /judge，或使用 /close --force 强制关闭。"

    def _fallback_closing_statement(self, state: Dict[str, Any]) -> str:
        return f"""## Chair closing statement

Major question:
{state.get('current_major_question', '')}

What survived:
{'; '.join(state.get('surviving_arguments') or ['尚未形成明确幸存观点。'])}

What was killed:
{'; '.join(state.get('killed_arguments') or ['尚未形成明确淘汰观点。'])}

Main unresolved tension:
{state.get('strongest_attack', '')}

Current state update:
- Core claim: {state.get('core_claim', '')}
- User position: {state.get('user_position', '')}
- Strongest attack: {state.get('strongest_attack', '')}
- Strongest defense: {state.get('strongest_defense', '')}
- Best redesign: {state.get('best_redesign', '')}
- Judge verdict: {state.get('judge_verdict', 'undecided')}

Next major question:
{state.get('pending_next_question') or '下一步最值得验证的问题是什么？'}

Mode recommendation:
MANUAL

Reason:
需要用户确认下一问题或补充真实约束。"""

    def _format_summary(self, state: Dict[str, Any]) -> str:
        history_count = len(state.get("major_question_history") or [])
        open_questions = state.get("open_questions") or []
        last_open = open_questions[-3:] if open_questions else ["暂无"]
        return (
            "## Session summary\n"
            f"- session_id: {state.get('session_id')}\n"
            f"- round: {state.get('round')}\n"
            f"- mode: {state.get('chair_mode')}\n"
            f"- core_claim: {state.get('core_claim')}\n"
            f"- current_major_question: {state.get('current_major_question')}\n"
            f"- user_position: {state.get('user_position')}\n"
            f"- strongest_attack: {state.get('strongest_attack') or '暂无'}\n"
            f"- strongest_defense: {state.get('strongest_defense') or '暂无'}\n"
            f"- best_redesign: {state.get('best_redesign') or '暂无'}\n"
            f"- judge_verdict: {state.get('judge_verdict')}\n"
            f"- pending_next_question: {state.get('pending_next_question') or '暂无'}\n"
            f"- major_question_history_count: {history_count}\n"
            f"- recent_open_questions: {json.dumps(last_open, ensure_ascii=False)}\n"
            "- useful_commands: /auto 3, /exec, /spawn <role> <n>, /defend, /build, /judge, /close, /close --force"
        )

    def _format_chair_decision(self, decision: Dict[str, Any]) -> str:
        warnings = decision.get("validation_warnings") or []
        warning_text = f"\n- validation_warnings: {warnings}" if warnings else ""
        return (
            "### Chair decision\n"
            f"- decision_type: {decision.get('decision_type')}\n"
            f"- role_to_call: {decision.get('role_to_call')}\n"
            f"- clone_count: {decision.get('clone_count')}\n"
            f"- reason: {decision.get('reason')}\n"
            f"- message: {decision.get('message_to_user')}"
            f"{warning_text}"
        )

    def _format_agent_result(self, result: Dict[str, Any]) -> str:
        role = result.get("role", "agent")
        if role == "merger":
            return (
                "### Merger result\n"
                f"- source_role: {result.get('source_role')}\n"
                "- clone_input: same frozen state + recent turns snapshot\n"
                f"- strongest_point: {result.get('strongest_point')}\n"
                f"- merged_points: {json.dumps(result.get('merged_points', []), ensure_ascii=False, indent=2)}"
            )

        if role == "judge":
            return (
                "### Judge result\n"
                f"- verdict: {result.get('verdict')}\n"
                f"- confidence: {result.get('confidence')}\n"
                f"- reasoning: {result.get('reasoning_summary')}\n"
                f"- next_validation_action: {result.get('next_validation_action')}"
            )

        strongest = (
            result.get("strongest_attack")
            or result.get("strongest_defense")
            or result.get("best_redesign")
            or result.get("strongest_point")
            or ""
        )
        return f"### {role} result\n- strongest: {strongest}"

    def _default_clone_tasks(self, role: str, count: int) -> List[Dict[str, str]]:
        angles = {
            "executioner": [
                "attack definition ambiguity",
                "attack evidence gap",
                "attack execution complexity",
            ],
            "defender": [
                "defend from user value",
                "defend from minimal MVP feasibility",
            ],
            "builder": [
                "design the lightest MVP",
                "design the smallest falsifiable test",
            ],
            "judge": [
                "judge product value",
                "judge implementation complexity",
            ],
        }.get(role, ["single instance"])
        return [
            {
                "name": f"{role.capitalize()}-{i + 1}",
                "angle": angles[i % len(angles)],
                "task": f"Evaluate current major question from angle: {angles[i % len(angles)]}",
            }
            for i in range(count)
        ]

    def _require_state(self) -> Dict[str, Any]:
        state = self.storage.load_latest_state(self._active_session_id())
        if state is None:
            raise RuntimeError("没有 active session。请先运行 /start <idea>")
        return ensure_shape(state)

    def _active_session_id(self) -> Optional[str]:
        return self.storage.get_active_session_id() or self.storage.latest_session_id()

    def _save_state(self, state: Dict[str, Any]) -> None:
        state = ensure_shape(state)
        self.storage.save_snapshot(state["session_id"], state)

    def _progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(f"[running] {message}")

    def _emit_chair_decision(self, decision: Dict[str, Any]) -> None:
        if self.progress is None:
            return
        decision_type = decision.get("decision_type") or "unknown"
        role = decision.get("role_to_call") or "-"
        clone_count = int(decision.get("clone_count") or 0)
        target = role
        if decision_type == "spawn_clones":
            target = f"{role} x{clone_count}"
        reason = self._clip_inline_text(str(decision.get("reason") or ""), limit=180)
        suffix = f": {reason}" if reason else ""
        self.progress(f"[chair] {decision_type} -> {target}{suffix}")

    def _emit_agent_result(self, speaker: str, result: Dict[str, Any]) -> None:
        if self.progress is None:
            return
        summary = self._summarize_result(result)
        if summary:
            self.progress(f"[{speaker}] {summary}")

    def _emit_inline(self, speaker: str, message: str) -> None:
        if self.progress is not None:
            self.progress(f"[{speaker}] {self._clip_inline_text(message, limit=260)}")

    def _summarize_result(self, result: Dict[str, Any]) -> str:
        role = result.get("role") or "agent"
        if role == "merger":
            strongest = self._stringify_summary(result.get("strongest_point"))
            if not strongest:
                strongest = self._stringify_summary((result.get("merged_points") or [""])[0])
            return f"strongest merged point: {self._clip_inline_text(strongest, limit=220)}"

        if role == "judge":
            verdict = result.get("verdict") or (result.get("state_patch") or {}).get("judge_verdict") or "undecided"
            confidence = result.get("confidence") or "unknown"
            reasoning = self._stringify_summary(result.get("reasoning_summary"))
            reasoning = self._clip_inline_text(reasoning, limit=180)
            return f"verdict: {verdict} ({confidence})" + (f"; {reasoning}" if reasoning else "")

        strongest = (
            result.get("strongest_attack")
            or result.get("strongest_defense")
            or result.get("best_redesign")
            or result.get("strongest_point")
            or ""
        )
        strongest = self._stringify_summary(strongest)
        if not strongest:
            return f"{role} returned"
        return f"strongest: {self._clip_inline_text(strongest, limit=220)}"

    def _stringify_summary(self, value: Any) -> str:
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, dict):
            for key in ("claim", "proposal", "summary", "point", "text"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    return " ".join(text.split())
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        return str(value)

    def _split_command(self, line: str) -> Tuple[str, str]:
        parts = line.strip().split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        return command, arg
