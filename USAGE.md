# killme-lite 使用与配置示例

本文档补充 `README.md`，重点说明本地初始化、配置优先级、mock 模式、真实 LLM 模式、常见中转站配置和测试命令。

不要把真实 `.env`、API key、SQLite 运行数据提交到版本库。

---

## 1. 快速启动

PowerShell：
第一次使用:
```powershell
cd D:\agentstart\killme-lite-v2
uv venv
uv sync --dev
Copy-Item .env.example .env
uv run python main.py --init-db
uv run python main.py  # 启动
```

如果 `.env` 已经存在，不要重复复制覆盖。它可能包含你的真实 API key 或中转站地址。

进入 REPL 后：

```text
/start 我想设计一套机制，解决多智能体下提示词对齐问题
/auto 3
/judge
/close
/summary
```

退出：

```text
/quit
```

`/start` 会先创建 session，然后由 Chair 根据你的 idea 生成一个 topic-specific 的第一轮 `current_major_question`。如果真实 LLM 不可用，runtime 会降级到本地兜底问题，保证 session 仍能创建。

---

## 2. 配置文件分工

项目同时支持 `.env` 和 `config.toml`。

建议分工：

| 文件 | 适合放什么 | 是否提交 |
|---|---|---|
| `.env` | API key、本机数据库路径、mock 开关、临时覆盖项 | 不提交 |
| `.env.example` | 给别人看的环境变量模板 | 可提交 |
| `config.toml` | 当前机器使用的模型名、base URL、reasoning effort | 通常不提交真实私有配置 |
| `config.example.toml` | 给别人看的 TOML 配置模板 | 可提交 |

配置优先级：

```text
process environment > config.toml > .env > default
```

也就是说，命令行里临时设置的环境变量优先级最高。

---

## 3. 最小 mock 配置

如果你只是想测试 CLI、状态流转、clone、Merger、close guard，不需要真实 API。

`.env`：

```env
# Secrets
OPENAI_API_KEY=

# Runtime
KILLME_DATA_DIR=./data
KILLME_DB_PATH=./data/killme.sqlite
KILLME_LLM_TIMEOUT=120

# Development
KILLME_MOCK_LLM=1
KILLME_LOG_LEVEL=INFO
```

启动：

```powershell
uv run python main.py
```

或者不改 `.env`，只在当前 PowerShell 临时启用 mock：

```powershell
$env:KILLME_MOCK_LLM="1"
uv run python main.py
```

mock 模式适合回归测试和开发，不适合判断真实提示词质量。

---

## 4. 真实 LLM 配置

### 4.1 官方 OpenAI-compatible 配置

`.env`：

```env
OPENAI_API_KEY=sk-your-real-key

KILLME_DATA_DIR=./data
KILLME_DB_PATH=./data/killme.sqlite
KILLME_LLM_TIMEOUT=120
KILLME_MOCK_LLM=0
KILLME_LOG_LEVEL=INFO
```

`config.toml`：

```toml
model = "gpt-5.5"
model_reasoning_effort = "xhigh"
json_response_format = true

[model_providers.openai]
base_url = "https://api.openai.com/v1"
wire_api = "chat"
```

启动真实模式：

```powershell
$env:KILLME_MOCK_LLM="0"
uv run python main.py
```

### 4.2 第三方中转站配置

如果你使用 OpenAI-compatible 中转站，只要它兼容 `/chat/completions` 即可。

`.env`：

```env
OPENAI_API_KEY=your-gateway-key

KILLME_DATA_DIR=./data
KILLME_DB_PATH=./data/killme.sqlite
KILLME_LLM_TIMEOUT=180
KILLME_MOCK_LLM=0
KILLME_LOG_LEVEL=INFO
```

`config.toml`：

```toml
model = "gpt-5.5"
model_reasoning_effort = "xhigh"
json_response_format = true

[model_providers.openai]
base_url = "https://your-gateway.example.com/v1"
wire_api = "chat"
```

如果中转站不支持 OpenAI 的 `response_format` 参数：

```toml
model = "gpt-5.5"
model_reasoning_effort = "xhigh"
json_response_format = false

[model_providers.openai]
base_url = "https://your-gateway.example.com/v1"
wire_api = "chat"
```

也可以用环境变量临时关闭：

```powershell
$env:KILLME_JSON_RESPONSE_FORMAT="0"
uv run python main.py
```

### 4.3 低成本真实测试配置

如果只想先验证链路，不想每轮跑太重：

```toml
model = "gpt-5.5"
model_reasoning_effort = "low"
json_response_format = true

[model_providers.openai]
base_url = "https://api.openai.com/v1"
wire_api = "chat"
```

