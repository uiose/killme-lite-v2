import copy
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import AppConfig, load_config
from llm import LLMClient, extract_json
from merger import merge_clone_outputs
from state import (
    INITIAL_USER_POSITION,
    MAX_CLONE_LIMIT,
    append_major_question_history,
    apply_state_patch,
    ensure_shape,
    increment_round,
    merge_user_position,
    new_state,
    normalize_clone_mode,
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
DECISION_AUTO_MAX_STEPS = 3
EXPLORATION_AUTO_MAX_STEPS = 12
AUTO_MAX_STEPS = DECISION_AUTO_MAX_STEPS
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
        self.progress = progress
        self.llm = LLMClient(base_dir=self.base_dir, config=self.config)
        self.llm.progress = self._progress

    def handle_line(self, line: str) -> str:
        begin_command = getattr(self.llm, "begin_command", None)
        end_command = getattr(self.llm, "end_command", None)
        if callable(begin_command):
            begin_command()
        try:
            if line.startswith("/"):
                return self.handle_command(line)
            return self.handle_user_message(line)
        finally:
            if callable(end_command):
                end_command()

    def handle_command(self, line: str) -> str:
        command, arg = self._split_command(line)

        if command == "/start":
            return self.cmd_start(arg)

        if command == "/explore":
            return self.cmd_explore(arg)

        if command == "/auto":
            return self.cmd_auto(arg)

        if command == "/manual":
            return self.cmd_manual()

        if command == "/mode":
            return self.cmd_mode(arg)

        if command in {"/clone-mode", "/clones"}:
            return self.cmd_clone_mode(arg)

        if command == "/focus":
            return self.cmd_focus(arg)

        if command in {"/map", "/exploration-map"}:
            return self.cmd_map()

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

        if command in {"/position", "/user-position"}:
            return self.cmd_position(arg)

        if command == "/evidence":
            return self.cmd_evidence(arg)

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

        state = new_state(self.base_dir, idea, agenda_mode="decision")
        self.storage.create_session(state["session_id"], title=idea[:80])
        self.storage.set_active_session_id(state["session_id"])
        self.storage.save_turn(state["session_id"], state["round"], "user", f"/start {idea}")
        state, question_source = self._bootstrap_initial_major_question(state)
        self.storage.save_snapshot(state["session_id"], state)

        return (
            f"已创建 decision session：{state['session_id']}\n"
            f"Core claim：{state['core_claim']}\n"
            f"Agenda mode：{state['agenda_mode']}\n"
            f"Current major question：{state['current_major_question']}\n"
            f"Question source：{question_source}\n"
            f"建议下一步：/auto {DECISION_AUTO_MAX_STEPS} 或 /exec；开放探究请用 /explore <question>"
        )

    def cmd_explore(self, question: str) -> str:
        question = question.strip()
        if not question:
            return "用法：/explore <open-ended question>"

        state = new_state(self.base_dir, question, agenda_mode="exploration")
        self.storage.create_session(state["session_id"], title=question[:80])
        self.storage.set_active_session_id(state["session_id"])
        self.storage.save_turn(state["session_id"], state["round"], "user", f"/explore {question}")
        state, question_source = self._bootstrap_initial_major_question(state)
        self.storage.save_snapshot(state["session_id"], state)

        return (
            f"已创建 exploration session：{state['session_id']}\n"
            f"Exploration focus：{state.get('exploration_focus') or state['core_claim']}\n"
            f"Agenda mode：{state['agenda_mode']}\n"
            f"Exploration frame：{state['current_major_question']}\n"
            f"Frame source：{question_source}\n"
            f"Clone mode：{state.get('exploration_clone_mode', 'independent')}\n"
            f"建议下一步：/auto {min(6, EXPLORATION_AUTO_MAX_STEPS)}、/map、/clone-mode visible、/spawn defender 2 或 /evidence request <keywords>"
        )

    def cmd_auto(self, arg: str) -> str:
        state = self._require_state()
        auto_limit = self._auto_max_steps(state)
        try:
            steps = int(arg.strip() or "1")
        except ValueError:
            return f"用法：/auto <n>，当前模式最多 /auto {auto_limit}"

        if steps < 1:
            return "/auto 的步数必须 >= 1"

        if steps > auto_limit:
            mode = state.get("agenda_mode", "decision")
            return (
                f"/auto {steps} 超过当前 {mode} mode 上限 {auto_limit}。"
                "decision mode 保持短促收束；exploration mode 允许更长扫描，但仍保留上限以防无限循环。"
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

    def cmd_mode(self, arg: str = "") -> str:
        state = self._require_state()
        value = arg.strip().lower()
        aliases = {
            "explore": "exploration",
            "exploratory": "exploration",
            "research": "exploration",
            "探索": "exploration",
            "探究": "exploration",
            "decide": "decision",
            "decision-node": "decision",
            "决策": "decision",
            "裁决": "decision",
        }
        value = aliases.get(value, value)
        if not value:
            return (
                "## Mode\n"
                f"- agenda_mode: {state.get('agenda_mode', 'decision')}\n"
                f"- chair_mode: {state.get('chair_mode', 'manual')}\n"
                f"- exploration_status: {state.get('exploration_status', 'open')}\n"
                f"- exploration_clone_mode: {state.get('exploration_clone_mode', 'independent')}\n\n"
                "用法：/mode exploration 或 /mode decision；clone 可见性用 /clone-mode visible|independent"
            )
        if value not in {"decision", "exploration"}:
            return "用法：/mode exploration 或 /mode decision"

        previous = state.get("agenda_mode", "decision")
        state = increment_round(state)
        state["agenda_mode"] = value
        state["requires_user_intervention"] = True
        if value == "exploration":
            state["exploration_status"] = "open"
            if not state.get("exploration_focus"):
                state["exploration_focus"] = state.get("core_claim", "")
        else:
            state["exploration_status"] = "paused"
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            f"/mode {value}",
            {"command": "mode", "previous_agenda_mode": previous, "agenda_mode": value},
        )
        self.storage.save_snapshot(state["session_id"], state)
        return f"已切换 agenda_mode：{previous} -> {value}。当前 Chair 会等待用户下一步。"

    def cmd_clone_mode(self, arg: str = "") -> str:
        state = self._require_state()
        value = normalize_clone_mode(arg.strip())
        if not value:
            return (
                "## Clone mode\n"
                f"- exploration_clone_mode: {state.get('exploration_clone_mode', 'independent')}\n\n"
                "用法：/clone-mode independent 或 /clone-mode visible"
            )
        previous = state.get("exploration_clone_mode", "independent")
        state = increment_round(state)
        state["exploration_clone_mode"] = value
        state["requires_user_intervention"] = True
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            f"/clone-mode {value}",
            {"command": "clone-mode", "previous": previous, "exploration_clone_mode": value},
        )
        self.storage.save_snapshot(state["session_id"], state)
        if value == "visible":
            return f"已切换 exploration_clone_mode：{previous} -> visible。探索模式下同批 clone 会按顺序看到前面 sibling 的输出，但 state 仍等 Merger 后统一写入。"
        return f"已切换 exploration_clone_mode：{previous} -> independent。同批 clone 将继续使用冻结快照独立评审。"

    def cmd_focus(self, arg: str = "") -> str:
        state = self._require_state()
        focus = arg.strip()
        if not focus:
            return "用法：/focus <exploration branch or subquestion>"
        if state.get("agenda_mode") != "exploration":
            return "只有 agenda_mode=exploration 时才能 /focus。请先 /mode exploration。"
        state = increment_round(state)
        previous_focus = state.get("exploration_focus", "")
        state["exploration_focus"] = focus
        state["current_major_question"] = f"围绕「{focus}」，它关联哪些假设、证据、异常点和覆盖缺口，下一轮应如何继续探索？"
        state["exploration_status"] = "open"
        node = {
            "id": f"focus:{uuid.uuid4().hex[:8]}",
            "type": "focus",
            "label": focus,
            "summary": "用户选择的探索分支",
            "source_role": "user",
        }
        edge = {
            "source": "focus:current",
            "target": node["id"],
            "relation": "refocused_to",
            "summary": f"Exploration focus changed from {previous_focus or 'initial focus'} to {focus}",
        }
        state["exploration_nodes"] = self._append_unique_state_item(state.get("exploration_nodes"), node)
        state["exploration_edges"] = self._append_unique_state_item(state.get("exploration_edges"), edge)
        state["research_threads"] = self._append_unique_state_item(state.get("research_threads"), focus)
        state["requires_user_intervention"] = False
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            f"/focus {focus}",
            {"command": "focus", "previous_focus": previous_focus, "new_focus": focus},
        )
        self.storage.save_snapshot(state["session_id"], state)
        return "已切换探索焦点。\n\n" + self._format_exploration_map(state)

    def cmd_map(self) -> str:
        state = self._require_state()
        return self._format_exploration_map(state)

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

        clone_tasks = self._default_clone_tasks(role, count, state.get("agenda_mode"))
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

    def cmd_position(self, arg: str = "") -> str:
        state = self._require_state()
        action, value = self._split_position_arg(arg)

        if action in {"", "show"}:
            return self._format_user_position(state)

        if action not in {"set", "add", "clear"}:
            return "用法：/position [show] | /position set <text> | /position add <text> | /position clear"

        previous_position = state.get("user_position", "")
        if action == "clear":
            updated_position = INITIAL_USER_POSITION
        else:
            if not value:
                return f"用法：/position {action} <text>"
            if action == "set":
                updated_position = value.strip()
            else:
                updated_position = merge_user_position(previous_position, value)

        state = increment_round(state)
        state["user_position"] = updated_position
        state["requires_user_intervention"] = False
        state = ensure_shape(state)
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            f"/position {action}" + (f" {value}" if value else ""),
            {
                "type": "manual_user_position_update",
                "action": action,
                "previous_user_position": previous_position,
                "updated_user_position": state["user_position"],
            },
        )
        self.storage.save_snapshot(state["session_id"], state)
        return "已手动更新 user_position。\n\n" + self._format_user_position(state)

    def cmd_evidence(self, arg: str = "") -> str:
        state = self._require_state()
        action, value = self._split_evidence_arg(arg)

        if action in {"", "show", "list"}:
            return self._format_evidence_pack(state)

        if action == "requests":
            return self._format_evidence_requests(state)

        if action == "request":
            if not value:
                return "用法：/evidence request <search keywords or evidence need>"
            request = self._normalize_evidence_request({"query": value, "reason": "manual request"})
            state = increment_round(state)
            state["evidence_requests"] = self._append_unique_state_item(state.get("evidence_requests"), request)
            self.storage.save_turn(
                state["session_id"],
                state["round"],
                "user",
                f"/evidence request {value}",
                {"command": "evidence", "action": "request"},
            )
            self.storage.save_snapshot(state["session_id"], state)
            return "已记录 evidence request。\n\n" + self._format_evidence_requests(state)

        if action == "add":
            if not value:
                return "用法：/evidence add <text-or-json-object>"
            items = self._parse_evidence_items(value, source_hint="inline")
            return self._add_evidence_items(state, items, raw_command=f"/evidence add {value}", source="inline")

        if action == "import":
            if not value:
                return "用法：/evidence import <path-to-json-md-or-txt>"
            path = self._resolve_import_path(value)
            if not path.exists() or not path.is_file():
                return f"未找到 evidence 文件：{path}"
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8-sig")
            items = self._parse_evidence_items(text, source_hint=str(path))
            return self._add_evidence_items(state, items, raw_command=f"/evidence import {value}", source=str(path))

        return (
            "用法：/evidence [list|requests] | /evidence add <text-or-json-object> | "
            "/evidence import <path> | /evidence request <keywords>"
        )

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
        state["requires_user_intervention"] = True
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            text,
            {"type": "user_message", "user_position_unchanged": True},
        )
        self.storage.save_snapshot(state["session_id"], state)
        return (
            "已记录这条用户消息，但没有写入 user_position。\n"
            "如果这是长期约束或立场，请使用 /position add <text>；如果要覆盖，请使用 /position set <text>。"
        )

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
        return answer + "\n\n（这次追问没有写入 user_position；如果你是在新增长期约束，请使用 /position add <text>。）"

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
        original_agenda_mode = state.get("agenda_mode", "decision")
        if original_agenda_mode == "exploration":
            decision = self._guard_exploration_auto_decision(decision)

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
                decision.get("clone_tasks") or self._default_clone_tasks(role, count, state.get("agenda_mode")),
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

    def _guard_exploration_auto_decision(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        decision = dict(decision)
        patch = dict(decision.get("state_patch") or {})
        warnings = list(decision.get("validation_warnings") or [])

        if "agenda_mode" in patch:
            patch.pop("agenda_mode", None)
            warnings.append("exploration_auto_agenda_mode_patch_ignored")

        decision_type = decision.get("decision_type")
        role_to_call = decision.get("role_to_call")
        if decision_type in {"judge", "close_question"} or role_to_call == "judge":
            patch["requires_user_intervention"] = True
            patch["exploration_status"] = "open"
            decision.update(
                {
                    "decision_type": "wait_user",
                    "role_to_call": None,
                    "clone_count": 0,
                    "clone_tasks": [],
                    "reason": (
                        "exploration mode blocks automatic judge/close; keep the map open unless the "
                        "user explicitly asks for /close synthesis or switches to /mode decision."
                    ),
                    "requires_user_intervention": True,
                    "message_to_user": (
                        "探索模式不会自动裁决或关闭。可以继续 /auto、/focus 深挖、补 evidence，"
                        "也可以在用户明确需要时才用 /mode decision。"
                    ),
                }
            )
            warnings.append("exploration_auto_judge_close_blocked")

        decision["state_patch"] = patch
        decision["validation_warnings"] = warnings
        return decision

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
        clone_context_mode = "independent"
        if (
            len(clone_tasks) > 1
            and base_state_snapshot.get("agenda_mode") == "exploration"
            and base_state_snapshot.get("exploration_clone_mode") == "visible"
        ):
            clone_context_mode = "visible"
        visible_recent = copy.deepcopy(recent_snapshot)

        try:
            for index, clone in enumerate(clone_tasks, start=1):
                # In decision mode, clones stay independent. In exploration mode,
                # /clone-mode visible lets later clones see sibling outputs as
                # brainstorming context, but state patches still wait for Merger.
                clone_name = clone.get("name") or role
                self._progress(f"calling {clone_name} ({index}/{len(clone_tasks)})")
                try:
                    raw_output = self.llm.call_agent(
                        role,
                        state=copy.deepcopy(base_state_snapshot),
                        recent_turns=copy.deepcopy(visible_recent if clone_context_mode == "visible" else recent_snapshot),
                        current_task=clone.get("task") or task,
                        clone_name=clone_name,
                        angle=clone.get("angle"),
                        clone_context_mode=clone_context_mode,
                    )
                except TypeError as exc:
                    if "clone_context_mode" not in str(exc):
                        raise
                    raw_output = self.llm.call_agent(
                        role,
                        state=copy.deepcopy(base_state_snapshot),
                        recent_turns=copy.deepcopy(visible_recent if clone_context_mode == "visible" else recent_snapshot),
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
                        "clone_independent": len(clone_tasks) > 1 and clone_context_mode == "independent",
                        "clone_context_mode": clone_context_mode,
                        "sibling_outputs_visible": clone_context_mode == "visible" and index > 1,
                        "independent_snapshot": len(clone_tasks) > 1,
                        "input_state_round": base_state_snapshot.get("round"),
                        "state_snapshot_round": base_state_snapshot.get("round"),
                        "state_patch_applied": len(clone_tasks) == 1,
                    },
                )
                if clone_context_mode == "visible":
                    visible_recent.append(
                        {
                            "round": run_state["round"],
                            "speaker": clone_name,
                            "content": json.dumps(output, ensure_ascii=False, indent=2),
                            "metadata": {
                                "role": role,
                                "angle": clone.get("angle"),
                                "clone_group_id": clone_group_id or None,
                                "clone_context_mode": clone_context_mode,
                            },
                        }
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
                        "merged_after_independent_clones": clone_context_mode == "independent",
                        "merged_independent_clones": clone_context_mode == "independent",
                        "clone_context_mode": clone_context_mode,
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
                current_task=(
                    "Produce exploration synthesis for current exploration frame. "
                    "Do not give a KILL/REDESIGN/TEST/BUILD verdict."
                    if state.get("agenda_mode") == "exploration"
                    else "Produce closing statement for current major question."
                ),
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
        state = ensure_shape(state)
        if state.get("agenda_mode") == "exploration":
            return False
        return state.get("judge_verdict") == "undecided"

    def _bootstrap_initial_major_question(self, state: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        agenda_mode = state.get("agenda_mode", "decision")
        if agenda_mode == "exploration":
            task = (
                "Bootstrap initial exploration frame for a new exploration session. "
                "Generate exactly one topic-specific current_major_question from state.core_claim, "
                "but treat it as an exploration frame, not a decision node. "
                "Do not call any role, do not attack, defend, judge, close, or force a verdict. "
                "The frame must preserve multiple hypotheses, source-discovery paths, adjacent concepts, "
                "evidence-hypothesis relations, anomalies, and open unknowns without reducing the "
                "question to yes/no feasibility or treating decision conversion as the default endpoint."
            )
        else:
            task = (
                "Bootstrap initial major question for a new decision session. "
                "Generate exactly one topic-specific current_major_question from state.core_claim. "
                "Do not call any role, do not attack, defend, judge, or close. "
                "The question must clarify definitions, boundaries, success/failure criteria, "
                "or the smallest falsifiable scenario."
            )
        fallback_question = self._fallback_initial_major_question(state.get("core_claim", ""), agenda_mode)
        question = fallback_question
        source = "fallback"
        metadata: Dict[str, Any] = {"type": "initial_major_question", "source": source}
        content: Dict[str, Any] = {
            "current_major_question": question,
            "reason": "fallback_initial_major_question",
        }

        try:
            if agenda_mode == "exploration":
                self._progress("start: asking Chair to generate the initial exploration frame")
            else:
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
                state = apply_state_patch(state, decision.get("state_patch", {}))
                metadata = {
                    "type": "initial_exploration_frame" if agenda_mode == "exploration" else "initial_major_question",
                    "source": source,
                    "agenda_mode": agenda_mode,
                    "validation_warnings": decision.get("validation_warnings") or [],
                }
                content = {
                    "current_major_question": question,
                    "exploration_focus": state.get("exploration_focus", ""),
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
        frame_label = "initial exploration frame" if agenda_mode == "exploration" else "initial major question"
        if source == "chair":
            self._emit_inline("chair", f"{frame_label}: {question}")
        else:
            self._emit_inline("chair", f"fallback {frame_label}: {question}")
        return state, source

    def _fallback_initial_major_question(self, idea: str, agenda_mode: str = "decision") -> str:
        idea = self._clip_inline_text(idea.strip() or "这个想法", limit=80)
        if agenda_mode == "exploration":
            return f"围绕「{idea}」，哪些定义、竞争性假设、相邻领域、证据关系和异常点值得先展开，而不急于形成裁决？"
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
        has_provider = getattr(self.llm, "has_configured_provider", None)
        has_real_llm = callable(has_provider) and has_provider()
        if self.config.mock_llm or not has_real_llm:
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
            f"- agenda_mode: {state.get('agenda_mode', 'decision')}",
            f"- core_claim: {state.get('core_claim', '')}",
            f"- exploration_focus: {state.get('exploration_focus') or '暂无'}",
            f"- exploration_status: {state.get('exploration_status') or '暂无'}",
            f"- exploration_clone_mode: {state.get('exploration_clone_mode') or 'independent'}",
            f"- current_major_question: {state.get('current_major_question', '')}",
            f"- user_position: {state.get('user_position', '')}",
            f"- strongest_attack: {state.get('strongest_attack') or '暂无'}",
            f"- strongest_defense: {state.get('strongest_defense') or '暂无'}",
            f"- best_redesign: {state.get('best_redesign') or '暂无'}",
            f"- judge_verdict: {state.get('judge_verdict', 'undecided')}",
            f"- pending_next_question: {state.get('pending_next_question') or '暂无'}",
            f"- evidence_items_count: {len(state.get('evidence_items') or [])}",
            f"- evidence_requests_count: {len(state.get('evidence_requests') or [])}",
            "",
            "## Exploration Map",
            "",
            self._json_block(
                {
                    "hypotheses": state.get("hypotheses") or [],
                    "research_threads": state.get("research_threads") or [],
                    "findings": state.get("findings") or [],
                    "coverage_gaps": state.get("coverage_gaps") or [],
                    "decision_candidates": state.get("decision_candidates") or [],
                    "anomalies": state.get("anomalies") or [],
                    "exploration_nodes": state.get("exploration_nodes") or [],
                    "exploration_edges": state.get("exploration_edges") or [],
                }
            ),
            "",
            "## Evidence Pack",
            "",
            self._json_block(
                {
                    "evidence_items": state.get("evidence_items") or [],
                    "evidence_requests": state.get("evidence_requests") or [],
                }
            ),
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
        if state.get("agenda_mode") == "exploration":
            return f"""## Chair exploration synthesis

Exploration frame:
{state.get('current_major_question', '')}

Exploration focus:
{state.get('exploration_focus') or state.get('core_claim', '')}

Hypotheses preserved:
{'; '.join(state.get('hypotheses') or ['尚未形成明确竞争性假设。'])}

Research threads:
{'; '.join(state.get('research_threads') or ['尚未形成明确研究线索。'])}

Findings so far:
{'; '.join(state.get('findings') or ['尚未导入或沉淀明确发现。'])}

Coverage gaps:
{'; '.join(state.get('coverage_gaps') or ['尚未记录覆盖缺口。'])}

Evidence requests:
{json.dumps(state.get('evidence_requests') or [], ensure_ascii=False, indent=2)}

Decision candidates, optional:
{'; '.join(state.get('decision_candidates') or state.get('open_questions') or ['暂无；探索可以继续，不必强行转裁决。'])}

Anomalies / weak signals:
{'; '.join(state.get('anomalies') or ['暂无明确异常点。'])}

Graph snapshot:
- nodes: {len(state.get('exploration_nodes') or [])}
- edges: {len(state.get('exploration_edges') or [])}

Mode recommendation:
MANUAL

Reason:
这是探索综合，不是 verdict。下一步可以补资料、/focus 深挖分支、继续 /auto，或在用户明确选择时才转入 decision。"""

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

    def _format_exploration_map(self, state: Dict[str, Any]) -> str:
        state = ensure_shape(state)
        nodes = state.get("exploration_nodes") or []
        edges = state.get("exploration_edges") or []
        hypotheses = state.get("hypotheses") or []
        threads = state.get("research_threads") or []
        findings = state.get("findings") or []
        gaps = state.get("coverage_gaps") or []
        candidates = state.get("decision_candidates") or []
        anomalies = state.get("anomalies") or []

        def tail(items: List[Any], limit: int = 8) -> str:
            return json.dumps(items[-limit:] if items else ["暂无"], ensure_ascii=False, indent=2)

        return (
            "## Exploration map\n"
            f"- agenda_mode: {state.get('agenda_mode', 'decision')}\n"
            f"- exploration_status: {state.get('exploration_status', 'open')}\n"
            f"- exploration_clone_mode: {state.get('exploration_clone_mode', 'independent')}\n"
            f"- exploration_focus: {state.get('exploration_focus') or state.get('core_claim')}\n"
            f"- current_frame: {state.get('current_major_question')}\n"
            f"- graph_nodes: {len(nodes)}\n"
            f"- graph_edges: {len(edges)}\n\n"
            "### Hypotheses\n"
            f"{tail(hypotheses)}\n\n"
            "### Research threads\n"
            f"{tail(threads)}\n\n"
            "### Findings\n"
            f"{tail(findings)}\n\n"
            "### Coverage gaps\n"
            f"{tail(gaps)}\n\n"
            "### Anomalies / weak signals\n"
            f"{tail(anomalies)}\n\n"
            "### Decision candidates (optional, not terminal)\n"
            f"{tail(candidates)}\n\n"
            "### Graph nodes\n"
            f"{tail(nodes, 12)}\n\n"
            "### Graph edges\n"
            f"{tail(edges, 12)}"
        )

    def _format_summary(self, state: Dict[str, Any]) -> str:
        history_count = len(state.get("major_question_history") or [])
        open_questions = state.get("open_questions") or []
        last_open = open_questions[-3:] if open_questions else ["暂无"]
        evidence_requests = state.get("evidence_requests") or []
        last_evidence_requests = evidence_requests[-3:] if evidence_requests else ["暂无"]
        hypotheses = state.get("hypotheses") or []
        threads = state.get("research_threads") or []
        findings = state.get("findings") or []
        gaps = state.get("coverage_gaps") or []
        candidates = state.get("decision_candidates") or []
        anomalies = state.get("anomalies") or []
        nodes = state.get("exploration_nodes") or []
        edges = state.get("exploration_edges") or []
        exploration_lines = ""
        if state.get("agenda_mode") == "exploration":
            exploration_lines = (
                "## Exploration map\n"
                f"- exploration_focus: {state.get('exploration_focus') or state.get('core_claim')}\n"
                f"- exploration_status: {state.get('exploration_status', 'open')}\n"
                f"- exploration_clone_mode: {state.get('exploration_clone_mode', 'independent')}\n"
                f"- graph_nodes: {len(nodes)}\n"
                f"- graph_edges: {len(edges)}\n"
                f"- recent_hypotheses: {json.dumps(hypotheses[-3:] if hypotheses else ['暂无'], ensure_ascii=False)}\n"
                f"- recent_research_threads: {json.dumps(threads[-3:] if threads else ['暂无'], ensure_ascii=False)}\n"
                f"- recent_findings: {json.dumps(findings[-3:] if findings else ['暂无'], ensure_ascii=False)}\n"
                f"- recent_coverage_gaps: {json.dumps(gaps[-3:] if gaps else ['暂无'], ensure_ascii=False)}\n"
                f"- recent_anomalies: {json.dumps(anomalies[-3:] if anomalies else ['暂无'], ensure_ascii=False)}\n"
                f"- decision_candidates_optional: {json.dumps(candidates[-3:] if candidates else ['暂无'], ensure_ascii=False)}\n"
            )
        return (
            "## Session summary\n"
            f"- session_id: {state.get('session_id')}\n"
            f"- round: {state.get('round')}\n"
            f"- chair_mode: {state.get('chair_mode')}\n"
            f"- agenda_mode: {state.get('agenda_mode', 'decision')}\n"
            f"- core_claim: {state.get('core_claim')}\n"
            f"- current_major_question: {state.get('current_major_question')}\n"
            f"- user_position: {state.get('user_position')}\n"
            f"- strongest_attack: {state.get('strongest_attack') or '暂无'}\n"
            f"- strongest_defense: {state.get('strongest_defense') or '暂无'}\n"
            f"- best_redesign: {state.get('best_redesign') or '暂无'}\n"
            f"- judge_verdict: {state.get('judge_verdict')}\n"
            f"- pending_next_question: {state.get('pending_next_question') or '暂无'}\n"
            f"- major_question_history_count: {history_count}\n"
            f"- evidence_items_count: {len(state.get('evidence_items') or [])}\n"
            f"- recent_evidence_requests: {json.dumps(last_evidence_requests, ensure_ascii=False)}\n"
            f"- recent_open_questions: {json.dumps(last_open, ensure_ascii=False)}\n"
            f"{exploration_lines}"
            "- useful_commands: /mode, /explore <question>, /map, /focus <branch>, /clone-mode visible|independent, /position, /evidence, /evidence add <text-or-json>, /auto <n>, /spawn <role> <n>, /exec, /defend, /build, /judge, /close"
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
        evidence_requests = result.get("evidence_requests") or (result.get("state_patch") or {}).get("evidence_requests") or []
        evidence_text = (
            "\n- evidence_requests: "
            + json.dumps(evidence_requests, ensure_ascii=False, indent=2)
            if evidence_requests
            else ""
        )
        state_patch = result.get("state_patch") or {}
        exploration_payload = {}
        for key in (
            "hypotheses",
            "research_threads",
            "findings",
            "coverage_gaps",
            "decision_candidates",
            "exploration_nodes",
            "exploration_edges",
            "anomalies",
        ):
            value = result.get(key) or state_patch.get(key)
            if value:
                exploration_payload[key] = value
        exploration_text = (
            "\n- exploration_updates: "
            + json.dumps(exploration_payload, ensure_ascii=False, indent=2)
            if exploration_payload
            else ""
        )
        if role == "merger":
            return (
                "### Merger result\n"
                f"- source_role: {result.get('source_role')}\n"
                f"- clone_input: {('visible sibling context' if self._require_state().get('exploration_clone_mode') == 'visible' and self._require_state().get('agenda_mode') == 'exploration' else 'same frozen state + recent turns snapshot')}\n"
                f"- strongest_point: {result.get('strongest_point')}\n"
                f"- merged_points: {json.dumps(result.get('merged_points', []), ensure_ascii=False, indent=2)}"
                f"{evidence_text}"
                f"{exploration_text}"
            )

        if role == "judge":
            return (
                "### Judge result\n"
                f"- verdict: {result.get('verdict')}\n"
                f"- confidence: {result.get('confidence')}\n"
                f"- reasoning: {result.get('reasoning_summary')}\n"
                f"- next_validation_action: {result.get('next_validation_action')}"
                f"{evidence_text}"
                f"{exploration_text}"
            )

        strongest = (
            result.get("strongest_attack")
            or result.get("strongest_defense")
            or result.get("best_redesign")
            or result.get("strongest_point")
            or ""
        )
        return f"### {role} result\n- strongest: {strongest}{evidence_text}{exploration_text}"

    def _default_clone_tasks(
        self,
        role: str,
        count: int,
        agenda_mode: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        if agenda_mode == "exploration":
            angles = {
                "executioner": [
                    "scan premature closure and false dichotomies",
                    "map missing evidence and source blind spots",
                    "mark anomalies and weak signals",
                ],
                "defender": [
                    "generate plausible competing hypotheses",
                    "connect adjacent concepts and literatures",
                    "map competing mechanism families",
                ],
                "builder": [
                    "design source discovery plan",
                    "design relationship graph and source scan",
                    "turn unknowns into next probes",
                ],
                "judge": [
                    "assess optional readiness for decision mode",
                    "identify what should remain open",
                ],
            }.get(role, ["single exploratory angle"])
            task_prefix = "Expand the exploration map from angle"
        else:
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
            task_prefix = "Evaluate current major question from angle"
        return [
            {
                "name": f"{role.capitalize()}-{i + 1}",
                "angle": angles[i % len(angles)],
                "task": f"{task_prefix}: {angles[i % len(angles)]}",
            }
            for i in range(count)
        ]

    def _auto_max_steps(self, state: Dict[str, Any]) -> int:
        if state.get("agenda_mode") == "exploration":
            return EXPLORATION_AUTO_MAX_STEPS
        return DECISION_AUTO_MAX_STEPS

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

    def _split_position_arg(self, arg: str) -> Tuple[str, str]:
        arg = (arg or "").strip()
        if not arg:
            return "", ""
        parts = arg.split(maxsplit=1)
        action = parts[0].lower()
        value = parts[1] if len(parts) > 1 else ""
        return action, value

    def _split_evidence_arg(self, arg: str) -> Tuple[str, str]:
        arg = (arg or "").strip()
        if not arg:
            return "", ""
        parts = arg.split(maxsplit=1)
        action = parts[0].lower()
        value = parts[1] if len(parts) > 1 else ""
        return action, value

    def _resolve_import_path(self, raw_path: str) -> Path:
        path = Path(raw_path.strip().strip('"').strip("'"))
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()

    def _parse_evidence_items(self, raw: str, source_hint: str) -> List[Dict[str, Any]]:
        text = raw.strip()
        parsed: Any = None
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None

        raw_items: List[Any]
        if isinstance(parsed, dict):
            if isinstance(parsed.get("evidence_items"), list):
                raw_items = parsed["evidence_items"]
            elif isinstance(parsed.get("items"), list):
                raw_items = parsed["items"]
            else:
                raw_items = [parsed]
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = [
                {
                    "title": Path(source_hint).name if source_hint != "inline" else "",
                    "source_type": "note",
                    "key_quote_or_summary": text,
                }
            ]

        return [self._normalize_evidence_item(item, source_hint) for item in raw_items if item not in (None, "", [], {})]

    def _normalize_evidence_item(self, raw: Any, source_hint: str = "") -> Dict[str, Any]:
        if isinstance(raw, dict):
            item = dict(raw)
        else:
            item = {"key_quote_or_summary": str(raw)}

        summary = (
            item.get("key_quote_or_summary")
            or item.get("summary")
            or item.get("abstract")
            or item.get("text")
            or item.get("content")
            or ""
        )
        title = item.get("title") or item.get("name") or ""
        if not title and source_hint and source_hint != "inline":
            title = Path(source_hint).name

        normalized = {
            "source_id": str(item.get("source_id") or f"src-{uuid.uuid4().hex[:8]}"),
            "title": self._clip_inline_text(str(title or "untitled evidence"), limit=160),
            "url": self._clip_inline_text(str(item.get("url") or item.get("link") or ""), limit=300),
            "date": self._clip_inline_text(str(item.get("date") or item.get("year") or ""), limit=40),
            "source_type": self._normalize_evidence_source_type(item.get("source_type") or item.get("type")),
            "claim_supported": self._clip_inline_text(str(item.get("claim_supported") or ""), limit=360),
            "key_quote_or_summary": self._clip_inline_text(str(summary or ""), limit=2000),
            "reliability": self._normalize_reliability(item.get("reliability")),
        }
        return normalized

    def _normalize_evidence_request(self, raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            item = dict(raw)
        else:
            item = {"query": str(raw)}
        query = item.get("query") or item.get("keywords") or item.get("search_query") or item.get("evidence_needed") or ""
        return {
            "request_id": str(item.get("request_id") or f"req-{uuid.uuid4().hex[:8]}"),
            "query": self._clip_inline_text(str(query or ""), limit=240),
            "reason": self._clip_inline_text(str(item.get("reason") or item.get("why") or ""), limit=360),
            "source_type": self._normalize_evidence_source_type(item.get("source_type") or item.get("type")),
            "priority": self._normalize_priority(item.get("priority")),
            "status": self._clip_inline_text(str(item.get("status") or "open"), limit=40),
        }

    def _add_evidence_items(
        self,
        state: Dict[str, Any],
        items: List[Dict[str, Any]],
        raw_command: str,
        source: str,
    ) -> str:
        if not items:
            return "没有解析到可导入的 evidence item。"
        state = increment_round(state)
        existing = state.get("evidence_items") or []
        for item in items:
            existing = self._append_unique_state_item(existing, item)
        state["evidence_items"] = existing
        state["requires_user_intervention"] = False
        self.storage.save_turn(
            state["session_id"],
            state["round"],
            "user",
            raw_command,
            {
                "command": "evidence",
                "action": "add",
                "source": source,
                "items_added": len(items),
            },
        )
        self.storage.save_snapshot(state["session_id"], state)
        return f"已导入 {len(items)} 条 evidence item。\n\n" + self._format_evidence_pack(state)

    def _append_unique_state_item(self, existing: Any, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        result = list(existing or [])
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        seen = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in result}
        if key not in seen:
            result.append(item)
        return result[-80:]

    def _normalize_evidence_source_type(self, value: Any) -> str:
        raw = str(value or "unknown").strip().lower()
        allowed = {"paper", "docs", "benchmark", "blog", "repo", "web", "note", "unknown"}
        return raw if raw in allowed else "unknown"

    def _normalize_reliability(self, value: Any) -> str:
        raw = str(value or "unknown").strip().lower()
        return raw if raw in {"low", "medium", "high", "unknown"} else "unknown"

    def _normalize_priority(self, value: Any) -> str:
        raw = str(value or "medium").strip().lower()
        return raw if raw in {"low", "medium", "high"} else "medium"

    def _format_evidence_pack(self, state: Dict[str, Any]) -> str:
        items = state.get("evidence_items") or []
        lines = [
            "## Evidence pack",
            f"- evidence_items: {len(items)}",
            f"- evidence_requests: {len(state.get('evidence_requests') or [])}",
            "",
        ]
        if not items:
            lines.append("暂无 evidence items。")
        else:
            for index, item in enumerate(items[-20:], start=max(1, len(items) - 19)):
                lines.extend(
                    [
                        f"### {index}. {item.get('source_id', '')}",
                        f"- title: {item.get('title', '')}",
                        f"- type: {item.get('source_type', 'unknown')}",
                        f"- url: {item.get('url') or '暂无'}",
                        f"- reliability: {item.get('reliability', 'unknown')}",
                        f"- summary: {item.get('key_quote_or_summary') or '暂无'}",
                        "",
                    ]
                )
        lines.append("可用命令：/evidence add <text-or-json-object>、/evidence import <path>、/evidence requests")
        return "\n".join(lines).rstrip()

    def _format_evidence_requests(self, state: Dict[str, Any]) -> str:
        requests = state.get("evidence_requests") or []
        lines = ["## Evidence requests", f"- count: {len(requests)}", ""]
        if not requests:
            lines.append("暂无待检索请求。")
        else:
            for index, item in enumerate(requests[-20:], start=max(1, len(requests) - 19)):
                if isinstance(item, dict):
                    query = (
                        item.get("query")
                        or item.get("keywords")
                        or item.get("search_query")
                        or item.get("evidence_needed")
                        or ""
                    )
                    lines.extend(
                        [
                            f"### {index}. {item.get('request_id', '')}",
                            f"- query: {query}",
                            f"- reason: {item.get('reason') or '暂无'}",
                            f"- source_type: {item.get('source_type', 'unknown')}",
                            f"- priority: {item.get('priority', 'medium')}",
                            f"- status: {item.get('status', 'open')}",
                            "",
                        ]
                    )
                else:
                    lines.extend([f"### {index}.", f"- query: {item}", ""])
        lines.append("可用命令：/evidence request <keywords>、/evidence add <text-or-json-object>、/evidence import <path>")
        return "\n".join(lines).rstrip()

    def _format_user_position(self, state: Dict[str, Any]) -> str:
        return (
            "## User position\n"
            f"{state.get('user_position') or INITIAL_USER_POSITION}\n\n"
            "可用命令：/position set <text>、/position add <text>、/position clear"
        )
