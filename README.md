# Hierarchical Attention Memory · HFA 2.2

> **本地事务型 AI 代理分层记忆系统**：以 `L0 (directory.md)` 主线专注锁隔离跨分支噪声，以 `L1 (chunks/*.json)` 作为 Git 可审计唯一真源，通过 Python 标准库内置 `sqlite3 (FTS5)` 提供零外部依赖的混合检索加速，并配备 `PreInvocation / PostInvocation / Stop` 全生命周期钩子与 `MCP` 服务端。
>
> *严守诚实工程原则：本项目不自行调用大模型 API、不上传任何对话数据，所有检索与提炼均为本地确定性逻辑。*

---

## 为什么需要 HFA？（核心痛点与架构对比）

在长周期 AI 编程与多分支协作中，现有记忆方案普遍面临三大工程瓶颈：

1. **单文件 `MEMORY.md` / Memory Bank 膨胀与串味**：每轮全量塞入上下文，随记录增长呈 $O(N)$ Token 消耗；旧方案、废弃分支与当前主线混杂，极易引发旧指令污染。
2. **纯黑盒 SQLite / 向量库不可审计与噪声堆积**：记忆锁死在二进制 `.db` 中无法用 `git diff` 逐行审查；无差别抓取每次工具输出会导致检索结果充斥闲聊与未验证中间态。
3. **仅靠 Prompt 提醒模型手动调工具极易遗忘**：缺少回合前自动召回（`PreInvocation`）与回合后自动幂等沉淀（`PostInvocation` / `Stop`）的闭环机制。

### 同类方案实事求是对比

| 维度 | 单文件 `MEMORY.md` / Memory Bank | 纯 SQLite / 向量 MCP 竞品 (如 Engram / claude-mem) | **HFA 2.2 (本项目)** |
| :--- | :--- | :--- | :--- |
| **持久化真源与 Git 可审计性** | Markdown 单文件（易冲突、难追溯替代关系） | 二进制 `.db` / 向量库（无法直接 `git diff` 审查） | **`chunks/chunk_XXXX.json` 原子 JSON 真源**（天然支持 `git diff`）+ 自动生成可读 Markdown 摘要 |
| **未命中回合 Token 开销** | 全文件常驻上下文（随历史线性增长） | 工具 Schema 常驻开销 | **`0 Token`**（`PreInvocation` 钩子未命中时输出 `{}`，不注入任何提示词） |
| **命中回合 Token 预算控制** | 无硬性上限（容易撑爆窗口） | Top-K 条目拼接（通常无精确 Token 截断） | **双重硬性预算锁**（默认路由 `R ≤ 600`、召回 `C ≤ 600` Tokens，最多 2 条片段，超额自动延后至 `deferred`） |
| **跨分支与废弃方案隔离** | 无隔离，全量混读 | 按项目或 Tag 简单过滤 | **L0 主线专注锁 (`ACTIVE M<n>`)**：支线与墓碑 (`TOMBSTONED`) 默认拦截，须显式 `--unlock` 说明用途并记入 `access.jsonl` 审计 |
| **旧决定推翻与历史演进** | 直接删改覆盖（丢失历史原因） | 追加新记录（旧错误结论仍可能被同分召回） | **显式 `supersedes` 替代链**：旧 `chunk_XXXX.json` 字节原样保留供审计，但永久退出当前有效召回池 |
| **回合自动读写闭环** | 依赖人工或模型自觉编辑文件 | `SessionStart` + `PostToolUse` / `Stop` 钩子 | **`PreInvocation` 自动检索注入 + `PostInvocation` / `Stop` 自动提炼结论/路径/关键词并按 `request_id` 幂等落盘 + `stdio MCP`** |
| **检索内核与运行依赖** | 无检索（全文硬塞） | Go 单二进制 FTS5 或 Node/Python + 向量库 | **Python 3.10+ 纯标准库**：精确命中词 + **`sqlite3` FTS5 (BM25)** + 中文双字分词 + 同义词组 + 否定句剥离 |

---

## 一键安装与零配置快速开始

需要 Python 3.10+；核心 CLI 与生命周期钩子**仅使用 Python 标准库（零第三方依赖）**。

### 方式 1：`pip` 安装 CLI 入口（推荐本地开发）

```powershell
# 仅安装标准库核心 CLI 与 Hook 入口 (hfa, hfa-hook, hfa-mcp)
python -m pip install -e .

# 或同时安装可选的官方 MCP SDK (Python 3.11+) 与 tiktoken 精确分词器
python -m pip install -e ".[all]"
```

安装后即可在任意工作区直接使用 `hfa`、`hfa-hook`、`hfa-mcp` 命令（未显式传入 `memory_dir` 时自动定位环境变量 `HFA_MEMORY_DIR` 或当前工作区 `./memory`）：

```powershell
hfa init
hfa status --json
hfa add-topic --title "编码修复" --hit-words "GBK乱码, 编码修复" --conclusion "使用 UTF-8 写入和解码。"
hfa recall --query "之前 GBK 乱码 怎么修的"
hfa reindex --verify
```

### 方式 2：`uvx` 零配置启动 MCP Server

