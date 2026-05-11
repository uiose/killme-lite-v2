import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import AppConfig, load_config


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_dir: Path, config: Optional[AppConfig] = None):
        self.base_dir = Path(base_dir)
        self.config = config or load_config(self.base_dir)
        self.api_key = self.config.openai_api_key
        self.base_url = self.config.openai_base_url.rstrip("/")
        self.model = self.config.openai_model
        self.reasoning_effort = self.config.openai_reasoning_effort
        self.force_mock = self.config.mock_llm
        self.use_response_format = self.config.json_response_format
        self.timeout_seconds = self.config.llm_timeout

    def call_agent(
        self,
        role: str,
        state: Dict[str, Any],
        recent_turns: List[Dict[str, Any]],
        current_task: str,
        clone_name: Optional[str] = None,
        angle: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "state": state,
            "recent_turns": recent_turns,
            "current_task": current_task,
            "clone_name": clone_name,
            "angle": angle,
            "important_runtime_rule": "If this is a clone call, all clones in the same group receive an identical frozen state snapshot. Do not rely on sibling clone output.",
        }

        if self.force_mock or not self.api_key:
            return mock_agent(role, payload)

        prompt = self.load_prompt(role)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]

        raw = self.chat_completion(messages)
        parsed = extract_json(raw)
        if not isinstance(parsed, dict):
            raise LLMError(f"Agent {role} did not return a JSON object: {raw[:500]}")
        return parsed

    def load_prompt(self, role: str) -> str:
        path = self.base_dir / "agents" / f"{role}.md"
        if not path.exists():
            raise LLMError(f"Prompt not found: {path}")
        return path.read_text(encoding="utf-8")

    def chat_completion(self, messages: List[Dict[str, str]]) -> str:
        url = f"{self.base_url}/chat/completions"
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if self.use_response_format:
            body["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "OpenAI/Python",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP error {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response: {data}") from exc


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

    if role == "executioner":
        if "definition" in angle:
            strongest = f"{angle}: 核心概念、成功标准和失败阈值还不够可操作，容易让系统看似在审议，实际无法判断是否变好。"
        elif "evidence" in angle:
            strongest = f"{angle}: 目前缺少真实用户愿意持续使用该审议循环的证据，可能只是把一次性 prompt 包装成系统。"
        elif "complexity" in angle:
            strongest = f"{angle}: clone、merger、judge 和状态写入会让轻量 MVP 迅速变成复杂 orchestrator。"
        elif "workflow" in angle:
            strongest = f"{angle}: 用户可能被太多角色输出打断，反而更难表达真实约束。"
        else:
            strongest = f"{angle}: 该方案可能在真实使用场景中缺少可证伪需求证据。"
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
            "state_patch": {
                "strongest_attack": strongest,
                "killed_arguments": ["先做完整平台再验证价值"],
                "open_questions": [f"围绕「{question}」最小可证伪测试是什么？"],
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


def mock_chair(state: Dict[str, Any], task: str) -> Dict[str, Any]:
    task_lower = task.lower()
    if "initial major question" in task_lower or "bootstrap" in task_lower:
        question = _mock_initial_major_question(state)
        return {
            "role": "chair",
            "decision_type": "wait_user",
            "role_to_call": None,
            "clone_count": 0,
            "clone_tasks": [],
            "reason": "为新 session 生成 topic-specific 的第一轮 major question。",
            "requires_user_intervention": False,
            "message_to_user": "",
            "state_patch": {"current_major_question": question},
            "closing_statement": None,
        }

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


def _mock_initial_major_question(state: Dict[str, Any]) -> str:
    claim = str(state.get("core_claim") or "这个想法").strip()
    if len(claim) > 80:
        claim = claim[:77].rstrip() + "..."
    return f"围绕「{claim}」，最需要先澄清的核心定义、适用边界和可证伪标准是什么？"


def _chair_call(role: str, count: int, reason: str, message: str) -> Dict[str, Any]:
    return {
        "role": "chair",
        "decision_type": "spawn_clones" if count > 1 else "call_role",
        "role_to_call": role,
        "clone_count": count,
        "clone_tasks": _clone_tasks(role, count),
        "reason": reason,
        "requires_user_intervention": False,
        "message_to_user": message,
        "state_patch": {},
        "closing_statement": None,
    }


def _clone_tasks(role: str, count: int) -> List[Dict[str, str]]:
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

    return [
        {
            "name": f"{role.capitalize()}-{index + 1}",
            "angle": angles[index % len(angles)],
            "task": f"Evaluate current major question from this independent angle only: {angles[index % len(angles)]}",
        }
        for index in range(count)
    ]


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
