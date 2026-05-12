# killme-lite MVP 行为规格

版本：v0.3-review

本文件只冻结 MVP 行为规则，不描述长期平台化能力。

## 1. 支持哪些命令

MVP CLI 支持：

- `/start <idea>`：创建新 session，初始化 shared state。
- `/auto <N>`：让 Chair 自动推进 N 个调度步骤。MVP 单次硬上限为 3，超过上限直接拒绝，不静默执行。
- `/manual`：切换到 manual mode，并设置 `requires_user_intervention = true`。
- `/exec`：手动调用单个 Executioner。
- `/defend`：手动调用单个 Defender。
- `/build`：手动调用单个 Builder。
- `/judge`：手动调用单个 Judge。
- `/spawn <role> <count>`：显式手动生成某角色的临时 clones，并由 Merger 合并。必须遵守 `clone_limits`。
- `/state`：输出当前 shared state JSON。
- `/summary`：输出当前 session 的简洁摘要。
- `/checkpoint`：显式保存当前 state snapshot，并返回摘要。
- `/config <role> <max_clones>`：修改非 Chair 角色的 clone 上限。支持 `executioner / defender / builder / judge`。MVP 最多允许 5，但建议保持 1-3。
- `/close`：强制 Chair 对当前 major question 输出 closing statement，并写入 `major_question_history`。
- `/reset`：清空本地 sessions、turns、state_snapshots。

`/reset` 是开发工具，不属于审议协议核心能力。

## 2. Chair 怎么决定下一步

Chair 只负责调度、总结、抛问题和最小状态记录，不亲自替任何一方辩论。

Chair 每次只读取：

- 当前 `state`；
- 最近若干轮 turns；
- 当前任务或用户命令。

Chair 决策顺序：

1. 如果 `requires_user_intervention = true`，先停下来等用户，除非用户显式输入 `/auto <N>` 或某个角色命令。
2. 如果没有 `current_major_question`，Chair 生成一个新的 major question。
3. 如果当前问题还没有清晰攻击，优先调度 Executioner。
4. 如果已有强攻击但没有强辩护，调度 Defender。
5. 如果攻击和辩护都存在但还没有可执行重设计，调度 Builder。
6. 如果已经有攻击、辩护和重设计，但裁决仍是 `undecided`，调度 Judge。
7. 如果当前问题已经出现裁决、核心分歧已经清楚、或者继续讨论只会重复，输出 Chair closing statement。
8. 如果 Chair 判断需要真实业务约束、用户价值判断或用户立场选择，设置 `requires_user_intervention = true` 并等待用户。

Chair 输出必须是结构化调度结果。运行时会做轻量 JSON validation：非法角色、非法 verdict、超上限 clone_count、越权 state_patch、list/string 类型错误都会被规范化、裁剪或 fail-closed。

## 3. 什么时候克隆角色

Chair 可以克隆非 Chair 角色，但 clone 不是为了制造更多声音，只用于并行检查互相独立的角度。

允许克隆的情况：

- 当前问题存在多个独立失败面、辩护面、构建路径或裁决标准；
- 单个角色输出可能遗漏关键角度；
- 上一轮没有出现明显重复噪音；
- clone 上限允许；
- clone 之后必须由 Merger 合并。

禁止克隆的情况：

- Chair 永远不可克隆；
- 当前讨论已经足够收束；
- 上一批 clones 只是重复旧观点；
- 用户显式要求 manual 或降低上限；
- Judge 默认保持 1 个，除非用户显式提高 `judge` 上限。

## 4. Clone 独立性规则

同一组 clone 必须像并行评审员，而不是接力发言的人。

运行时规则：

1. Chair 先产出 clone group 决策。
2. Router 在调用第一位 clone 前冻结一次 `state` 和 recent turns。
3. 同组所有 clone 都读取这份完全相同的 frozen snapshot。
4. clone 输出会写入 turns，但 clone 的 `state_patch` 不会直接应用到 shared state。
5. Router 调用 Merger 合并所有 clone outputs。
6. 只有 Merger 的 `state_patch` 可以更新 shared state。

因此不会出现：

```text
Executioner-1 更新 state
Executioner-2 读取 Executioner-1 影响后的 state
Executioner-3 读取前两个 clone 污染后的 state
```

turn metadata 必须记录：

```json
{
  "clone_group_id": "...",
  "input_state_round": 3,
  "independent_snapshot": true,
  "state_patch_applied": false
}
```

Merger turn metadata 必须记录：

```json
{
  "merged_after_independent_clones": true
}
```

## 5. 克隆上限怎么配置

clone 上限保存在 `state.clone_limits`：

```json
{
  "executioner": 3,
  "defender": 2,
  "builder": 2,
  "judge": 1
}
```

用户通过命令修改：

```text
/config executioner 3
/config defender 2
/config builder 2
/config judge 1
```