若已安装 `uv`（Python 3.11+），无需手动克隆或预建目录，直接一行命令启动绑定当前工作区 `./memory`（不存在时自动初始化模板）的 MCP 服务：

```powershell
uvx --from "git+https://github.com/kiaqyn/hierarchical-attention-memory[mcp]" hfa-mcp
```

### 方式 3：原生脚本直接运行（免安装）

```powershell
python scripts/hfa_manager.py init ./my_memory
python scripts/hfa_manager.py status ./my_memory --json
python scripts/hfa_manager.py add-topic ./my_memory --title "编码修复" --hit-words "GBK乱码, 编码修复" --conclusion "使用 UTF-8 写入和解码。"
python scripts/hfa_manager.py recall ./my_memory --query "之前 GBK 乱码 怎么修的"
```

打开 `visualizer/index.html` 可在浏览器中体验交互式沙盘（沙盘数据仅存在于页面内存中，不连接本地磁盘或模型服务）。

---

## 让 AI 自动读写记忆（Lifecycle Hooks + MCP 双通道）

### 1. 全生命周期自动化钩子（`PreInvocation` + `PostInvocation` + `Stop`）

`scripts/hfa_hook.py`（或 `hfa-hook` 命令）配合根目录 `hooks.json` / `plugin.json`，在支持生命周期钩子的宿主中实现**零手动干预的读写闭环**：

- **`PreInvocation`（回合前自动按需召回）**：自动读取 `lastUserInput` 并执行混合检索。无命中时输出 `{}`（**0 额外 Token**）；命中主线记忆或发现支线 `deferred` / 偏好变更 `profile_drift` 线索时，通过单轮 `ephemeralMessage` 注入，并明确标注为历史参考数据而非覆盖用户的指令。
- **`PostInvocation` / `Stop`（回合后自动提炼与幂等落盘）**：当一轮对话结束并提供结构化结论（`decision` 字段或 ```` ```hfa-memory ```` 代码块）或包含明确结论/修复方案/文件路径的助手回复时：
  - **防污染过滤**：自动忽略日常闲聊与无实质结论的寒暄；自动对 `sk-...`、`ghp_...`、`AKIA...`、密码等敏感凭据执行 `[REDACTED]` 脱敏。
  - **结构化提炼**：自动提取标题、关联文件名/代码标识符/技术命中词（1–8 个）及核心结论（1–3 行），通过确定性 `request_id` 幂等写入 `chunks/chunk_XXXX.json`（同一回合重复触发 `PostInvocation` 与 `Stop` 不会重复生成文件）。
  - **零配置自愈**：若工作区尚未初始化记忆目录，回合结束需要落盘时会自动初始化 `<workspace>/memory`。

生成带当前环境验证绝对路径的 `hooks.json` 配置：

```powershell
python -B scripts/hfa_hook.py --print-hooks --memory-dir ./my_memory
```

### 2. 本地 stdio MCP 接口（`recall_memory` / `remember_decision` / `memory_health`）

`scripts/hfa_mcp.py`（或 `hfa-mcp` 命令）提供标准 MCP 协议工具接口。生成宿主 `mcpServers` 启动配置：

```powershell
python -B scripts/hfa_mcp.py --memory-dir ./my_memory --print-config
```

详细接入约定与代理调用规约见 [docs/ai-integration.md](docs/ai-integration.md)。

---

## 目录结构与单一真源协议

```text
memory/
  config.json                   # 主线、R/C Token 预算、每纪元主题数上限、自定义同义词
  directory.md                  # L0：节点、路径、状态、命中词（主线专注锁路由表）
  chunks/chunk_0001.json        # L1：唯一可持久化记忆真源（Git 友好，全局递增 ID）
  index/fts.sqlite              # 派生：Python 标准库 sqlite3 FTS5 全文索引与词频表（含 source_digest）
  index/search.json             # 派生：标题、正文与同义词的检索特征 JSON（含 source_digest）
  index/hitwords.json           # 派生：L0/L1 精确命中词倒排表（含 source_digest）
  index/projects.json           # 派生：L0 节点状态表（含 source_digest）
  history/epoch_01/summaries.md # 派生：由 L1 自动生成的人类可读纪元摘要视图
  history/epoch_01/framework.md # 纪元 ACTIVE / SEALED 状态与架构说明
  history/stream.md             # 可选里程碑事件流日志
  history/access.jsonl          # 召回与 --unlock 侧向读取审计日志
  history/legacy-backups/       # 重建前自动备份的旧版非生成 Markdown 原文
  tombstones.jsonl              # 支线逻辑归档（墓碑化）审计记录
  main/ profile/ branches/      # L0 指向的主线、用户画像与支线说明文档
```

- **全局唯一身份**：L1 文件 ID (`chunk_XXXX`) 在整棵记忆树内单调递增，跨纪元永不重置；`Topic#01` 仅作为所属纪元内的展示编号。
- **纪元自动滚动**：默认每纪元最多 50 条（`max_topics_per_epoch` 可配）。超出时自动封存旧纪元（`SEALED`）、开启新纪元并切换 L0 指针，旧 L1 文件与旧摘要完整保留。