然后在 CLI 里优先使用小步数：

```text
/start 我想测试真实 LLM 链路是否正常
/auto 1
/summary
```

---

## 5. 支持的环境变量

推荐变量：

| 变量 | 说明 | 示例 |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI-compatible API key | `sk-...` |
| `OPENAI_BASE_URL` | API base URL，可覆盖 `config.toml` | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | 模型名，可覆盖 `config.toml` | `gpt-5.5` |
| `OPENAI_REASONING_EFFORT` | reasoning effort | `low` / `medium` / `high` / `xhigh` |
| `KILLME_DATA_DIR` | 数据目录 | `./data` |
| `KILLME_DB_PATH` | SQLite 数据库路径 | `./data/killme.sqlite` |
| `KILLME_LLM_TIMEOUT` | LLM 请求超时秒数，最小 5 | `120` |
| `KILLME_MOCK_LLM` | 是否强制 mock | `0` / `1` |
| `KILLME_LOG_LEVEL` | 日志等级 | `INFO` |
| `KILLME_JSON_RESPONSE_FORMAT` | 是否发送 `response_format` | `1` / `0` |

兼容旧变量：

```text
KILLME_LLM_API_KEY
KILLME_API_KEY
KILLME_LLM_BASE_URL
KILLME_API_BASE
KILLME_LLM_MODEL
KILLME_MODEL
KILLME_REASONING_EFFORT
KILLME_MOCK
```

注意：如果 `.env` 里写了 `KILLME_MOCK_LLM=0`，而你想临时跑 mock 测试，需要在当前 PowerShell 显式覆盖：

```powershell
$env:KILLME_MOCK_LLM="1"
uv run pytest
```

---

## 6. 支持的 `config.toml` 字段

最小示例：

```toml
model = "gpt-5.5"

[model_providers.openai]
base_url = "https://api.openai.com/v1"
```

完整示例：

```toml
# 当前运行模型。
model = "gpt-5.5"

# 可选：传给支持 reasoning effort 的模型或中转站。
model_reasoning_effort = "xhigh"

# 可选：运行数据目录。
data_dir = "./data"

# 可选：SQLite 数据库路径。
db_path = "./data/killme.sqlite"

# 可选：LLM 请求超时秒数。
llm_timeout = 120

# 可选：是否强制 mock LLM。
mock_llm = false

# 可选：日志等级。
log_level = "INFO"

# 可选：是否请求 JSON object response_format。
json_response_format = true

# 可选：选择 provider，默认是 openai。
model_provider = "openai"

[model_providers.openai]
base_url = "https://api.openai.com/v1"
wire_api = "chat"
```

当前 runtime 使用 OpenAI-compatible `chat/completions` 接口；`wire_api = "chat"` 主要作为配置语义保留。

---

## 7. 数据库配置示例

默认数据库：

```env
KILLME_DB_PATH=./data/killme.sqlite
```

为开发测试使用独立数据库：

```powershell
uv run python main.py --db ./data/dev.sqlite
```

为真实 LLM smoke test 使用独立数据库：

```powershell
$env:KILLME_MOCK_LLM="0"
uv run python main.py --db ./data/real-llm-smoke.sqlite
```

初始化数据库：

```powershell
uv run python main.py --init-db
```

清空本地运行数据：

```text
/reset
```

`/reset` 是开发工具，会清空本地 `sessions`、`turns`、`state_snapshots`、`clone_runs`。

---

## 8. 会话管理与导出

列出最近 sessions：

```text
/sessions
```

限制显示数量：

```text
/sessions 50
```

输出中的 `*` 表示当前 active session。普通命令如 `/summary`、`/auto`、`/state` 都默认作用于 active session。

切换 active session：

```text
/use session-xxxxxxxxxxxx
```

`/use` 需要完整 `session_id`，不支持前缀匹配。

导出当前 active session：

```text
/export
```

导出指定 session：

```text
/export session-xxxxxxxxxxxx
```

导出全部 sessions：

```text
/export-all
```

导出文件写入：

```text
exports/<session_id>.json
exports/<session_id>.md
```

JSON 是权威备份格式，结构为：

```json
{
  "schema_version": 1,
  "exported_at": "...",
  "session": {},
  "latest_state": {},
  "turns": [],
  "state_snapshots": [],
  "clone_runs": []
}
```

Markdown 是阅读副本，包含 session 元信息、latest state 摘要、major question history、完整 turns 和 clone run 状态。

注意：导出内容可能包含用户输入、LLM 输出和本地审议记录。`exports/` 已被 `.gitignore` 忽略，不要把真实导出内容提交到公开仓库。

