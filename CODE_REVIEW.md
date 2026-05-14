# CODE_REVIEW.md

审查版本：`killme-lite-v2.zip`  
修改后版本：v3-review  
审查重点：CLI 控制流、状态一致性、SQLite 生命周期、clone 上限、LLM 输出校验、命令语义、可测试性和发布包卫生。

## 1. 审查结论

项目已经具备一个可运行的轻量原型骨架：`Router -> LLMClient -> role prompt -> validation -> Storage/state` 的分层基本清楚，mock LLM 也让本地烟测变得容易。

本次审查发现的主要问题不是“算法不能跑”，而是工程边界和命令语义上的几个隐患：手动角色命令会意外触发 clone、SQLite 连接没有真正关闭、外部/脏 state 可能绕过 clone 上限、源码包包含运行时数据库、规格中列出的 `/summary` 和 `/checkpoint` 未实现。以上问题已经在修改版中修复。

## 2. 已修复问题

### P1. SQLite 连接未关闭

原代码大量使用：

```python
with self.connect() as conn:
    ...
```

但 `sqlite3.Connection` 作为 context manager 只负责 commit/rollback，不负责 close。长会话或测试中会产生连接泄漏和 `ResourceWarning`。

修复：

- `Storage.connect()` 改为 `@contextmanager`；
- 显式 `commit / rollback / close`；
- 保留 `PRAGMA foreign_keys = ON`。

涉及文件：`storage.py`

### P1. `/exec` 等手动角色命令会按 clone 上限批量执行

原逻辑中：

```python
limit = clone_limits[role]
clone_tasks = default_clone_tasks(role, limit) if limit > 1 else None
```

这会导致 `/exec` 在 `executioner=3` 时实际启动 3 个 Executioner clone。它和 MVP 规格中“手动调用某个角色”的语义不一致，也会造成用户误触发成本和噪音。

修复：

- `/exec`、`/defend`、`/build`、`/judge` 固定调用单实例；
- 新增 `/spawn <role> <count>`，用于用户显式手动克隆；
- `/spawn` 严格遵守当前 `clone_limits`。

涉及文件：`router.py`, `README.md`, `tests/test_core_flow.py`

### P1. clone 上限只在命令层生效，脏 state 可能绕过

`/config` 会限制最大 clone 数，但如果 SQLite 快照或 `state_schema.json` 被污染为很大的数字，`ensure_shape()` 原本只做 `max(1, value)`，没有全局上限。

修复：

- 在 `state.py` 增加 `MAX_CLONE_LIMIT = 5`；
- `ensure_shape()` 对所有加载后的 clone limit 做 `1..5` 夹取；
- `router.py` 复用同一个常量，避免命令层和 state 层上限漂移。

涉及文件：`state.py`, `router.py`, `tests/test_core_flow.py`

### P2. 发布包包含运行时 SQLite 数据库

上传包内包含 `data/killme.sqlite`。虽然当前库为空，但发布源码包时携带运行时数据库容易泄露真实会话，并且会让用户解压后继承不该存在的本地状态。

修复：

- 修改版压缩包删除 `data/killme.sqlite`；
- 保留 `data/.gitkeep`；
- 新增 `.gitignore` 忽略 SQLite、缓存和临时文件。

涉及文件：`.gitignore`, release zip packaging

### P2. 规格中列出的 `/summary` 和 `/checkpoint` 未实现

原需求里包含 `/summary` 和 `/checkpoint`，但 CLI 不识别。

修复：

- 新增 `/summary`：输出简洁 session 摘要；
- 新增 `/checkpoint`：显式保存一轮 checkpoint，并返回摘要。

涉及文件：`router.py`, `README.md`, `tests/test_core_flow.py`

### P2. `INSERT OR REPLACE` 存在外键副作用风险

`Storage.create_session()` 使用 `INSERT OR REPLACE`。SQLite 的 `REPLACE` 语义是删除旧行再插入新行，未来如果开启更严格外键或保留子表记录，可能造成非预期行为。

修复：

- 改为 `INSERT ... ON CONFLICT(session_id) DO UPDATE`；
- 保留原 `created_at`，只更新 `title / updated_at / closed_at`。

涉及文件：`storage.py`

### P2. 缺少查询索引

`recent_turns()` 和 `load_latest_state()` 都按 `session_id` 查询并按自增 id 排序，原表没有索引。

修复：

- 增加 `idx_turns_session_id_id`；
- 增加 `idx_state_snapshots_session_id_id`；
- 增加 `idx_sessions_updated_at`。

涉及文件：`storage.py`

### P3. mock 环境变量优先级有小 bug

原逻辑：

```python
os.environ.get("KILLME_MOCK") or os.environ.get("KILLME_MOCK_LLM", "")
```

当 `KILLME_MOCK=0` 且 `KILLME_MOCK_LLM=1` 时，第二个变量不会生效。

修复：两个 mock flag 独立解析，任一为真即启用 mock。

涉及文件：`llm.py`

### P3. LLM 请求 timeout 写死

原 `urlopen(..., timeout=120)` 写死。

修复：新增 `KILLME_LLM_TIMEOUT`，最小 5 秒，默认 120 秒。

涉及文件：`llm.py`

### P3. Merger 的 `duplicates_removed` 永远为空

原 Merger 会去重，但返回字段 `duplicates_removed` 固定为空，调试 clone 噪音时信息不真实。

修复：

- 用稳定 key 去重；
- 返回真实的 `duplicates_removed`。

涉及文件：`merger.py`

### P3. 缺少自动化回归测试

原包只有手动 smoke test 文档，没有可执行测试。

修复：新增标准库 `unittest` 测试，不引入第三方依赖。

覆盖：

- 手动 `/exec` 单实例语义；
- `/spawn` clone + merger 路径；
- 加载脏 clone limit 时自动夹取；
- `/summary` 与 `/checkpoint`；
- `/spawn` 超过配置上限时拒绝。

涉及文件：`tests/test_core_flow.py`

## 3. 验证结果

已执行：

```bash
python3 -m py_compile main.py llm.py router.py state.py storage.py merger.py validation.py
python3 -m unittest discover -s tests -v
KILLME_MOCK=1 python3 main.py --db /tmp/killme-review.sqlite --script /tmp/killme-script.txt
```

结果：通过。

## 4. 仍然保留的风险

1. **真实 LLM 质量未验证**：mock 只能证明控制流，不能证明真实 Chair 是否会过早 close、过晚 close 或重复调度。
2. **没有 schema migration 机制**：当前 `CREATE TABLE IF NOT EXISTS` 适合 MVP，但后续字段变更需要迁移策略。
3. **没有 ruff/mypy/pytest 工程链路**：本次避免引入依赖，只使用标准库测试。后续可补 `pyproject.toml`。
4. **Merger 仍偏规则化**：它能去重和提取强点，但还不是一个真正能判断观点强弱、冲突和覆盖率的 agent。
5. **多进程并发不是目标**：SQLite 本地 CLI 足够；如果未来 Web UI 或多会话并发，需要重新审查事务和锁。

## 5. 建议下一步

先用真实 API 跑 3 个真实 idea，每个 15-20 轮，记录：

- Chair 调度是否符合用户直觉；
- clone 是否产生非重复信息；
- `/summary` 和 closing statement 能否恢复上下文；
- Judge verdict 是否稳定且可解释；
- 用户是否觉得它明显优于普通 ChatGPT 多角色 prompt。
