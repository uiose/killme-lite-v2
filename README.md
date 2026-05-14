# killme-lite

轻量本地多智能体审议 CLI 运行时。

`killme-lite` 是一个基于 CLI 的本地原型，用于结构化审查想法、方案和决策。它受到 kill-me 式批判工作流启发：系统不会默认支持一个想法，而是先攻击假设，再保留幸存观点，提出更小的实现或测试，并记录阶段性裁决。

当前工程审查版：`v0.4-engineering-hygiene`

核心设计：

```text
唯一不可克隆的 Chair
+ 可克隆的非 Chair 角色
+ shared state
+ major question 生命周期
+ decision / exploration 双议程模式
+ 本地 SQLite 持久化
+ prompt files
+ OpenAI-compatible LLM API
```

---

## 为什么做这个项目

普通多角色 prompt 很容易退化成一次性报告：

```text
Executioner 说一段。
Defender 说一段。
Judge 说一段。
结束。
```

`killme-lite` 要验证的是一个小型本地运行时能否支撑**多轮审议**，而不是只生成一份看起来完整的报告。

它帮助回答这些问题：

- 这个想法真的值得做吗？
- 哪些假设会杀死它？
- 最强的诚实辩护是什么？
- 最小可测试版本是什么？
- 应该 `KILL`、`REDESIGN`、`TEST` 还是 `BUILD`？
- 如果问题还太开放，如何先扫描资料、生成多假设和保存研究空间？
- 经过 10-20 轮后，系统还能不能保持上下文清楚？

---

## 当前状态

项目目前是一个**本地 MVP**，不是生产级 agent platform。

当前重点：

- 本地 CLI 使用；
- SQLite 状态持久化；
- deterministic validation；
- 受控角色克隆；
- 开发用 mock mode；
- 本地真实 LLM 测试模式；
- 简单、可检查的 runtime 行为。

暂不包含：

- Web UI；
- Docker 部署；
- LangGraph；
- Postgres；
- 向量数据库；
- 浏览器自动化；
- 插件市场；
- always-on 后台 agent。

---

## 核心概念

### Chair

Chair 是中枢主持人。它唯一存在，不能被克隆。

Chair 负责：

- 选择下一个发言角色；
- 判断是否生成 clone；
- 跟踪当前 major question；
- 推荐 manual 或 auto mode；
- 生成 closing statement；
- 推动讨论前进。

Chair **并不拥有所有 state 字段**。runtime validation 会限制每个角色能更新哪些字段。

### 角色

| 角色 | 职责 |
|---|---|
| `Chair` | 主持审议流程 |
| `Executioner` | 攻击假设、计划、风险和模糊定义 |
| `Defender` | 给出最强诚实辩护 |
| `Builder` | 把想法压缩成更小的实现或测试 |
| `Judge` | 给出裁决：`KILL`、`REDESIGN`、`TEST` 或 `BUILD` |
| `Merger` | 合并 clone 输出，降低噪音 |

### 可克隆角色

Chair 不能被克隆。其他角色可以在配置上限内克隆。

示例：

```text
/spawn executioner 3
```

这可能产生多个 Executioner 视角：

```text
Executioner-1: 攻击过度工程
Executioner-2: 攻击证据不足
Executioner-3: 攻击用户流程摩擦
```

clone 输出不会直接写入 shared state，而是先经过 Merger 合并。

### Major Question 生命周期

系统围绕 major question 推进。

典型流程：

```text
Chair 提出 major question
-> 各角色参与审议
-> 必要时生成 clones
-> Merger 合并 clone 输出
-> Judge 给出裁决
-> Chair 关闭当前问题
-> 更新 state
-> 进入下一个 major question
```

### Exploration Mode

开放探究性问题不一定已经适合裁决。`/explore <question>` 会创建 `agenda_mode = exploration` 的 session：

```text
/explore killme-lite 的议程机制如何支持开放探索型研究问题
```

Exploration mode 不自动调用 Judge，也不自动输出 `KILL / REDESIGN / TEST / BUILD`。它不是 decision mode 的预处理阶段，而是一等议程：可以长期保持开放、分支、回溯、深化，并维护一张探索地图。

