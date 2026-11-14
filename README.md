# Hierarchical Attention Memory · HFA 3

第一次使用请看 [五分钟上手](docs/quickstart.md)。`hfa init` 只建一条主线和一份摘要；`hfa init --example` 才安装教学用的分支和墓碑。已安装可运行 `hfa --version` 查看代码版本，或 `hfa doctor <记忆目录>` 检查本地查存与 MCP 依赖状态；本地就绪不等于宿主已接入。

> **本地事务型 AI 代理分层记忆系统**：以 `L0 (directory.md)` 主线专注锁隔离跨分支噪声，以 `L1 (chunks/*.json)` 作为 Git 可审计唯一真源，通过 Python 标准库内置 `sqlite3 (FTS5)` 提供零外部依赖的混合检索加速，并配备 `PreInvocation / PostInvocation / Stop` 全生命周期钩子与 `MCP` 服务端。
>
> *本项目不自行调用生成式大模型 API、不上传对话数据。默认检索与提炼使用本地规则；可显式启用本地 embedding 候选补位。相似度不等于事实充分性。*

可选预览：[本地语义后端](docs/local-semantic.md) 已接入 CLI/MCP 与安装包，支持增量向量缓存、有效记录过滤、并发快照复核、MCP 常驻编码器复用和超时恢复，以及显式启用的成对重排兜底（`--semantic-reranker`）与子句级事实约束绑定（`--bind-claims` / `--reference-date` / `config.json` 中的 `claim_binding`；默认均关闭）。不改变标准库核心依赖；需要 Python 3.12+ 及单独的 `semantic` 依赖，模型必须显式下载/指定。

