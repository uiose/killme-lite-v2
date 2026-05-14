import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import AppConfig, ProviderConfig, load_config


class LLMError(RuntimeError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMClient:
    def __init__(self, base_dir: Path, config: Optional[AppConfig] = None):
        self.base_dir = Path(base_dir)
        self.config = config or load_config(self.base_dir)
        self.providers = self.config.model_providers
        self.api_key = self.config.openai_api_key
        self.base_url = self.config.openai_base_url.rstrip("/")
        self.model = self.config.openai_model
        self.reasoning_effort = self.config.openai_reasoning_effort
        self.llm_mode = self.config.llm_mode
        self.force_mock = self.config.mock_llm
        self.use_response_format = self.config.json_response_format
        self.timeout_seconds = self.config.llm_timeout
        self.progress: Optional[Callable[[str], None]] = None
        self.last_provider: Optional[str] = None
        self.last_model: Optional[str] = None
        self.last_fallback_used = False
        self._command_active_provider: Optional[str] = None

    def begin_command(self) -> None:
        self._command_active_provider = self._initial_provider_name()
        self.last_fallback_used = False

    def end_command(self) -> None:
        self._command_active_provider = None
        self.last_fallback_used = False

    def has_configured_provider(self) -> bool:
        return bool(self._provider(self._initial_provider_name()).api_key)

    def call_agent(
        self,
        role: str,
        state: Dict[str, Any],
        recent_turns: List[Dict[str, Any]],
        current_task: str,
        clone_name: Optional[str] = None,
        angle: Optional[str] = None,
        clone_context_mode: str = "independent",
    ) -> Dict[str, Any]:
        if clone_context_mode == "visible":
            runtime_rule = (
                "Exploration visible clone mode: later clones may see earlier sibling outputs in recent_turns, "
                "but no sibling state_patch has been applied to shared state until Merger runs. Extend, contrast, "
                "or branch from prior outputs; do not merely repeat them."
            )
        else:
            runtime_rule = (
                "If this is a clone call, all clones in the same group receive an identical frozen state snapshot. "
                "Do not rely on sibling clone output."
            )
        payload = {
            "state": state,
            "recent_turns": recent_turns,
            "current_task": current_task,
            "clone_name": clone_name,
            "angle": angle,
            "clone_context_mode": clone_context_mode,
            "important_runtime_rule": runtime_rule,
        }

        provider = self._agent_provider(role)
        if self.force_mock or not provider.api_key:
            return mock_agent(role, payload)

        prompt = self.load_prompt(role)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]

        raw = self.chat_completion(messages, provider=provider)
        parsed = extract_json(raw)
        if not isinstance(parsed, dict):
            raise LLMError(f"Agent {role} did not return a JSON object: {raw[:500]}")
        return parsed

    def load_prompt(self, role: str) -> str:
        path = self.base_dir / "agents" / f"{role}.md"
        if not path.exists():
            raise LLMError(f"Prompt not found: {path}")
        return path.read_text(encoding="utf-8")

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        provider: Optional[ProviderConfig] = None,
    ) -> str:
        if provider is not None:
            return self._chat_completion_with_provider_config(provider, messages)

        provider_name = self._current_provider_name()
        try:
            return self._chat_completion_with_provider(provider_name, messages)
        except LLMTimeoutError:
            if not self._should_retry_with_deepseek(provider_name):
                raise
            self._emit_progress(
                "LLM timeout on provider openai; retrying with deepseek for this command"
            )
            self._command_active_provider = "deepseek"
            self.last_fallback_used = True
            return self._chat_completion_with_provider("deepseek", messages)

    def _chat_completion_with_provider(
        self,
        provider_name: str,
        messages: List[Dict[str, str]],
    ) -> str:
        provider = self._provider(provider_name)
        return self._chat_completion_with_provider_config(provider, messages)

    def _chat_completion_with_provider_config(
        self,
        provider: ProviderConfig,
        messages: List[Dict[str, str]],
    ) -> str:
        if provider.wire_api != "chat":
            raise LLMError(f"Unsupported wire_api for provider {provider.name}: {provider.wire_api}")
        if not provider.api_key:
            raise LLMError(f"Missing API key for provider {provider.name}")

        url = f"{provider.base_url}/chat/completions"
        body: Dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
        }
        thinking_enabled = provider.thinking == "enabled"
        if not (provider.name == "deepseek" and thinking_enabled and _is_deepseek_model(provider.model)):
            body["temperature"] = provider.temperature if provider.temperature is not None else 0.2
        if provider.reasoning_effort:
            body["reasoning_effort"] = provider.reasoning_effort
        if self.use_response_format:
            body["response_format"] = {"type": "json_object"}
        if provider.thinking in {"enabled", "disabled"} and _is_deepseek_model(provider.model):
            body["thinking"] = {"type": provider.thinking}

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "OpenAI/Python",
            },
        )

        self.last_provider = provider.name
        self.last_model = provider.model
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP error {exc.code}: {error_body}") from exc
        except TimeoutError as exc:
            raise LLMTimeoutError(f"LLM request timed out on provider {provider.name}") from exc
        except urllib.error.URLError as exc:
            if _is_timeout_reason(exc.reason):
                raise LLMTimeoutError(f"LLM request timed out on provider {provider.name}") from exc
            raise LLMError(f"LLM request failed: {exc}") from exc

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response: {data}") from exc

    def _initial_provider_name(self) -> str:
        if self.llm_mode == "deepseek":
            return "deepseek"
        return "openai"

    def _current_provider_name(self) -> str:
        if self.llm_mode == "deepseek":
            return "deepseek"
        if self.llm_mode == "openai":
            return "openai"
        return self._command_active_provider or "openai"

    def _provider(self, provider_name: str) -> ProviderConfig:
        try:
            return self.providers[provider_name]
        except KeyError as exc:
            raise LLMError(f"Unknown provider: {provider_name}") from exc

    def _agent_provider(self, role: str) -> ProviderConfig:
        configured = self.config.agent_models.get(str(role or "").strip().lower())
        if configured is not None:
            return configured
        return self._provider(self._current_provider_name())

    def _should_retry_with_deepseek(self, provider_name: str) -> bool:
        return self.llm_mode == "openai_then_deepseek_per_command" and provider_name == "openai"

    def _emit_progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def _is_timeout_reason(reason: Any) -> bool:
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    lowered = str(reason).lower()
    return "timed out" in lowered or "timeout" in lowered