| 字段 | 用途 |
|---|---|
| `exploration_status` | `open / paused / synthesized`，允许探索不转化 |
| `exploration_clone_mode` | `independent / visible`，控制 clone 是否能看到 sibling 输出 |
| `exploration_focus` | 当前开放探究主题或分支 |
| `hypotheses` | 多个竞争性假设或解释 |
| `research_threads` | 资料、概念、反例、基线等线索 |
| `findings` | 阶段性发现，不等于 verdict |
| `coverage_gaps` | 尚未扫描的覆盖缺口 |
| `decision_candidates` | 可选的未来裁决节点，不是默认终点 |
| `exploration_nodes` / `exploration_edges` | 假设、证据、缺口、异常之间的关系图 |
| `anomalies` | 低共识但可能高价值的异常、边缘、矛盾线索 |

常用探索命令包括 `/map`、`/focus <branch>`、`/clone-mode visible`。当某条线索确实需要裁决时，用户可以手动 `/mode decision`；系统不会默认把开放探索压缩成 verdict。详见 `EXPLORATION_MODE.md`。

### Evidence Pack

`evidence_items` 保存用户或外部主持程序导入的证据摘要、论文、benchmark、repo 或文档条目。LLM 角色可以读取 evidence pack，但不能自己写入 `evidence_items`。

当角色发现当前判断需要外部资料时，只能写入 `evidence_requests`，例如检索关键词、需要的来源类型和理由。用户或主持程序据此下载、摘录并用 `/evidence add` 或 `/evidence import` 导入材料。

这个边界故意保留：系统可以意识到“需要检索什么”，但不会把未导入资料伪装成已知事实。

---

## 当前实现重点

本版 README 保留 v2 的项目思路，同时对齐当前工作区实现。

- `config.py`、`.env.example`、`config.example.toml`、`pyproject.toml` 已纳入基础工程配置；
- 使用 `uv` 管理虚拟环境、依赖、测试和 lint；
- `agents/chair.md` 的 Chair `state_patch` allowlist 与 `validation.py` 对齐；
- deterministic Merger 在 decision mode 按 severity / confidence / evidence / support_count 排序；exploration mode 改用多样性、异常点、少数派线索优先；
- Merger 支持 exact dedupe，并输出 `points`、`duplicates_removed`、`strongest_point`、`conflicts`、`state_patch`；
- SQLite 保留 `sessions`、`turns`、`state_snapshots`、`clone_runs`；
- clone group 开始、完成、失败均可追踪；
- clone group 中途失败会写入 `system` error turn，且不会把部分 clone patch 写入 shared state；
- `/close` 在 decision mode 且 `judge_verdict = undecided` 时默认拒绝关闭；exploration mode 可直接输出 synthesis；
- `/close --force` 允许 decision mode 未裁决关闭，并标记 `closed_without_judgement: true`；
- pytest 覆盖配置、validation、merger、exploration mode、visible clone mode、clone failure、close guard 和核心流程。

---

## 项目结构

```text
killme-lite/
  main.py
  config.py
  llm.py
  router.py
  state.py
  storage.py
  merger.py
  validation.py
  state_schema.json

  agents/
    chair.md
    executioner.md
    defender.md
    builder.md
    judge.md
    merger.md

  tests/
    test_config.py
    test_validation.py
    test_exploration_mode.py
    test_merger.py
    test_core_flow.py
    test_clone_failure.py
    test_close_guard.py

  data/
    .gitkeep

  .env.example
  config.example.toml
  pyproject.toml
  uv.lock
  README.md
```

补充文档：

- `EXPLORATION_MODE.md`：exploration mode 的设计动机、状态字段和推荐流程。
- `NATIVE_EXPLORATION_UPGRADE.md`：回应探索模式结构性批评的二次升级记录。

- `MVP_SPEC.md`：MVP 行为规格；
- `USAGE.md`：使用方式与详细配置示例；
- `CODE_REVIEW.md`：审查记录；
- `FIX_REPORT.md`：修复说明；
- `WORK_ORDER_REVIEW.md`：工单级审查；
- `TEST_RUN.md`：测试运行记录。

---

## 环境要求

- Python 3.11+
- `uv`
- 真实 LLM 模式需要 OpenAI-compatible API endpoint