---

## 9. 常用运行方式

### 9.1 交互式 REPL

```powershell
uv run python main.py
```

### 9.2 指定数据库

```powershell
uv run python main.py --db ./data/dev.sqlite
```

### 9.3 脚本模式

创建 `smoke.txt`：

```text
/start 我想设计一套机制，解决多智能体下提示词对齐问题
/auto 1
/judge
/close
/summary
```

运行：

```powershell
uv run python main.py --script .\smoke.txt
```

### 9.4 真实 LLM 小步 smoke test

```powershell
$env:KILLME_MOCK_LLM="0"
uv run python main.py --db ./data/real-llm-smoke.sqlite
```

进入后：

```text
/start 我想设计一套机制，解决多智能体下提示词对齐问题
/auto 1
/judge
/close
/summary
```

---

## 10. 常用命令流程

### 10.1 命令说明

| 命令 | 作用 | 何时使用 |
|---|---|---|
| `/start <idea>` | 创建新的审议 session，并让 Chair 生成第一轮 topic-specific `current_major_question` | 开始审查一个新想法、方案或决策 |
| `/auto <n>` | 让 Chair 自动推进最多 `n` 个调度步骤，当前单次上限是 3 | 你愿意让系统继续推进，但仍想限制自动运行范围 |
| `/manual` | 切回 manual mode，并要求系统等待用户 | 你想暂停自动推进，自己决定下一步 |
| `/exec` | 手动调用一次 Executioner | 你想先看最强攻击、失败路径、反例和证据缺口 |
| `/defend` | 手动调用一次 Defender | 已有攻击后，想看这个想法还有哪些诚实成立的部分 |
| `/build` | 手动调用一次 Builder | 想把想法缩小成可测试版本、MVP 或下一步实验 |
| `/judge` | 手动调用一次 Judge | 攻击、防守或改法已经足够，需要阶段性裁决 |
| `/spawn <role> <count>` | 显式生成某个角色的多个 clones，并由 Merger 合并 | 你想并行检查多个独立角度，例如多个失败面 |
| `/config <role> <max_clones>` | 设置某个非 Chair 角色的 clone 上限 | 你想增加或降低 clone 数量，控制成本和噪音 |
| `/sessions [limit]` | 列出历史 sessions，并标记 active session | 你想查看或切换过往会话 |
| `/use <session_id>` | 切换当前 active session | 你想回到某个历史 session 继续审议 |
| `/export [session_id]` | 导出一个 session 的 JSON 和 Markdown | 你想备份或分享当前/指定会话 |
| `/export-all` | 导出全部 sessions | 你想一次性备份本地所有会话 |
| `/state` | 输出当前 shared state JSON | 你想检查系统当前记住了什么 |
| `/summary` | 输出当前 session 的简洁摘要 | 你想快速了解当前进度和下一步 |
| `/checkpoint` | 保存当前 state snapshot，并输出摘要 | 重要节点前后想手动留档 |
| `/close` | 在已有 Judge verdict 后关闭当前 major question | 当前问题已经裁决，准备进入下一问题 |
| `/close --force` | 未裁决也强制关闭当前 major question | 你明确想跳过裁决，但仍保留关闭记录 |
| `/reset` | 清空本地 sessions、turns、state_snapshots、clone_runs | 本地开发或测试时重置数据 |
| `/quit` | 退出 CLI | 结束当前 REPL |

不以 `/` 开头的自然语言输入会被分成两类：

- 追问/澄清：例如 `这是什么意思，什么的方案`、`为什么这么判断？`、`展开一下第二点`。系统会基于 recent turns 回答，不写入 `user_position`。
- 长期约束/立场更新：例如 `我的约束是不能接真实写接口`、`我希望先做离线验证`。系统会合并进 `user_position`，供后续 Chair 和角色参考。

如果你想明确新增长期约束，推荐写成：

```text
我的约束是：第一阶段不能接真实写 API，只能做离线回放。
```

`/auto <n>` 的 `n` 是调度步数，不是对话轮数。一次调度可能调用一个角色，也可能生成 clones 并触发 Merger。项目故意限制单次最多 3 步，避免早期错误判断被自动放大。

真实 LLM 模式下，`/auto 3` 可能触发多次串行 API 调用。交互式 CLI 会打印步骤级即时输出：

```text
[running] auto step 1: asking Chair for the next move
[chair] spawn_clones -> executioner x3: 当前 major question 尚缺少清晰攻击面。
[running] calling Executioner-1 (1/3)
[Executioner-1] strongest: ...
[running] merging 3 executioner clone output(s)
[merger] strongest merged point: ...
```