def _is_deepseek_model(model: str) -> bool:
    return "deepseek" in str(model or "").strip().lower()


def extract_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        raise LLMError("Empty LLM response")

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Could not parse JSON object from LLM output: {exc}") from exc

    raise LLMError(f"Could not parse JSON from: {text[:500]}")


def mock_agent(role: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    state = payload.get("state", {})
    task = payload.get("current_task") or ""
    angle = payload.get("angle") or "default angle"
    claim = state.get("core_claim") or "当前想法"
    question = state.get("current_major_question") or "当前问题"

    if role == "chair":
        return mock_chair(state, task)

    if state.get("agenda_mode") == "exploration":
        return mock_exploration_agent(role, payload)

    if role == "executioner":
        if "definition" in angle:
            strongest = f"{angle}: 核心概念、成功标准和失败阈值还不够可操作，容易让系统看似在审议，实际无法判断是否变好。"
            evidence_requests = []
        elif "evidence" in angle:
            strongest = f"{angle}: 目前缺少真实用户愿意持续使用该审议循环的证据，可能只是把一次性 prompt 包装成系统。"
            evidence_requests = [
                {
                    "query": f"{claim} public benchmark baseline evidence",
                    "reason": "需要外部材料判断当前主张是否已有基线、公开评测或反例。",
                    "source_type": "web",
                    "priority": "medium",
                }
            ]
        elif "complexity" in angle:
            strongest = f"{angle}: clone、merger、judge 和状态写入会让轻量 MVP 迅速变成复杂 orchestrator。"
            evidence_requests = []
        elif "workflow" in angle:
            strongest = f"{angle}: 用户可能被太多角色输出打断，反而更难表达真实约束。"
            evidence_requests = []
        else:
            strongest = f"{angle}: 该方案可能在真实使用场景中缺少可证伪需求证据。"
            evidence_requests = []
        return {
            "role": "executioner",
            "task_understood": task,
            "new_attacks": [
                {
                    "claim": strongest,
                    "why_it_matters": "如果这个失败面成立，后续多智能体结构只会放大复杂度或噪音。",
                    "evidence_needed": "至少 3 个真实问题在 15-20 轮后仍能保持清晰状态，并让用户愿意继续用。",
                    "severity": "high",
                }
            ],
            "strongest_attack": strongest,
            "killed_arguments": ["先做完整平台再验证价值"],
            "open_questions": [f"围绕「{question}」最小可证伪测试是什么？"],
            "evidence_requests": evidence_requests,
            "state_patch": {
                "strongest_attack": strongest,
                "killed_arguments": ["先做完整平台再验证价值"],
                "open_questions": [f"围绕「{question}」最小可证伪测试是什么？"],
                "evidence_requests": evidence_requests,
            },
        }

    if role == "defender":
        if "user value" in angle:
            defense = f"{angle}: 如果用户确实需要一个不迎合自己的审议流程，{claim} 的价值来自持续反证和阶段收束。"
        elif "MVP" in angle or "feasibility" in angle:
            defense = f"{angle}: 只做 CLI + SQLite + prompt files + 本地校验，{claim} 可以用很低成本验证。"
        elif "learning" in angle:
            defense = f"{angle}: 即使早期产品价值未定，真实审议记录也能快速暴露 prompt、state 和 Chair 规则的缺陷。"
        else:
            defense = f"{angle}: 只要先限制范围，{claim} 仍值得用真实问题测试。"
        return {
            "role": "defender",
            "task_understood": task,
            "honest_defenses": [
                {
                    "claim": defense,
                    "condition": "只验证审议循环是否产生更好判断，不扩展平台能力。",
                    "weakness_admitted": "clone 可能重复，Chair 也可能调度错误。",
                    "evidence_needed": "15-20 轮后 state 仍清晰，且用户愿意继续使用。",
                }
            ],
            "strongest_defense": defense,
            "surviving_arguments": ["唯一 Chair + 可克隆非 Chair 角色值得测试"],
            "open_questions": ["怎样判断 clone 产生的是新信息而不是噪音？"],
            "state_patch": {
                "strongest_defense": defense,
                "surviving_arguments": ["唯一 Chair + 可克隆非 Chair 角色值得测试"],
                "open_questions": ["怎样判断 clone 产生的是新信息而不是噪音？"],
            },
        }

    if role == "builder":
        if "falsifiable" in angle:
            redesign = f"{angle}: 设计 3 个真实问题、每个最多 20 轮，并记录 clone 新信息率、Chair 停顿点和 state 清晰度。"
        elif "workflow" in angle:
            redesign = f"{angle}: 每次 /auto 最多 3 步，closing 后必须停，用户用 /auto 再进入下一大问题。"
        else:
            redesign = f"{angle}: 只实现命令路由、shared state、SQLite 三表、prompt 文件、本地校验和冻结 clone 快照。"
        return {
            "role": "builder",
            "task_understood": task,
            "redesign_options": [
                {
                    "proposal": redesign,
                    "what_it_removes": "Web UI、LangGraph、Postgres、向量库、长期记忆。",
                    "test_step": "用 3 个真实问题各跑 15-20 轮，检查 Chair、clone、state 是否有用。",
                    "cost": "low",
                }
            ],
            "best_redesign": redesign,
            "surviving_arguments": ["先证明审议循环，再加功能"],
            "open_questions": ["测试时如何记录 Chair 是否选对下一步？"],
            "state_patch": {
                "best_redesign": redesign,
                "surviving_arguments": ["先证明审议循环，再加功能"],
                "open_questions": ["测试时如何记录 Chair 是否选对下一步？"],
            },
        }

    if role == "judge":
        verdict = "TEST"
        summary = "方向值得验证，但证据不足以直接 BUILD；下一步应跑真实问题测试审议循环。"
        return {
            "role": "judge",
            "verdict": verdict,
            "confidence": "medium",
            "reasoning_summary": summary,
            "what_would_change_the_verdict": "如果 3 个真实问题 15-20 轮后 state 混乱或 clone 只重复，应改 prompt/schema/Chair 规则。",
            "next_validation_action": "运行 3 个真实问题，每个问题 15-20 轮，并记录 Chair 决策质量。",
            "state_patch": {
                "judge_verdict": verdict,
                "open_questions": ["真实测试中的成功/失败判据是否足够明确？"],
                "surviving_arguments": ["先 TEST 而不是直接 BUILD"],
                "killed_arguments": ["未验证前扩展成完整 agent platform"],
            },
        }

    raise LLMError(f"Unknown role: {role}")


def mock_exploration_agent(role: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    state = payload.get("state", {})
    task = payload.get("current_task") or ""
    angle = payload.get("angle") or "exploratory angle"
    focus = state.get("exploration_focus") or state.get("core_claim") or "这个开放问题"
    question = state.get("current_major_question") or focus
    focus_node = {
        "id": "focus:current",
        "type": "focus",
        "label": focus,
        "summary": "当前开放探索主题",
        "status": state.get("exploration_status") or "open",
    }

    if role == "executioner":
        gap = f"{angle}: 当前探索容易把「{focus}」过早化约成单一可裁决问题，缺少反例、相邻领域和术语边界扫描。"
        thread = f"沿着「{question}」寻找反例、边界案例、失败定义和不可比基线。"
        anomaly = "异常标记：如果某个反例同时支持两种相反解释，应先深挖而不是并入多数假设。"
        gap_node = {"id": "gap:premature-closure", "type": "gap", "label": gap, "source_role": "executioner"}
        thread_node = {"id": "thread:counterexamples", "type": "thread", "label": thread, "source_role": "executioner"}
        edges = [
            {"source": "focus:current", "target": "gap:premature-closure", "relation": "has_gap", "summary": "探索主题存在过早收敛风险。"},
            {"source": "gap:premature-closure", "target": "thread:counterexamples", "relation": "can_be_probed_by", "summary": "用反例与边界案例扫描填补缺口。"},
        ]
        evidence_requests = [
            {
                "query": f"{focus} survey review competing hypotheses",
                "reason": "探索模式需要先扫相邻概念、综述和竞争解释，而不是直接裁决。",
                "source_type": "web",
                "priority": "medium",
            }
        ]
        return {
            "role": "executioner",
            "task_understood": task,
            "new_attacks": [],
            "strongest_attack": "",
            "killed_arguments": [],
            "open_questions": ["哪些问题目前还不能被压成 yes/no 或 verdict？"],
            "coverage_gaps": [gap],
            "research_threads": [thread],
            "anomalies": [anomaly],
            "exploration_nodes": [focus_node, gap_node, thread_node],
            "exploration_edges": edges,
            "evidence_requests": evidence_requests,
            "state_patch": {
                "coverage_gaps": [gap],
                "research_threads": [thread],
                "anomalies": [anomaly],
                "exploration_nodes": [focus_node, gap_node, thread_node],
                "exploration_edges": edges,
                "open_questions": ["哪些问题目前还不能被压成 yes/no 或 verdict？"],
                "evidence_requests": evidence_requests,
            },
        }

    if role == "defender":
        hypothesis = f"{angle}: {focus} 可能不是单一方案问题，而是一组待分解的机制、情境和评价标准。"
        thread = f"把「{focus}」拆成定义、现象、机制、可观察信号和已有研究传统五条线。"
        finding = "探索阶段的核心收益是保留多种解释路径，避免把资料不足误判为方案失败。"
        hyp_node = {"id": "hypothesis:multi-mechanism", "type": "hypothesis", "label": hypothesis, "source_role": "defender"}
        thread_node = {"id": "thread:decompose-five-lines", "type": "thread", "label": thread, "source_role": "defender"}
        finding_node = {"id": "finding:preserve-plurality", "type": "finding", "label": finding, "source_role": "defender"}
        edges = [
            {"source": "focus:current", "target": "hypothesis:multi-mechanism", "relation": "may_be_explained_by", "summary": "开放主题可由多机制解释。"},
            {"source": "hypothesis:multi-mechanism", "target": "thread:decompose-five-lines", "relation": "needs_probe", "summary": "该假设需要拆分定义、现象、机制、信号、传统。"},
            {"source": "finding:preserve-plurality", "target": "hypothesis:multi-mechanism", "relation": "supports", "summary": "保留多解释路径支持继续探索。"},
        ]
        return {
            "role": "defender",
            "task_understood": task,
            "honest_defenses": [],
            "strongest_defense": "",
            "surviving_arguments": [],
            "open_questions": ["有哪些互相竞争但目前都还合理的解释？"],
            "hypotheses": [hypothesis],
            "research_threads": [thread],
            "findings": [finding],
            "exploration_nodes": [focus_node, hyp_node, thread_node, finding_node],
            "exploration_edges": edges,
            "state_patch": {
                "hypotheses": [hypothesis],
                "research_threads": [thread],
                "findings": [finding],
                "exploration_nodes": [focus_node, hyp_node, thread_node, finding_node],
                "exploration_edges": edges,
                "open_questions": ["有哪些互相竞争但目前都还合理的解释？"],
            },
        }

    if role == "builder":
        thread = "用 30-60 分钟做第一轮资料发现：关键词、综述/README/benchmark、反例、相邻术语各至少 2 条。"
        gap = "还没有足够资料判断哪些分支值得转成 decision node；探索可以继续积累，不必强行转化。"
        finding = "下一步不是构建 MVP，而是降低未知空间：先找资料、聚类假设、再选择是否需要可裁决节点。"
        candidate = f"候选 decision node（可选而非终点）：在特定语境下，{focus} 的哪一种机制最值得验证？"
        thread_node = {"id": "thread:first-source-scan", "type": "thread", "label": thread, "source_role": "builder"}
        gap_node = {"id": "gap:not-enough-evidence", "type": "gap", "label": gap, "source_role": "builder"}
        candidate_node = {"id": "candidate:specific-mechanism-test", "type": "decision_candidate", "label": candidate, "source_role": "builder"}
        edges = [
            {"source": "focus:current", "target": "thread:first-source-scan", "relation": "needs_scan", "summary": "先做资料扫描而不是构建。"},
            {"source": "thread:first-source-scan", "target": "gap:not-enough-evidence", "relation": "may_reduce", "summary": "资料扫描可降低证据缺口。"},
            {"source": "thread:first-source-scan", "target": "candidate:specific-mechanism-test", "relation": "may_generate", "summary": "扫描后可能生成可选裁决节点。"},
        ]
        evidence_requests = [
            {
                "query": f"{focus} benchmark dataset baseline implementation",
                "reason": "需要基线、数据集、已有实现或评价方式来判断哪些分支值得继续。",
                "source_type": "web",
                "priority": "medium",
            }
        ]
        return {
            "role": "builder",
            "task_understood": task,
            "redesign_options": [],
            "best_redesign": "",
            "surviving_arguments": [],
            "open_questions": ["哪条研究线索最值得先补证据？"],
            "research_threads": [thread],
            "coverage_gaps": [gap],
            "findings": [finding],
            "decision_candidates": [candidate],
            "exploration_nodes": [focus_node, thread_node, gap_node, candidate_node],
            "exploration_edges": edges,
            "evidence_requests": evidence_requests,
            "state_patch": {
                "research_threads": [thread],
                "coverage_gaps": [gap],
                "findings": [finding],
                "decision_candidates": [candidate],
                "exploration_nodes": [focus_node, thread_node, gap_node, candidate_node],
                "exploration_edges": edges,
                "open_questions": ["哪条研究线索最值得先补证据？"],
                "evidence_requests": evidence_requests,
            },
        }

    if role == "judge":
        return {
            "role": "judge",
            "verdict": "undecided",
            "confidence": "low",
            "reasoning_summary": "当前是 exploration mode，Judge 只能评估是否适合转入 decision mode，不能给 KILL/REDESIGN/TEST/BUILD。",
            "what_would_change_the_verdict": "选择一条具体假设、导入必要证据，并由用户显式切换 /mode decision。",
            "next_validation_action": "继续资料扫描、扩展探索图，或把某一条分支作为候选 decision node。",
            "evidence_requests": [],
            "state_patch": {
                "open_questions": ["要继续开放探索，还是把哪条探索线索切换成 decision node？"]
            },
        }

    raise LLMError(f"Unknown role: {role}")


def mock_chair(state: Dict[str, Any], task: str) -> Dict[str, Any]:
    task_lower = task.lower()
    if "initial major question" in task_lower or "initial exploration" in task_lower or "bootstrap" in task_lower:
        question = _mock_initial_exploration_frame(state) if state.get("agenda_mode") == "exploration" else _mock_initial_major_question(state)
        state_patch = {"current_major_question": question}
        if state.get("agenda_mode") == "exploration":
            focus = state.get("exploration_focus") or state.get("core_claim", "")
            state_patch.update(
                {
                    "agenda_mode": "exploration",
                    "exploration_focus": focus,
                    "exploration_status": "open",
                    "research_threads": ["定义与边界", "竞争性假设", "资料与基线扫描"],
                    "coverage_gaps": ["尚未导入资料，不能裁决。"],
                    "exploration_nodes": [
                        {"id": "focus:current", "type": "focus", "label": focus, "summary": "当前开放探索主题"},
                        {"id": "thread:definitions", "type": "thread", "label": "定义与边界"},
                        {"id": "thread:hypotheses", "type": "thread", "label": "竞争性假设"},
                        {"id": "thread:sources", "type": "thread", "label": "资料与基线扫描"},
                    ],
                    "exploration_edges": [
                        {"source": "focus:current", "target": "thread:definitions", "relation": "needs_definition"},
                        {"source": "focus:current", "target": "thread:hypotheses", "relation": "needs_hypotheses"},
                        {"source": "focus:current", "target": "thread:sources", "relation": "needs_sources"},
                    ],
                }
            )
        return {
            "role": "chair",
            "decision_type": "wait_user",
            "role_to_call": None,
            "clone_count": 0,
            "clone_tasks": [],
            "reason": "为新 session 生成 topic-specific 的第一轮探索框架。" if state.get("agenda_mode") == "exploration" else "为新 session 生成 topic-specific 的第一轮 major question。",
            "requires_user_intervention": False,
            "message_to_user": "",
            "state_patch": state_patch,
            "closing_statement": None,
        }

    if "exploration synthesis" in task_lower:
        return _mock_chair_exploration_close(state)

    if "closing" in task.lower() or "close" in task.lower():
        return _mock_chair_close(state)

    if state.get("requires_user_intervention") and state.get("chair_mode") != "auto":
        return {
            "role": "chair",
            "decision_type": "wait_user",
            "role_to_call": None,
            "clone_count": 0,
            "clone_tasks": [],
            "reason": "当前 state 要求用户介入。",
            "requires_user_intervention": True,
            "message_to_user": "当前需要用户补充约束或选择下一步。",
            "state_patch": {"requires_user_intervention": True},
            "closing_statement": None,
        }

    limits = state.get("clone_limits", {})
    if state.get("agenda_mode") == "exploration":
        if not state.get("hypotheses"):
            count = min(2, int(limits.get("defender", 1)))
            return _chair_call(
                "defender",
                count,
                "探索模式先保留多个竞争性假设，而不是裁决。",
                "让 Defender 生成互相竞争的解释和相邻概念。",
                agenda_mode="exploration",
            )
        if not state.get("coverage_gaps") or not state.get("anomalies"):
            count = min(3, int(limits.get("executioner", 1)))
            return _chair_call(
                "executioner",
                count,
                "已有初步假设，下一步扫描盲区、异常点和过早收敛风险。",
                "让 Executioner 找资料缺口、反例、边界和异常信号。",
                agenda_mode="exploration",
            )
        if (
            not state.get("research_threads")
            or len(state.get("research_threads") or []) < 4
            or not state.get("exploration_edges")
            or not state.get("decision_candidates")
        ):
            count = min(2, int(limits.get("builder", 1)))
            return _chair_call(
                "builder",
                count,
                "需要把未知空间变成下一轮资料发现动作。",
                "让 Builder 设计最小资料扫描计划。",
                agenda_mode="exploration",
            )
        return {
            "role": "chair",
            "decision_type": "wait_user",
            "role_to_call": None,
            "clone_count": 0,
            "clone_tasks": [],
            "reason": "探索地图已有初步假设、线索、缺口和关系；下一步需要用户补 evidence、选择焦点，或继续开放探索。",
            "requires_user_intervention": True,
            "message_to_user": "请选择一条研究线索继续补资料，运行 /focus 深挖分支，或继续 /auto 扩展探索图；/mode decision 只是可选路径。",
            "state_patch": {"requires_user_intervention": True, "exploration_status": "open"},
            "closing_statement": None,
        }

    if not state.get("strongest_attack"):
        count = min(3, int(limits.get("executioner", 1)))
        return _chair_call("executioner", count, "当前 major question 尚缺少清晰攻击面。", "先让 Executioner 从独立失败面攻击。")

    if not state.get("strongest_defense"):
        count = min(2, int(limits.get("defender", 1)))
        return _chair_call("defender", count, "已有攻击，下一步需要最强诚实防守。", "让 Defender 判断哪些部分仍值得测试。")

    if not state.get("best_redesign"):
        count = min(2, int(limits.get("builder", 1)))
        return _chair_call("builder", count, "攻击和防守已经形成张力，需要最轻改法。", "让 Builder 降低复杂度。")

    if state.get("judge_verdict") == "undecided":
        return {
            "role": "chair",
            "decision_type": "judge",
            "role_to_call": "judge",
            "clone_count": 1,
            "clone_tasks": [],
            "reason": "攻击、防守和改法已出现，需要阶段性裁决。",
            "requires_user_intervention": False,
            "message_to_user": "让 Judge 给出阶段 verdict。",
            "state_patch": {},
            "closing_statement": None,
        }

    return _mock_chair_close(state)


def _mock_initial_exploration_frame(state: Dict[str, Any]) -> str:
    focus = str(state.get("exploration_focus") or state.get("core_claim") or "这个开放问题").strip()
    if len(focus) > 80:
        focus = focus[:77].rstrip() + "..."
    return f"围绕「{focus}」，哪些定义、竞争性解释、相邻领域、证据关系和异常点值得先展开，而不急于形成裁决？"


def _mock_initial_major_question(state: Dict[str, Any]) -> str:
    claim = str(state.get("core_claim") or "这个想法").strip()
    if len(claim) > 80:
        claim = claim[:77].rstrip() + "..."
    return f"围绕「{claim}」，最需要先澄清的核心定义、适用边界和可证伪标准是什么？"


def _chair_call(
    role: str,
    count: int,
    reason: str,
    message: str,
    agenda_mode: str = "decision",
) -> Dict[str, Any]:
    return {
        "role": "chair",
        "decision_type": "spawn_clones" if count > 1 else "call_role",
        "role_to_call": role,
        "clone_count": count,
        "clone_tasks": _clone_tasks(role, count, agenda_mode),
        "reason": reason,
        "requires_user_intervention": False,
        "message_to_user": message,
        "state_patch": {},
        "closing_statement": None,
    }


def _clone_tasks(role: str, count: int, agenda_mode: str = "decision") -> List[Dict[str, str]]:
    if agenda_mode == "exploration":
        angles = {
            "executioner": [
                "scan premature closure and false dichotomies",
                "map missing evidence and source blind spots",
                "surface underexplored counter-hypotheses",
            ],
            "defender": [
                "generate plausible competing hypotheses",
                "connect adjacent concepts and literatures",
                "charitably expand possible mechanisms",
            ],
            "builder": [
                "design source discovery plan",
                "design research space scan",
                "turn unknowns into next probes",
            ],
            "judge": [
                "assess readiness to convert into decision node",
                "identify what remains too open to judge",
            ],
        }.get(role, ["default exploratory angle"])
        task_prefix = "Expand the exploration map from this angle"
    else:
        angles = {
            "executioner": [
                "attack definition ambiguity",
                "attack evidence gap",
                "attack execution complexity",
                "attack user workflow friction",
                "attack falsifiability",
            ],
            "defender": [
                "defend from user value",
                "defend from minimal MVP feasibility",
                "defend from learning value",
            ],
            "builder": [
                "design the lightest MVP",
                "design the smallest falsifiable test",
                "design the simplest operational workflow",
            ],
            "judge": [
                "judge product value",
                "judge implementation complexity",
                "judge validation value",
            ],
        }.get(role, ["default angle"])
        task_prefix = "Evaluate current major question from this independent angle only"

    return [
        {
            "name": f"{role.capitalize()}-{index + 1}",
            "angle": angles[index % len(angles)],
            "task": f"{task_prefix}: {angles[index % len(angles)]}",
        }
        for index in range(count)
    ]


def _mock_chair_exploration_close(state: Dict[str, Any]) -> Dict[str, Any]:
    question = state.get("current_major_question") or "当前 exploration frame"
    closing = f"""## Chair exploration synthesis

Exploration frame:
{question}

Exploration focus:
{state.get('exploration_focus') or state.get('core_claim', '')}

Hypotheses preserved:
{'; '.join(state.get('hypotheses') or ['尚未形成明确竞争性假设。'])}

Research threads:
{'; '.join(state.get('research_threads') or ['定义、假设、资料路径仍需展开。'])}

Findings so far:
{'; '.join(state.get('findings') or ['尚未沉淀明确发现。'])}

Coverage gaps:
{'; '.join(state.get('coverage_gaps') or ['尚未记录覆盖缺口。'])}

Evidence requests:
{json.dumps(state.get('evidence_requests') or [], ensure_ascii=False, indent=2)}

Decision candidates, optional:
{'; '.join(state.get('decision_candidates') or state.get('open_questions') or ['暂无；探索可以继续，不必强行转成裁决。'])}

Anomalies / weak signals:
{'; '.join(state.get('anomalies') or ['暂无明确异常点。'])}

Graph snapshot:
- nodes: {len(state.get('exploration_nodes') or [])}
- edges: {len(state.get('exploration_edges') or [])}

Mode recommendation:
MANUAL

Reason:
探索模式的关闭动作只是阶段性地图综合，不输出 verdict；session 仍可继续开放积累。"""
    return {
        "role": "chair",
        "decision_type": "close_question",
        "role_to_call": None,
        "clone_count": 0,
        "clone_tasks": [],
        "reason": "探索模式阶段性综合，不裁决。",
        "requires_user_intervention": True,
        "message_to_user": "已综合当前探索地图；下一步可以补 evidence、/focus 深挖分支，或继续开放探索。",
        "state_patch": {"requires_user_intervention": True, "exploration_status": "synthesized"},
        "closing_statement": closing,
    }


def _mock_chair_close(state: Dict[str, Any]) -> Dict[str, Any]:
    question = state.get("current_major_question") or "当前 major question"
    next_question = "下一步真实测试如何设计，才能判断这个审议循环是否值得继续？"
    closing = f"""## Chair closing statement

Major question:
{question}

What survived:
{'; '.join(state.get('surviving_arguments') or ['轻量审议循环仍值得测试。'])}

What was killed:
{'; '.join(state.get('killed_arguments') or ['未验证前扩展成完整平台。'])}

Main unresolved tension:
系统必须足够轻，但又要证明 Chair 调度和 clone 合并真的产生新信息。

Current state update:
- Core claim: {state.get('core_claim', '')}
- User position: {state.get('user_position', '')}
- Strongest attack: {state.get('strongest_attack', '')}
- Strongest defense: {state.get('strongest_defense', '')}
- Best redesign: {state.get('best_redesign', '')}
- Judge verdict: {state.get('judge_verdict', 'undecided')}

Next major question:
{next_question}

Mode recommendation:
MANUAL

Reason:
当前阶段需要用户决定测试问题和真实约束，避免 auto mode 自嗨。"""
    return {
        "role": "chair",
        "decision_type": "close_question",
        "role_to_call": None,
        "clone_count": 0,
        "clone_tasks": [],
        "reason": "当前 major question 已具备攻击、防守、改法和 verdict，可以收束。",
        "requires_user_intervention": True,
        "message_to_user": "当前问题已收束，建议用户确认下一问题。",
        "state_patch": {
            "pending_next_question": next_question,
            "requires_user_intervention": True,
        },
        "closing_statement": closing,
    }