没有 API key 时，runtime 会自动使用 deterministic mock LLM，便于本地验证 CLI 控制流。

---

## 初始化与运行

推荐使用 `uv`：

```bash
cd killme-lite-v2
uv venv
uv sync --dev
```

创建本地环境变量文件。

PowerShell：

```powershell
Copy-Item .env.example .env
```

macOS / Linux：

```bash
cp .env.example .env
```

初始化数据库并启动 CLI：

```bash
uv run python main.py --init-db
uv run python main.py
```

也可以直接指定数据库路径：

```bash
uv run python main.py --db ./data/dev.sqlite
```

运行命令脚本：

```bash
uv run python main.py --script ./path/to/script.txt
```

---

## 配置

本地 secret 写入 `.env`，不要提交真实 `.env`。非 secret 的模型配置可以写入 `config.toml`。

优先级：

```text
process environment > config.toml > .env > default
```

`config.toml` 示例：

```toml
model = "gpt-5.5"
model_reasoning_effort = "xhigh"

[model_providers.openai]
base_url = "https://api.openai.com/v1"
wire_api = "chat"
```

`.env` 示例：

```env
# Secrets
OPENAI_API_KEY=

# Runtime
KILLME_DATA_DIR=./data
KILLME_DB_PATH=./data/killme.sqlite
KILLME_LLM_TIMEOUT=120

# Development
KILLME_MOCK_LLM=0
KILLME_LOG_LEVEL=INFO
```