如果长时间停在某个 clone，通常表示正在等待该次 LLM 请求返回。

`/spawn` 只支持非 Chair 角色：

```text
executioner
defender
builder
judge
```

示例：

```text
/config executioner 3
/spawn executioner 3
```

clone 输出不会直接写入 shared state。系统会先把 clone 输出保存为 turns，再由 Merger 合并，只有 Merger 的结果可以更新 shared state。

`/close` 和 `/close --force` 的区别：

```text
/close
```

要求当前问题已有 Judge verdict。否则系统会拒绝关闭。

```text
/close --force
```

允许没有 verdict 也关闭，但会在 history 中记录：

```text
closed_without_judgement: true
```

这适合你明确知道当前问题暂时不值得继续，但它不是推荐的常规路径。

### 10.2 推荐流程

最短流程：

```text
/start <idea>
/auto 1
/summary
```

标准审议流程：

```text
/start <idea>
/auto 3
/judge
/close
/summary
```

手动控制角色：

```text
/start <idea>
/exec
/defend
/build
/judge
/close
```

显式 clone：

```text
/start <idea>
/config executioner 3
/spawn executioner 3
/judge
/close
```

进入下一个 major question：

```text
/close
/auto 1
```

未裁决强制关闭：

```text
/close --force
```

这会在 history 里标记：

```text
closed_without_judgement: true
```

---

## 11. 测试与质量检查

安装开发依赖：

```powershell
uv sync --dev
```

编译检查：

```powershell
uv run python -m py_compile config.py main.py llm.py router.py state.py storage.py merger.py validation.py
```

mock 模式测试：

```powershell
$env:KILLME_MOCK_LLM="1"
uv run pytest
```

lint：

```powershell
uv run ruff check .
```

如果只是运行项目，不需要安装 dev 依赖：

```powershell
uv sync --no-dev
```

---

## 12. 排错

### `uv sync --dev` 下载失败

这通常是网络、镜像源或 TLS 问题，不是项目代码问题。

可以先只装运行依赖：

```powershell
uv sync --no-dev
```

也可以尝试官方 PyPI：

```powershell
uv sync --dev --default-index https://pypi.org/simple
```

如果 TLS 证书链有问题：

```powershell
uv sync --dev --default-index https://pypi.org/simple --system-certs
```

### 真实 LLM 返回 403

先确认不是 mock：

```powershell
$env:KILLME_MOCK_LLM="0"
uv run python -c "from pathlib import Path; from config import load_config; c=load_config(Path('.')); print(c.mock_llm, bool(c.openai_api_key), c.openai_base_url, c.openai_model)"
```

如果 API key、base URL、model 都正确，但仍返回 403，通常是中转站拒绝请求、key 无权限、模型名不可用或账号策略限制。

当前 runtime 会发送：

```text
User-Agent: OpenAI/Python
```

这是为了兼容部分会拦截默认 `urllib` 请求头的 OpenAI-compatible 中转站。

### 中转站不支持 `response_format`

关闭 JSON response format：

```powershell
$env:KILLME_JSON_RESPONSE_FORMAT="0"
uv run python main.py
```

或者写入 `config.toml`：

```toml
json_response_format = false
```

### 测试误打真实 LLM

如果 `.env` 里是：

```env
KILLME_MOCK_LLM=0
```

直接运行 `uv run pytest` 可能会打真实 LLM。测试时显式开启 mock：

```powershell
$env:KILLME_MOCK_LLM="1"
uv run pytest
```

### SQLite `disk I/O error`

优先检查：

- 数据库路径是否在工作区内；
- `data/` 是否可写；
- 是否有另一个进程占用 sqlite 文件；
- 是否残留 `.sqlite-journal`；
- 当前 shell 是否有权限写入 `data/` 和 `__pycache__/`。

可以换一个临时数据库验证：

```powershell
uv run python main.py --db ./data/tmp.sqlite --init-db
```

---

## 13. 推荐本地工作流

开发或验证逻辑：

```powershell
$env:KILLME_MOCK_LLM="1"
uv run pytest
uv run ruff check .
uv run python main.py --db ./data/dev.sqlite
```

真实 LLM smoke test：

```powershell
$env:KILLME_MOCK_LLM="0"
uv run python main.py --db ./data/real-llm-smoke.sqlite
```

真实使用：

```powershell
$env:KILLME_MOCK_LLM="0"
uv run python main.py
```

在真实使用时，建议从 `/auto 1` 开始，确认 Chair 调度合理后再使用 `/auto 3`。
