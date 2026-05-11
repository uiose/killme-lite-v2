# 工单审查与实现报告

版本：v0.4-engineering-hygiene  
对象：工程规范化与核心缺陷修复：`.env` + `uv` + Runtime 稳定性

## 1. 工单合理性审查

结论：工单合理，建议按 P1 执行。

理由：当前项目已经具备可运行 MVP，风险不再主要来自“功能缺失”，而是来自真实 LLM 接入后会被放大的工程问题：配置散落、prompt/runtime 契约不一致、clone group 半完成状态、Merger 噪音、以及未裁决状态下过早 close。该工单聚焦 MVP 可维护性，没有引入 Web UI、LangGraph、Postgres、Docker Compose 等非目标，边界是合理的。

## 2. 建议增删与调整

### 保留

- `.env` / `.env.example` / `config.py`：合理，能把本地配置集中起来。
- `uv` + `pyproject.toml`：合理，能统一开发命令与 dev 依赖。
- Chair prompt 与 validation 契约对齐：必须做，否则真实 LLM 调试会很困难。
- deterministic Merger：合理，当前阶段不应引入 LLM Merger。
- clone group 失败追踪：必须做，否则数据库会出现难解释的半完成轮次。
- `/close` verdict guard：合理，能维持审议流程严肃性。

### 增补

1. **保留旧环境变量兼容**  
   新规范使用 `OPENAI_*` 与 `KILLME_*`，但旧版已支持 `KILLME_LLM_API_KEY`、`KILLME_API_BASE`、`KILLME_MODEL` 等。本次保留兼容，避免升级后破坏已有本地配置。

2. **新增 `clone_runs` 表，而不是只写 error turn**  
   工单给了两个方案。实现时选择更规范的 `clone_runs` 表，并同时写入失败 `system` turn，便于查询与人工排错。

3. **Chair 自动 close 也加保护**  
   工单重点是 `/close`，但真实 LLM 可能让 Chair 在未裁决时输出 `close_question`。本次同时保护 Chair auto close：如果 `judge_verdict = undecided`，runtime 会拒绝 close 并要求先 `/judge` 或 force close。

4. **force close 后允许进入 pending next question**  
   `/close --force` 语义上已经关闭当前问题。本次补充逻辑：如果当前 major question 已写入 history，即使 verdict 仍是 `undecided`，后续 `/auto` 也可以推进到 `pending_next_question`。

5. **保留 `uv.lock`**  
   作为本地 CLI app，保留 lockfile 有助于复现实验环境。

### 不需要新增

- 不需要 Docker、CI/CD、mypy、数据库 migration 框架。当前仍是轻量本地 MVP，加入这些会超过本工单边界。
- 不需要 LLM Merger。当前 deterministic Merger 已足够验证 clone 降噪机制。

## 3. 已完成代码修改

### 环境与配置

- 新增 `.env.example`。
- 新增 `pyproject.toml`。
- 新增 `config.py`。
- 更新 `.gitignore`。
- 更新 `main.py`、`router.py`、`llm.py`，集中通过 `config.py` 读取配置。
- README 改为推荐：

```bash
uv venv
uv sync --dev
uv run python main.py
uv run pytest
uv run ruff check .
```

### Chair 契约修复

- 更新 `agents/chair.md`。
- Chair `state_patch` 只允许：

```text
current_major_question
pending_next_question
requires_user_intervention
```

- `validation.py` 中 Chair allowlist 与 prompt 对齐。
- Chair 输出越权字段时会被丢弃，并记录 validation warning，例如：

```text
state_patch_field_not_allowed:strongest_attack
```

### Merger 加强

- Merger 不再取第一个 point 作为 strongest。
- 新增 severity / confidence / evidence_needed / why_it_matters / support_count 打分。
- 支持 exact duplicate 去重与 `duplicates_removed` 输出。
- 输出结构包含：

```json
{
  "points": [],
  "merged_points": [],
  "duplicates_removed": [],
  "strongest_point": "",
  "conflicts": [],
  "state_patch": {}
}
```

- 支持基础冲突检测：
  - `verdict_conflict`
  - `complexity_conflict`
  - `scope_direction_conflict`
  - `judge_vs_close_conflict`

### clone group 失败恢复

- 新增 SQLite 表：`clone_runs`。
- clone group 开始时写入 `pending`。
- Merger 成功后写入 `completed`。
- 中途失败时写入 `failed`，并记录 error。
- 失败时额外保存 `system` turn：

```text
clone_group_failed: <error>
```

- 失败 clone 的部分 `state_patch` 不会写入 shared state。

### `/close` guard

- `/close` 在 `judge_verdict = undecided` 时默认拒绝。
- `/close --force` 允许强制关闭。
- force close 的 closing statement 与 history 都会标记：

```text
closed_without_judgement: true
```

- Chair auto close 在未裁决时也会被 runtime 拦截。

### 测试

新增或补强：

```text
tests/test_config.py
tests/test_validation.py
tests/test_merger.py
tests/test_clone_failure.py
tests/test_close_guard.py
tests/test_core_flow.py
```

覆盖：

- `.env` 加载；
- mock mode 无需真实 API key；
- Chair 越权字段丢弃；
- Merger 去重；
- Merger severity 排序；
- verdict / direction 冲突检测；
- clone group 失败记录；
- `/close` guard；
- `/close --force` 标记未裁决关闭；
- force close 后可进入 pending next question；
- 核心 CLI flow。

## 4. 验证结果

已执行：

```bash
uv run python -m py_compile config.py main.py llm.py router.py state.py storage.py merger.py validation.py
uv run pytest -q
uv run ruff check .
KILLME_MOCK_LLM=1 python3 main.py --db /tmp/killme-smoke.sqlite --script <smoke-script>
```

结果：

```text
16 passed
All checks passed
mock smoke flow passed
```

## 5. 剩余风险

1. Merger 的冲突检测仍是关键词 MVP，不等于完整语义推理。
2. 真实 LLM 输出质量仍需用 3 个真实 idea、每个 15-20 轮验证。
3. SQLite schema 仍使用 `CREATE TABLE IF NOT EXISTS`，后续如果字段频繁变化，需要 migration 策略。
4. Chair 的调度质量无法靠单元测试完全证明，仍需要真实审议日志回放。
