# Exploration Mode

`killme-lite` 最早的强项是 decision mode：把一个想法压到可裁决的 decision node，然后让 Executioner / Defender / Builder / Judge 围绕它攻击、辩护、重设计和裁决。这对产品判断、方案审查、MVP 去留很有效。

真正开放的探究性问题不同。它们往往没有单一方案、没有明确 verdict、没有天然的结束点，也不应该被过早改写成“这个东西该不该做”。Exploration mode 因此被设计成一等议程，而不是 decision mode 的预处理阶段：它可以长期保持开放、分支、回溯、积累关系图，并把异常线索保留下来。

## 什么时候用 exploration mode

使用：

```text
/explore <open-ended question>
```

适合的问题：

- 某个领域的知识结构、范式、争议或研究传统是什么；
- 一个现象有哪些竞争性解释，各自可能需要什么证据；
- A 和 B 之间是否存在深层联系；
- 还不知道该裁决什么，只想先扫描研究空间；
- 需要保留多条分支，而不是马上收敛到一个方案。

不适合的问题：

- 已经有明确方案，需要判断是否 BUILD / TEST / REDESIGN / KILL；
- 已经有明确、可证伪、可执行的 decision node；
- 用户要的是阶段性裁决而不是探索地图。

这些场景继续用：

```text
/start <idea>
/mode decision
```

## 原则

Exploration mode 遵循以下要素：

1. **探索可以不转化**：系统不会默认寻找退出 exploration 的理由。`decision_candidates` 只是可选候选，不是终点。
2. **延迟裁决**：不自动调用 Judge，不输出 KILL / REDESIGN / TEST / BUILD。
3. **多假设并存**：`hypotheses` 保存竞争性解释、机制、场景和研究传统。
4. **关系图优先**：用 `exploration_nodes` / `exploration_edges` 表达“finding 支持 hypothesis”“thread 填补 gap”“anomaly 挑战 hypothesis”等关系。
5. **异常保留**：`anomalies` 保存低共识但可能高价值的反常、边缘、矛盾、意外线索。
6. **资料边界清晰**：角色只能提出 `evidence_requests`，不能把未导入资料当作已知事实。
7. **可分支、可回溯、可深化**：用 `/focus` 切换分支；后续 evidence 可以重新改变旧假设的关系。
8. **探索合并重多样性**：Merger 在 exploration mode 中优先保留多样性、异常点、少数派线索，而不是只保留最高共识项。

## 状态字段

核心字段：

```json
{
  "agenda_mode": "decision | exploration",
  "exploration_status": "open | paused | synthesized",
  "exploration_clone_mode": "independent | visible",
  "exploration_focus": "",
  "hypotheses": [],
  "research_threads": [],
  "findings": [],
  "coverage_gaps": [],
  "decision_candidates": [],
  "exploration_nodes": [],
  "exploration_edges": [],
  "anomalies": []
}
```

字段含义：

- `hypotheses`：可能解释、机制、框架或竞争性观点。
- `research_threads`：可继续挖掘的资料线、术语、相邻领域、基线、反例。
- `findings`：阶段性发现；不是最终结论。
- `coverage_gaps`：尚未覆盖的范围、资料缺口、方法盲区。
- `decision_candidates`：可以在未来切换到 decision mode 的候选节点；不是默认目标。
- `exploration_nodes`：关系图节点，通常包含 `id / type / title / summary`。
- `exploration_edges`：关系图边，通常包含 `source / target / relation / summary`。
- `anomalies`：低共识但可能高价值的异常线索。

## 命令

```text
/explore <question>              # 创建 exploration session
/mode                            # 查看当前 agenda mode
/mode exploration                # 切回开放探索
/mode decision                   # 切到可裁决模式
/map                             # 查看探索地图
/focus <branch-or-subquestion>   # 聚焦某个分支或子问题
/clone-mode independent          # clone 之间使用冻结快照，避免相互污染
/clone-mode visible              # exploration 中允许后续 clone 看见前面 sibling 输出并延伸
/auto <n>                        # 自动推进；decision 上限 3，exploration 上限 12
/spawn defender 2                # 多假设/机制生成
/spawn executioner 2             # 盲区/异常/反例扫描
/spawn builder 2                 # 资料路线/研究操作/候选节点设计
/evidence request <keywords>     # 请求资料
/evidence add <text-or-json>     # 导入证据包
/summary                         # 会显示 exploration map 摘要
/close                           # 输出 exploration synthesis，不要求 verdict
```

## Clone 模式

Exploration mode 支持两种 clone 上下文：

- `independent`：每个 clone 看到同一份 frozen base state，适合避免群体思维。
- `visible`：同批后续 clone 可以看到前面 sibling 的输出，适合头脑风暴式延伸。

即使使用 `visible`，clone 的 `state_patch` 也不会立即写入 state；只有 Merger 合并后统一写入，避免中途污染状态。

## 推荐流程

```text
/explore LLM 智能体的记忆机制有哪些设计范式？
/clone-mode visible
/auto 6
/map
/evidence request memory agent survey benchmark architecture
/evidence add <你找到的资料摘要或 JSON>
/focus 长期记忆和任务上下文压缩之间的关系
/auto 4
/map
```

如果某条线索变得足够具体，再手动切换：

```text
/mode decision
/focus 是否应该优先做“长期记忆评测基准”而不是“记忆组件”
/auto 3
```

## 仍然没有做的事

当前实现仍保持证据边界：系统提出 `evidence_requests`，用户或外部程序通过 `/evidence add` 导入资料。它没有内置主动联网检索器。这样做是为了保持来源可见、证据可复现、状态可审计；后续可以在这一层之上接入独立的 retrieval adapter。