代理输出可显式精简：CLI `recall --output compact` / MCP 启动 `--compact`，完整记忆正文与来源保留，去掉重复正文和诊断数据。见[接入与成本边界](docs/ai-integration.md#减少代理工具输出开销)；工具体积减少不等于已测得净额度节省。 项目配置还可显式启用 `max_recalls_per_turn`，按真实会话/回合限制成功召回份数；默认关闭，需要宿主提供一致身份，详见接入说明。

换种说法或证据分散时，宿主模型可在一次补查中传入至多3个短查询方向：MCP `refinement_queries`，或 CLI 重复使用 `--refine-query`。各方向独立词法检索、去重合并，共用原范围、条数和完整正文预算；不把多个事实的词硬拼成一个弱匹配查询，不自动生成或保存新词。未传时保持现有单查询行为。见[补查示例与验证边界](docs/query-refinement.md)。

读取 JSON 文本的 MCP 客户端还可启用 `--recall-text-only`：召回结果只返回一个文本块，避免 SDK 再附一份相同正文的 `structuredContent`。可与 `--compact` 组合；依赖结构化结果的客户端保持现有启动方式。`experiments/quota/evaluate_transport.py --output transport-check.json` 可用纯合成记忆复查真实 SDK 结果；宿主本来只读文本时，这项传递优化不产生额外节省。

---

## 为什么需要 HFA？（核心痛点与架构对比）

在长周期 AI 编程与多分支协作中，现有记忆方案普遍面临三大工程瓶颈：

1. **单文件 `MEMORY.md` / Memory Bank 膨胀与串味**：每轮全量塞入上下文，随记录增长呈 $O(N)$ Token 消耗；旧方案、废弃分支与当前主线混杂，极易引发旧指令污染。
2. **纯黑盒 SQLite / 向量库不可审计与噪声堆积**：记忆锁死在二进制 `.db` 中无法用 `git diff` 逐行审查；无差别抓取每次工具输出会导致检索结果充斥闲聊与未验证中间态。
3. **仅靠 Prompt 提醒模型手动调工具极易遗忘**：缺少回合前自动召回（`PreInvocation`）与回合后自动幂等沉淀（`PostInvocation` / `Stop`）的闭环机制。

### 同类方案实事求是对比

| 维度 | 单文件 `MEMORY.md` / Memory Bank | 纯 SQLite / 向量 MCP 竞品 (如 Engram / claude-mem) | **HFA 3 (本项目)** |
| :--- | :--- | :--- | :--- |
| **持久化真源与 Git 可审计性** | Markdown 单文件（易冲突、难追溯替代关系） | 二进制 `.db` / 向量库（无法直接 `git diff` 审查） | **`chunks/chunk_XXXX.json` 原子 JSON 真源**（天然支持 `git diff`）+ 自动生成可读 Markdown 摘要 |
| **未命中回合 Token 开销** | 全文件常驻上下文（随历史线性增长） | 工具 Schema 常驻开销 | 核心摘要仍会注入并计量，未命中不再宣称零开销 |
| **命中回合 Token 预算控制** | 无硬性上限（容易撑爆窗口） | Top-K 条目拼接（通常无精确 Token 截断） | 默认路由 600、召回片段 600、摘要 800；摘要、片段和提示组成的完整正文共受 1600 上限约束 |
| **跨分支与废弃方案隔离** | 无隔离，全量混读 | 按项目或 Tag 简单过滤 | **L0 主线专注锁 (`ACTIVE M<n>`)**：支线与墓碑 (`TOMBSTONED`) 默认拦截，须显式 `--unlock` 说明用途并记入 `access.jsonl` 审计 |
| **旧决定推翻与历史演进** | 直接删改覆盖（丢失历史原因） | 追加新记录（旧错误结论仍可能被同分召回） | **显式 `supersedes` 替代链**：旧 `chunk_XXXX.json` 字节原样保留供审计，但永久退出当前有效召回池 |
| **回合自动读写闭环** | 依赖人工或模型自觉编辑文件 | `SessionStart` + `PostToolUse` / `Stop` 钩子 | **`PreInvocation` 自动检索注入 + `PostInvocation` / `Stop` 受控提炼结论/路径/关键词并按 `request_id` 幂等落盘 + `stdio MCP`** |
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
hfa export --out backup-bundle.json --json
hfa import --bundle backup-bundle.json --json
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

打开 `visualizer/index.html` 可体验教学仿真：预设条件匹配与单步动画不连接本地磁盘、SQLite 或模型服务，不提供实测得分/耗时，也不证明宿主已接收上下文。没有对应示例的输入会显示未命中。

---

## 让 AI 自动读写记忆（Lifecycle Hooks + MCP 双通道）

### 1. 全生命周期自动化钩子（`PreInvocation` + `PostInvocation` + `Stop`）

`scripts/hfa_hook.py`（或 `hfa-hook` 命令）配合根目录 `hooks.json` / `plugin.json`，在支持生命周期钩子的宿主中提供自动召回与受控保存：

- **`PreInvocation`（回合前召回）**：自动读取 `lastUserInput`，把 `summary.md` 正文、相关 L1 片段与必要提示组成同一条计量后的 `ephemeralMessage`，并标注为历史参考数据。未命中仍携带核心摘要，避免漏掉长期约束，因此不再宣称零额外 Token。摘要修改后下一次召回立即读取新内容。
- **真实回合与有限重查**：宿主提供 `conversationId`/`sessionId` 和 `turnId`/`requestId`/`messageId` 时，按会话内的不同回合计数；同一回合重复检索不增加轮数，每 20 个不同回合提醒一次。缺失身份时返回 `turn_count: null`、`turn_count_exact: false`，只统计调用次数。已知回合首次未命中可提示一次换词重查，后续不重复提示；自动钩子仅对询问历史的输入提供此提示。
- **`PostInvocation` / `Stop`（回合后受控提炼与幂等落盘）**：接受显式结构化结论（`decision` 字段或 ```` ```hfa-memory ```` 代码块）；普通助手回复仅在宿主本轮提供布尔值 `memoryWriteAllowed: true` 时尝试提炼。`false` 或非布尔值拒绝保存，命中的用户“不要保存”指令优先于上述所有入口。
  - **防污染过滤**：忽略闲聊、没有明确结论的文本，以及命中假设/示例/未验证规则的内容。先对正文和元数据脱敏，再提取、截断标题与关键词，防止凭据从关键词绕回索引。支持常见 API 密钥、密码赋值、Bearer 与完整 PEM 私钥格式；这不是通用秘密检测器，也不能自动证明结论属实。
  - **结构化提炼**：自动提取标题、关联文件名/代码标识符/技术命中词（1–8 个）及核心结论（1–3 行），通过确定性 `request_id` 幂等写入 `chunks/chunk_XXXX.json`（同一回合重复触发 `PostInvocation` 与 `Stop` 不会重复生成文件）。
  - **目录绑定**：`--memory-dir` 优先于 `HFA_MEMORY_DIR`，其次才是工作区发现。已配置目录缺失时只初始化该目录；损坏或非空且未初始化时拒绝保存，不转写其他项目。多个工作区未指定根目录时拒绝猜测。只有通过保存条件后才会尝试初始化。
  - **对话与错误可见性**：支持从 `transcriptPath` 的 JSONL 尾部读取当前回合和用户禁存指令，不沿用上一回合回复。读取或写入失败在 stderr 给出不含聊天原文的提示，仍保持宿主要求的退出/Stop 返回格式。

升级注意：仅生成 hooks 配置不会自动添加逐轮写入许可。宿主若不能提供该信号，使用显式结构化决定或 MCP 的保存工具。已有含敏感信息的历史文件不会被本次修复自动清洗。

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
  index/fts-integrity.json      # 派生：SQLite 文件 SHA-256 与真源指纹，随索引事务提交
  index/search.json             # 派生：标题、正文与同义词的检索特征 JSON（含 source_digest）
  index/hitwords.json           # 派生：L0/L1 精确命中词倒排表（含 source_digest）
  index/projects.json           # 派生：L0 节点状态表（含 source_digest）
  history/epoch_01/summaries.md # 派生：由 L1 自动生成的人类可读纪元摘要视图
  history/epoch_01/framework.md # 纪元 ACTIVE / SEALED 状态与架构说明
  history/stream.md             # 可选里程碑事件流日志
  history/access.jsonl          # 召回与 --unlock 侧向读取审计日志
  history/session-state.json    # 会话/回合哈希与去重计数，同访问事件一起事务提交
  history/legacy-backups/       # 重建前自动备份的旧版非生成 Markdown 原文
  tombstones.jsonl              # 支线逻辑归档（墓碑化）审计记录
  main/ profile/ branches/      # L0 指向的主线、用户画像与支线说明文档
```

- **全局唯一身份**：L1 文件 ID (`chunk_XXXX`) 在整棵记忆树内单调递增，跨纪元永不重置；`Topic#01` 仅作为所属纪元内的展示编号。
- **纪元自动滚动**：默认每纪元最多 50 条（`max_topics_per_epoch` 可配）。超出时自动封存旧纪元（`SEALED`）、开启新纪元并只改写标题中的活跃纪元指针。其他 `history/epoch_*` 链接保持原样，旧 L1 文件与旧摘要完整保留。

---

## 混合检索（SQLite FTS5 + 词法守卫）与主线专注锁

`route` 和 `recall` 是同一个命令。默认召回只对照 `index/source-manifest.json` 里的大小和 mtime，不逐份重读 L0/L1，也不重算 SHA-256。内容改了、但大小不变且 mtime 被设回原值时，普通召回发现不了；整文件字节校验要加 `--full-verify`，或运行 `reindex --verify`。大小或 mtime 有变化时才退回全量读取。篡改生成的 Markdown 摘要不会改变召回结果。

清单吻合时从 `index/catalog.json` 和现有索引取字段。catalog 是派生缓存，不是第二真源。页内容变化、缺少完整性记录或结构校验失败会报错要求 `reindex`，不会当作正常未命中。SQLite 文件头 92–99 字节的版本字段与 `status` 一样不算损坏，且不会回写文件。校验用于发现损坏或过期，不防御有权限同时改写真源和所有校验文件的攻击者。健康索引上的查询仍要读取目录、摘要，以及本次命中的索引。

`status --json` 与 `doctor` 一样默认不返回 `summary.md` 正文。需要旧的完整状态时加 `--include-text`。

会话账本和 `history/access.jsonl` 可以在 `config.json` 里用 `session_retention.max_conversations` 与 `access_retention.max_events` 限制长度。`keep_unlocks: true` 时带解锁理由的行继续保留，因此条数可能超过上限。未配置的旧目录不裁剪。裁剪和当次写入在同一事务里。

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
  - 未安装时按 UTF-8 字节数计量（`method: "utf8_bytes_upper_bound"`, `exact: false`），作为 `o200k_base` 的保守上界；不再用可能低估罕见汉字的字符启发式。相同预算下可容纳的内容会更少，可缩短结论或安装可选分词器；不是精确模型账单，也不承诺其他分词器的上界。
  - `router_token_budget`（默认 600）与 `recall_token_budget`（默认 600）分别约束 `router_text` 与 `recall_text` 正文载荷，不包含外层 JSON 字段名或宿主工具封装开销。
  - `summary_token_budget` 默认 800；`context_token_budget` 默认 1600，约束实际传给 Hook/MCP 的完整正文 `context_text`，包含摘要、片段与提示。超限优先延后片段，再省略可选提示；核心摘要本身放不下时明确报错。工具外层 JSON 元数据与宿主封装仍不在正文预算内。

升级已有记忆目录后先运行 `reindex`，再检查 `status --json`，以生成新增的索引完整性记录。旧访问日志没有可靠回合身份，不倒推出真实对话轮数。

`delivery.payload_prepared: true` 只证明正文已生成；`host_accepted` 与 `model_used` 为 null，需要由宿主接收记录和真实对话评估另行验证。动画播放结束或 SDK 返回成功都不能证明模型已遵守记忆。

---

## 诚实能力边界声明（Scope & Limitations）

1. **默认词法与可选语义的边界**：HFA 的默认混合检索基于精确命中词、`sqlite3` FTS5 倒排索引、中文双字切分与可配置同义词表。它具有确定性和可解释的匹配词（`selected[].match`），但默认路径不是 Embedding 向量模型，对于完全无词汇重叠且未配置同义词的跨领域隐喻不会强行召回。可选本地语义模式另见 [后端说明](docs/local-semantic.md)，其相似度不保证事实正确，也不涵盖任意隐喻。查询时在清单通过后以只读方式查询磁盘 `index/fts.sqlite`。内容哈希一致时不重建表；只有哈希不一致时才在内存中对照期望映像，且不把结果写回磁盘。默认热路径不是每次全量 SHA-256：同样大小且 mtime 被恢复的修改，要 `--full-verify` 或 `reindex --verify` 才查得出。不能据此宣称零冷启动延迟或大规模性能已验证。
2. **评估集性质透明**：`tests/evaluate_memory.py` 包含 32 个公开可见的人工构造检索案例（混合模式正向召回 `23/23`，负向拒识 `9/9`，零误报）与 1,000 次工具读写/重试/替代操作回放。它是确定性工程回归基准，不代表独立盲测或千轮真实大模型对话的认知表现证明。
3. **进程级崩溃恢复而非断电级存储引擎**：`.hfa-transaction.json` 保障进程异常退出或普通 I/O 错误时的多文件回滚一致性；直接绕过 CLI 手动乱改文件不参与事务保护。
4. **子句级绑定与外部基准的真实范围**：`--bind-claims` / `claim_binding` 使用确定性正则与日期/数值校验（支持 `--reference-date YYYY-MM-DD`），在截断前将错配候选退回 `deferred`，但不等同于通用 NLI 蕴含证明；`benchmarks/general-memory-v1/run_hfa3.py` 用于独立隔离验证 `records` 与 `operational` 用例，不伪造未实现能力（如离线包导入导出或原生对话生成），不支持的能力明确报告为 `unsupported`。

---

## 运行完整验证套件

```powershell
python -B tests/validate_hfa.py
python -B tests/test_memory_quality.py
python -B tests/test_hook.py
python -B tests/test_review_regressions.py
python -B tests/test_context_delivery.py
python -B tests/test_mcp.py
python -B tests/evaluate_memory.py
```

可重复的工具成本回归（仓库源码或源码包，需要可选分词器）：

```powershell
python -m pip install ".[tokenizer]"
python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"
python experiments/quota/cost_gate.py
```

普通检查不会更新参考值；`--output` 可保存完整证据。它同时检查完整依据、范围与成本，采用7个开发者合成场景、4种HFA配置，保留单条查询反而增加补查的反例；不代表独立竞品排名或实际账户节省。细节见[接入与成本边界](docs/ai-integration.md#减少代理工具输出开销)。

完整 `unittest` 回归与 `general-memory-v1` 独立基准（按需启用可选 `tiktoken`、本地语义模型与重排模型以覆盖全部用例）：

```powershell
# 运行全量单元测试（配置 HFA_TEST_SEMANTIC_MODEL / HFA_TEST_RERANKER_MODEL 与 tiktoken 后可覆盖此前跳过的用例）
python -B -m unittest discover -s tests -v

# 运行 general-memory-v1 基准（混合模式；不支持的原生生成与导出/硬删用例会诚实标为 unsupported）
python -B benchmarks/general-memory-v1/run_hfa3.py --modes records,operational --retrieval-mode hybrid

# 启用本地语义模型与重排模型运行基准（超大规模填充集按上限退回混合检索以控制耗时）
$sh = Join-Path $env:LOCALAPPDATA 'HFA-Local-Semantic'
python -B benchmarks/general-memory-v1/run_hfa3.py --modes records,operational --retrieval-mode semantic --semantic-model "$sh\model" --semantic-reranker "$sh\reranker-model" --max-scale-semantic-records 500
```

MIT License · 实事求是构建可审计、受预算约束的本地 AI 工程记忆。