---

## 混合检索（SQLite FTS5 + 词法守卫）与主线专注锁

`route` 和 `recall` 是同一个命令。每次召回先校验主线文件、L0/L1 SHA-256 指纹 (`source_digest`) 与索引一致性，再从 L1 返回受预算约束的片段；篡改生成的 Markdown 摘要不会改变召回结果。

- **主线专注锁**：L0 必须恰好有一个 `ACTIVE` 的 `M<n>` 主线。同主线 L1 片段可直接返回；非主线、支线和 `TOMBSTONED` 节点命中时仅进入 `deferred` 提示，必须显式传入 `--unlock "原因"` 才会展开正文并记录到 `history/access.jsonl`。
- **SQLite FTS5 + 词法混合检索**：
  1. L0/L1 精确命中词 (`hitwords`) 最高优先级匹配；
  2. 利用内置 `sqlite3` FTS5 虚拟表 (`chunks_fts`) 与 `term_df` 快速召回候选集，结合标题 3 倍加权 IDF 覆盖率、英文术语、中文双字切分与同义词组排序，同分时较新记录优先；
  3. 支持全角/半角 NFKC 归一化与否定从句剥离（如 `"不要查乱码和部署"` 不会误触发召回，而 `"不要改代码但请检查 IDE 配置"` 会保留转折后的正向意图）；
  4. 内置六组高频工程同义词（乱码、部署、备份、并发、超时、回滚），支持在 `config.json` 中通过 `retrieval_aliases` 自定义扩展。

```powershell
python scripts/hfa_manager.py tombstone ./my_memory --id S1 --reason "已完成，保留审计"
python scripts/hfa_manager.py recall ./my_memory --query "IDE 配置" --unlock "复核旧工具链方案"
```

---

## 幂等写入与决定演进（`--request-id` & `--supersedes`）

```powershell
python scripts/hfa_manager.py add-topic ./my_memory \
  --title "发布规则" \
  --hit-words "发布规则" \
  --conclusion "发布前先确认。" \
  --source "chat:用户确认位置" \
  --request-id "release-rule-001" \
  --json
```

- **重试幂等 (`--request-id`)**：同一事件使用相同 `--request-id` 重试时直接返回原 chunk ID（`duplicate: true`），不重复落盘；若同 ID 传入不同内容则明确报错拒绝。
- **显式替代 (`--supersedes chunk_XXXX`)**：改变已有决定时指明真实旧 chunk ID。被替代的旧记录不再作为当前有效决定返回（即使传入 `--unlock`），但旧 JSON 原件保留，且生成的 `summaries.md` 自动标出 `历史记录；已被 chunk_YYYY 替代`。

---

## 事务一致性、崩溃恢复与 Token 计量边界

- **多文件事务与恢复日志**：所有写入先持线程锁与跨进程文件锁 (`.hfa.lock`)，将变更前快照写入 `.hfa-transaction.json`，再经临时文件、`fsync` 与 `os.replace` 原子替换更新 L1、`index/fts.sqlite`、`index/*.json` 与 Markdown 视图。若进程中途崩溃，下一次任意 CLI 访问会先自动回滚或完成未决事务，再允许读取；恢复失败时锁定读取以防暴露半写状态。
- **Token 计量说明**：
  - 安装 `requirements-optional.txt` (`tiktoken==0.14.0`) 后使用 `o200k_base` 精确计量（`exact: true`）；
  - 未安装时使用按 CJK 字符、英文词根与符号分段校准的保守启发式上界估算（`method: "heuristic_estimate"`, `exact: false`），绝不伪装成精确分词器结果。
  - `router_token_budget`（默认 600）与 `recall_token_budget`（默认 600）分别约束 `router_text` 与 `recall_text` 正文载荷，不包含外层 JSON 字段名或宿主工具封装开销。

---

## 诚实能力边界声明（Scope & Limitations）

1. **非通用向量语义理解**：HFA 的混合检索基于精确命中词、`sqlite3` FTS5 倒排索引、中文双字切分与可配置同义词表。它具有确定性、零冷启动延迟和完全可解释的匹配词（`selected[].match`），但不是 Embedding 向量模型，对于完全无词汇重叠且未配置同义词的跨领域隐喻不会强行召回。
2. **评估集性质透明**：`tests/evaluate_memory.py` 包含 32 个公开可见的人工构造检索案例（混合模式正向召回 `23/23`，负向拒识 `9/9`，零误报）与 1,000 次工具读写/重试/替代操作回放。它是确定性工程回归基准，不代表独立盲测或千轮真实大模型对话的认知表现证明。
3. **进程级崩溃恢复而非断电级存储引擎**：`.hfa-transaction.json` 保障进程异常退出或普通 I/O 错误时的多文件回滚一致性；直接绕过 CLI 手动乱改文件不参与事务保护。

---

## 运行完整验证套件

```powershell
python -B tests/validate_hfa.py
python -B tests/test_memory_quality.py
python -B tests/test_hook.py
python -B tests/test_mcp.py
python -B tests/evaluate_memory.py
```

MIT License · 实事求是构建可审计、受预算约束的本地 AI 工程记忆。