规则：

- 只允许配置非 Chair 角色；
- 每个角色每轮最多生成 `clone_limits[role]` 个临时 clone；
- clone 是一次性实例，不跨轮保留人格；
- clone 输出必须合并后再写入 shared state；
- 如果上限是 1，则直接调用单个角色，不需要 Merger 降噪；
- 如果模型请求超过上限，本地 validator 会截断到上限；
- 加载 state 时也会把所有 clone limit 夹取到 `1..5`，防止脏快照绕过命令层限制。

## 6. `/auto` 上限

`/auto` 是有限推进按钮，不是“放飞系统”按钮。

MVP 规则：

- 默认建议 `/auto 1` 到 `/auto 3`。
- 单次硬上限是 `/auto 3`。
- 用户输入 `/auto 50` 时，系统拒绝执行，并解释上限原因。
- 每次 auto 结束后强制回到 manual mode。
- 如果 auto 中途出现 `requires_user_intervention = true`，立即停止。
- 如果 Chair 输出 closing statement，立即停止并等待用户。

上限存在的原因：

- 保留用户发言权；
- 避免早期错误判断被自动放大；
- 控制 token 成本；
- 减少 state 污染；
- 防止体验变成自动报告生成器。

## 7. 什么时候停下来等用户

系统必须在以下情况停下来：

- `/auto <N>` 已经执行完 N 个调度步骤；
- `/auto <N>` 超过 `AUTO_MAX_STEPS`；
- Chair 输出了 closing statement；
- `chair_mode = manual`；
- `requires_user_intervention = true`；
- Judge verdict 变为 `KILL / REDESIGN / BUILD`；
- 当前问题需要用户提供真实约束、价值排序或业务事实；
- 多个 clones 输出重复，继续自动推进没有新信息；
- 用户输入自然语言插话、反驳、修改约束或要求暂停。

停下时，Chair 只能说明当前状态和下一步选项，不能替用户选择。

## 8. 用户立场怎么更新

`user_position` 代表压缩后的当前用户立场，不是最近一次用户原话。

自然语言输入不会覆盖旧立场，也不会默认写入 `user_position`。用户必须通过显式命令维护长期约束：

```text
/position
/position set <text>
/position add <text>
/position clear
```

运行时会把 `/position add <text>` 合并为短约束列表，例如：

```text
当前用户立场（压缩，不等于最近一次原话）：
- 我想越轻越好。
- 我可以接受 CLI，不急着做 Web UI。
- 我不想用 LangGraph。
- Chair 可以自动推进，但要有限制。
```

规则：

- 用户原话仍保存在 `turns`；
- `user_position` 只保存持续影响判断的压缩立场；
- `user_position` 只能由 `/position` 系列命令手动显示和修改；
- 不允许任何 agent 替用户新增偏好；
- 不允许普通 agent 修改 `user_position`；
- 如果用户后来明确修正约束，应作为新立场追加，由后续 Chair/Judge 解释冲突。

## 9. Major Question History

每个大问题的 closing statement 不是普通聊天记录，而是阶段性提交记录。

state 中必须保留：

```json
"major_question_history": []
```

每次 `/close` 或 Chair 自动 close 时，运行时追加：

```json
{
  "round_closed": 12,
  "major_question": "...",
  "closing_statement": "## Chair closing statement ...",
  "what_survived": [],
  "what_was_killed": [],
  "main_unresolved_tension": "...",
  "strongest_defense": "...",
  "best_redesign": "...",
  "judge_verdict": "TEST",
  "next_major_question": "..."
}
```

这让系统知道已经讨论过哪些大问题，避免 Chair 重复抛相似问题，并支持跨会话恢复。

## 10. Chair closing statement 格式

`/close` 或 Chair 判断当前 major question 已经讨论够时，必须输出以下格式，并写入 turns、state snapshot 和 `major_question_history`：

```markdown
## Chair closing statement

Major question:
<本轮讨论的大问题>

What survived:
<目前仍然成立的观点>

What was killed:
<已经被否定、降级或搁置的想法>

Main unresolved tension:
<还没解决的核心矛盾>

Current state update:
- Core claim:
- User position:
- Strongest attack:
- Strongest defense:
- Best redesign:
- Judge verdict:

Next major question:
<Chair 抛出的下一个问题>

Mode recommendation:
AUTO / MANUAL

Reason:
<为什么建议自动推进或等待用户介入>
```

closing statement 必须帮助用户在不阅读完整历史的情况下恢复上下文。

## 11. 第一版仍然不做什么

仍然不引入：

- LangGraph；
- Postgres；
- Web UI；
- 向量库；
- 长期复杂记忆；
- 多服务部署；
- 插件系统。

第一版目标只有一个：证明“唯一 Chair + 可克隆非 Chair 角色 + shared state + 用户介入”的审议循环真的有用。
