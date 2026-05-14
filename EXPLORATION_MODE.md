# Exploration Mode

`killme-lite` 原始流程适合把一个想法推进到可裁决的 decision node：攻击、辩护、降复杂度、裁决、关闭。这个流程对产品判断、方案审查和 MVP 决策很有用，但面对真正开放的探究性问题时会有结构性偏差：Chair 会寻找单一 major question，Executioner 会寻找致命失败面，Builder 会寻找最小测试，Judge 会输出 verdict。结果是系统容易把“尚未理解的问题空间”过早压缩成“是否应该做”的节点。

Exploration mode 的目标是保留未知空间，而不是给 verdict。

## 适用场景

使用 `/explore <question>` 启动：

```text
/explore killme-lite 的议程机制怎样支持开放探索型研究问题？
```

适合：

- 研究空间还不清楚；
- 资料、术语、相邻领域和基线尚未扫描；
- 需要生成多个竞争性假设；
- 还不知道应该裁决什么；
- 用户想先发现问题，而不是马上决定 BUILD / KILL。

不适合：

- 已经有明确方案，需要做是否继续投入的判断；
- 需要严格输出 KILL / REDESIGN / TEST / BUILD；
- 用户已经选定一个很具体的可证伪节点。

这些场景继续用 `/start <idea>` 的 decision mode。

## 核心要素

Exploration mode 遵循七个要素：

1. **延迟裁决**：不自动调用 Judge，不自动输出 verdict，不把开放问题改写成“是否可行”。
2. **多假设生成**：用 `hypotheses` 保存互相竞争或并存的解释、机制、场景和研究传统。
3. **资料发现优先**：角色只能提出 `evidence_requests`，不能把未导入资料当作事实。
4. **研究空间扫描**：用 `research_threads` 保存术语、相邻领域、反例、基线和资料线索。
5. **覆盖缺口显式化**：用 `coverage_gaps` 记录未扫描的面向，防止把资料不足误当失败证据。
6. **阶段性发现而非结论**：用 `findings` 保存暂时发现，但它们不等于 verdict。
7. **可转化节点**：当某条线索足够具体，用户可以 `/mode decision`，再围绕它裁决。

## 状态字段

新增字段：

```json
{
  "agenda_mode": "decision | exploration",
  "exploration_focus": "",
  "hypotheses": [],
  "research_threads": [],
  "findings": [],
  "coverage_gaps": []
}
```

`chair_mode` 仍只表示 manual / auto；`agenda_mode` 才表示 decision / exploration。

## 命令

```text
/explore <open-ended question>
/mode
/mode exploration
/mode decision
/summary
/auto 1
/spawn defender 2
/spawn executioner 2
/spawn builder 2
/evidence request <keywords>
/evidence add <text-or-json-object>
/close
```

`/close` 在 exploration mode 中输出的是 exploration synthesis，不是 Judge verdict。

## 推荐流程

```text
/explore 一个开放研究问题
/auto 3
/evidence requests
/evidence add <你找到的资料摘要>
/auto 2
/summary
/mode decision
```

切换到 decision mode 后，用户应把某条研究线索改写成明确节点，再运行 `/exec`、`/defend`、`/build`、`/judge` 或 `/auto 3`。
