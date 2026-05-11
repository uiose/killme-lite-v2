# FIX_REPORT.md

v0.2 针对五个问题的修改摘要。

## 1. Clone 独立性

修改点：

- Router 在 clone group 开始前冻结 `base_state_snapshot` 和 `recent_snapshot`。
- 同组每个 clone 都读取同一份 snapshot。
- clone turn 保存 metadata：`clone_group_id`、`independent_snapshot`、`input_state_round`、`state_patch_applied=false`。
- clone 的 `state_patch` 不直接应用。
- Merger 合并后才写入 shared state。

涉及文件：

- `router.py`
- `merger.py`
- `agents/*.md`
- `MVP_SPEC.md`

## 2. `/auto` 上限太宽

修改点：

- 设置 `AUTO_MAX_STEPS = 3`。
- `/auto 50` 直接拒绝，不静默截断。
- 每次 auto 结束后回到 manual mode。

涉及文件：

- `router.py`
- `MVP_SPEC.md`
- `README.md`

## 3. 缺少 `major_question_history`

修改点：

- `state_schema.json` 增加 `major_question_history`。
- `state.py` 增加 `append_major_question_history()`。
- `/close` 和 Chair close 会把 closing statement 写入 history。

涉及文件：

- `state_schema.json`
- `state.py`
- `router.py`
- `MVP_SPEC.md`

## 4. 用户立场覆盖问题

修改点：

- 自然语言输入调用 `merge_user_position()`。
- 新输入合并进压缩立场，不覆盖旧立场。
- 原始用户输入继续保存在 turns。
- prompt 明确 `state.user_position` 是持续约束，不是最近原话。

涉及文件：

- `state.py`
- `router.py`
- `agents/*.md`

## 5. 轻量 JSON validation 缺失

修改点：

- 新增 `validation.py`。
- 支持字段别名：`next_action/action`、`state_update/patch`、`count/clones`。
- 规范化 `decision_type`、`role_to_call`、`judge_verdict`、list 字段。
- clone_count 按 state 上限 clamp。
- role-specific state patch allowlist 防止越权写入。
- 非法 Chair 角色 fail-closed 到 `wait_user`。

涉及文件：

- `validation.py`
- `router.py`
- `state.py`
- `agents/*.md`

---

# v0.3-review additional fixes

本节记录本次代码审查后的追加修改。

## 1. SQLite connection lifecycle

- `Storage.connect()` 改为 context manager；
- 显式 `commit / rollback / close`；
- 消除测试中的 unclosed database `ResourceWarning`。

## 2. Manual role command semantics

- `/exec`、`/defend`、`/build`、`/judge` 现在只调用单实例；
- 新增 `/spawn <role> <count>` 作为显式手动 clone 命令。

## 3. Clone limit hardening

- `state.ensure_shape()` 对加载后的 clone limits 做 `1..5` 夹取；
- 防止被污染的 SQLite snapshot 或 schema 绕过命令层上限。

## 4. CLI completeness

- 新增 `/summary`；
- 新增 `/checkpoint`。

## 5. Storage safety and performance

- `create_session()` 从 `INSERT OR REPLACE` 改为 `ON CONFLICT DO UPDATE`；
- 增加 turns、state_snapshots、sessions 的常用查询索引。

## 6. Packaging hygiene

- 发布包不再包含 `data/killme.sqlite`；
- 新增 `.gitignore` 忽略运行时数据库和 Python 缓存。

## 7. Regression tests

- 新增 `tests/test_core_flow.py`；
- 通过 `python3 -m unittest discover -s tests -v`。