也可以用环境变量覆盖模型配置：

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-5.5
OPENAI_REASONING_EFFORT=xhigh
```

兼容旧环境变量：

```bash
KILLME_LLM_API_KEY
KILLME_API_KEY
KILLME_LLM_BASE_URL
KILLME_API_BASE
KILLME_LLM_MODEL
KILLME_MODEL
KILLME_REASONING_EFFORT
KILLME_MOCK
```

显式开启 mock mode：

```bash
KILLME_MOCK_LLM=1 uv run python main.py
```

如果中转站不支持 OpenAI 的 `response_format` 参数：

```bash
KILLME_JSON_RESPONSE_FORMAT=0 uv run python main.py
```

---

## 命令

```text
/start <idea>
/explore <open-ended question>
/mode [exploration|decision]
/map
/focus <branch-or-subquestion>
/clone-mode <independent|visible>
/auto 1
/auto 2
/auto 3
/manual
/exec
/defend
/build
/judge
/spawn <executioner|defender|builder|judge> <count>
/sessions [limit]
/use <session_id>
/export [session_id]
/export-all
/state
/summary
/position
/position set <text>
/position add <text>
/position clear
/evidence
/evidence add <text-or-json-object>
/evidence import <path>
/evidence requests
/evidence request <keywords>
/checkpoint
/config <executioner|defender|builder|judge> <max_clones>
/close
/close --force
/reset
/quit
```

说明：

- `/start <idea>` 创建新的审议 session；
- `/auto <n>` 让 Chair 自动推进 `n` 步；decision mode 单次最多 3 步，exploration mode 单次最多 12 步；
- `/explore <question>` 创建开放探索 session；
- `/mode [exploration|decision]` 查看或切换议程模式；
- `/map` 显示 hypotheses / threads / findings / gaps / anomalies / relation graph 摘要；
- `/focus <branch-or-subquestion>` 在 exploration mode 中切换研究分支；
- `/clone-mode visible` 允许 exploration clone 看到同批 sibling 输出并延伸；`independent` 则保持冻结快照；
- `/manual` 切回人工模式；
- `/exec`、`/defend`、`/build`、`/judge` 是单实例调用；
- `/spawn` 是显式 clone 命令，并严格遵守 `clone_limits`；
- `/sessions [limit]` 列出历史 session，并标记当前 active session；
- `/use <session_id>` 切换当前 active session；
- `/export [session_id]` 导出单个 session，默认导出 active session；
- `/export-all` 导出全部 sessions；
- `/state` 输出当前 shared state；
- `/summary` 输出当前 session 摘要；
- `/position` 显示当前 `user_position`；
- `/position set <text>` 手动覆盖 `user_position`；
- `/position add <text>` 手动合并新的长期约束；
- `/position clear` 清空回初始立场；
- `/evidence` 显示当前 evidence pack；
- `/evidence add <text-or-json-object>` 手动导入一条证据摘要或结构化 evidence item；
- `/evidence import <path>` 从本地 JSON / Markdown / 文本文件导入 evidence item；
- `/evidence requests` 显示角色提出的待检索关键词；
- `/evidence request <keywords>` 手动记录一条待检索请求；
- `/checkpoint` 保存当前 state snapshot；
- `/config` 修改非 Chair 角色的 clone 上限；
- `/close` 在 decision mode 需要先有 Judge verdict；在 exploration mode 输出 exploration synthesis；
- `/close --force` 允许 decision mode 未裁决关闭，但会写入 `closed_without_judgement: true`；
- `/reset` 会清空本地 `sessions`、`turns`、`state_snapshots`、`clone_runs`，是本地开发工具，不属于 MVP 审议协议核心命令；
- `/quit` 退出 REPL。

---

## 示例流程

```text
/start 我想做一个轻量本地多智能体审议系统
/auto 3
/judge
/close
/auto 3
/state
/summary
```

---

## 裁决类型

| 裁决 | 含义 |
|---|---|
| `KILL` | 停止投入这个想法 |
| `REDESIGN` | 修改范围、问题定义或机制 |
| `TEST` | 先做可证伪实验，再决定是否构建 |
| `BUILD` | 想法可以进入实现阶段 |
| `undecided` | 尚未裁决 |

---

## Clone Group 可追踪性

同一 clone group 的 clone turns metadata 会包含：

```json
{
  "clone_group_id": "executioner-2-abcd1234",
  "input_state_round": 1,
  "independent_snapshot": true,
  "state_patch_applied": false
}
```

Merger turn metadata 会包含：

```json
{
  "merged_after_independent_clones": true
}
```

`clone_runs` 会记录：

```text
pending -> completed
pending -> failed
```

失败时还会写入 `system` turn：

```text
clone_group_failed: <error>
```

---

## 真实 LLM Smoke Test

在分享给别人使用前，建议先进行本地真实 LLM 试运行。

使用真实 LLM 模式：

```env
KILLME_MOCK_LLM=0
OPENAI_API_KEY=your_api_key_here
```

然后跑一个完整流程：

```text
/start 构建一个轻量本地多智能体审议系统
/auto 3
/judge
/close
/auto 3
/judge
/close
/state
/summary
```

检查重点：

- Chair 是否跳过 closing statement；
- Judge 裁决是否前后一致；
- Merger 是否减少 clone 噪音；
- 多轮后 state 是否仍然清楚；
- 用户约束是否被保留；
- `/state` 是否还能说明当前讨论位置。

---

## 测试与质量检查

```bash
uv run python -m py_compile config.py main.py llm.py router.py state.py storage.py merger.py validation.py
uv run pytest
uv run ruff check .
```

本包应在 mock LLM 下通过上述检查。

---

## 开发原则

本项目刻意保持小而清楚。

优先使用：

```text
CLI
SQLite
prompt files
shared state JSON
deterministic validation
small tests
```

不要过早加入：

```text
Web UI
LangGraph
Postgres
向量数据库
多服务部署
插件系统
```

当前目标是先验证审议体验，而不是扩展成重型 agent platform。

---

## 数据与隐私

运行时数据存储在本地。

默认路径：

```text
data/killme.sqlite
```

不要提交本地运行数据。

以下内容应保持在 `.gitignore` 中：

```gitignore
.env
.venv/
data/*.sqlite
data/*.sqlite-*
```

---

## Roadmap

近期：

- 提升真实 LLM 长 session 稳定性；
- 增加 session export/import；
- 增加 schema migration；
- 改进 command audit trail；
- 增加 LLM retry/backoff；
- 改进 Merger 冲突检测。

后续：

- 最小部署指南；
- 可选 Dockerfile；
- 可选 Web UI；
- 更好的 observability；
- session 对比工具。

---

## License

TBD.

---

## 项目理念

`killme-lite` 不试图成为重型 agent platform。

它是一个本地小系统，用来持续追问：

```text
什么观点经受住了攻击？
什么应该被杀掉？
什么需要重构？
什么值得测试？
什么最终值得构建？
```
