# 显式启用的本地语义补查后端

状态：**可集成预览**。已接入源码 CLI/MCP 和安装包；默认关闭，日常安装没有自动升级。当前策略是词法优先、语义候选补位，仍需调用者判断资料是否足以回答。

## 这一轮解决的问题

实验脚本可以找到相似记录，但不能直接承担日常新增、替代、归档和并发写入后的索引维护。现在正式源码增加本地后端：

- 真源仍为 L1 JSON。语义缓存是可删除、可重建的派生数据，不改变原始记忆。
- 每次语义召回检查当前主线有效记录，只为新增/变化的正文重新编码；删除、归档或被替代的记录不再参与当前语义结果。
- 编码前排除非当前主线、非 ACTIVE 和被替代记录。即使有 `--unlock`，语义部分仍只覆盖当前主线；原有词法侧向读取规则独立保留。
- 模型与 tokenizer 固定修订及 SHA-256，不根据本地 manifest 自报的哈希信任替换权重。
- 模型加载/推理不持有记忆事务锁；提交前重新核对快照，变化时明确失败并要求重试。
- 缓存损坏会在下一次语义读取时重建，并返回 `cache_status=rebuilt_corrupt`。模型/依赖不可用则返回明确错误，不伪装成正常零命中。
- 缓存通过原有事务与恢复路径写入。它提供进程崩溃恢复，不是硬件断电保证。
- 提交前仍重查当前真源；底层事务在缓存序列化字节完全相同时跳过实际写入，不创建缓存事务，召回计数和访问审计仍照常提交。推理期间缓存被删、损坏或改变时会写回正确版本，不仅凭早先的 `cache_status=reused` 判断。重复执行无变化的 `semantic-index` 同样不重写缓存。

## 安装与模型准备

核心仍支持 Python 3.10+ 且无外部依赖。本轮固定的可选语义依赖要求 **Python 3.12+**，实际验证环境为 Windows x64 / Python 3.13 / CPU。其他系统与CPU型号尚未逐一验收。

仓库根目录的 PowerShell 示例，环境和权重放在仓库外：

```powershell
$semanticHome = Join-Path $env:LOCALAPPDATA 'HFA-Local-Semantic'
python -m venv "$semanticHome\env"
$semanticPython = "$semanticHome\env\Scripts\python.exe"
& $semanticPython -m pip install '.[semantic,mcp]'
# 只有显式运行此命令才下载公开模型；约136 MB。不是推理API调用。
& $semanticPython -m scripts.hfa_download_model --output "$semanticHome\model"
& $semanticPython -m scripts.hfa_download_model --output "$semanticHome\model" --verify
```

只需要 CLI 时可安装 `.[semantic]`；原有 `.[all]` 仍保留此前 MCP/tokenizer 依赖集合，语义依赖需显式选择。模型由 `intfloat/multilingual-e5-small` 官方固定修订提供；使用量化 ONNX、query/passage 前缀、512 token 截断、masked mean pooling 和 L2 归一化。不支持随意换一个维度相同的模型而沿用旧缓存。

## 先在专用合成目录运行

```powershell
& $semanticPython -m scripts.hfa_manager init ./semantic-sandbox
& $semanticPython -m scripts.hfa_manager semantic-index ./semantic-sandbox --model-dir "$semanticHome\model"
& $semanticPython -m scripts.hfa_manager recall ./semantic-sandbox --query '过去怎样解决字符显示成方框？' --mode semantic --semantic-model "$semanticHome\model"
& $semanticPython -m scripts.hfa_manager semantic-index ./semantic-sandbox --model-dir "$semanticHome\model" --verify
```

`semantic-index --verify` 不更新缓存；当前快照和缓存不一致时返回非零。原有 `status` 检查核心索引，不把可按需刷新、已过期的可选语义缓存算作核心损坏。

对 L1 手工编辑后，仍必须按原流程 `reindex`。语义召回沿用默认热路径：只对照大小和 mtime。同样大小且 mtime 被设回原值的修改，普通召回发现不了；整文件字节校验用 `--full-verify` 或 `reindex --verify`。大小或 mtime 变了才会报过期，再在核心重建后刷新向量。CLI 写入和显式替代决定则会自动维护核心派生索引。

## MCP 接入

```powershell
& $semanticPython -m scripts.hfa_mcp --memory-dir ./semantic-sandbox --semantic-model "$semanticHome\model" --print-config
```

仅输出配置，不会替你写全局配置或重载服务。将生成的配置用于专用测试服务后，`recall_memory` 走同一后端；`remember_decision` 和 `memory_health` 保留原有接口。结构化结果多出 `retrieval`，可查看是否启用、缓存复用/重建、实际重编码条数和模型版本。

本轮没有给 Hook 增加默认语义配置，没有切换真实记忆目录，也没有新的 Antigravity 模型遵守验收。实际验证的是本地 CLI、真实 stdio SDK 工具链和安装包，不把工具返回当成宿主模型已使用。

## 排序、预算与效果边界

语义模式保留原有命中词/词法优先级，仅用达到阈值的稠密候选补充不足；不是把高相似度直接当答案。内部默认预取至少8条，最终仍受既有主题数量、召回正文预算与完整上下文预算限制。

### 同一轮的多个补查方向

CLI 可重复传入最多3个 `--refine-query`，MCP 对应 `refinement_queries`。这些方向由调用者提供，引擎不会生成假设或自行调用语言模型。例如原问题问“上传和恢复分别有什么限制”，调用者可分开提供“大文件传输方案”和“断点续传凭据”两个方向。

显式语义模式会把**原问题及所有补查方向**交给同一个本地编码器，一次编码器调用传入整个查询计划；内部每条文本分别执行本地 ONNX 推理。此前这些补查方向仅用于词法检索，语义部分仍只查原问题。每个方向单独通过原有相似度和间隔门槛，再按倒数排名融合；同一记录只占一个返回位置。词法命中仍优先，单个方向的旧排序保持不变。

所有方向共享主线、有效记录过滤、召回条数和总预算；一次计划仍算一次召回，不会给每个方向额外分配预算。模型和文档向量照常复用，不把查询词写成长期记忆。原来的默认词法模式和显式语义开关不变。

当显式提供至少两个补查方向时，选择阶段先保留当前最相关的第一条，再优先从尚无返回记录的补查方向中选取候选，最后用原排序补满剩余位置。这样同一方向因原问题和补查重复命中而得分很高时，不会轻易把另一方向挤出有限的返回位置。单个原问题或一个补查方向的选择顺序不变。多方向计划中，这条覆盖规则可以让已经通过门槛的语义候选排在重复方向的词法候选之前；`query_plan.selection=first_relevance_then_uncovered_direction` 明确标识这项选择策略。

只有实际选中且符合范围和召回预算的记录才占用方向；过长、被替代、未解锁的候选不会算已覆盖。最终完整上下文预算仍可进一步裁剪，因此还应检查最终 `coverage`，而不能把选择策略当作“必定找齐”的保证。一个记录可覆盖多个方向，不会为了凑方向数而重复返回。

完整和精简结果中的 `query_plan` 提供：

- `semantic_query=all_directions`、`semantic_queries`：实际查询方向数；`retrieval.query_batches` 统计查询计划对编码器的调用次数，不是 ONNX 推理次数，也不是远程模型调用数；原问题加3个方向最多执行4次本地查询推理。
- `coverage`：各方向的 `query_index`、最终 `selected_ids`、前5个 `deferred_ids` 及 `deferred_total`。只列少量暂缓ID，防止诊断信息随记忆库膨胀。索引0指原问题，1起依次指去重后的补查方向。
- `uncovered_query_indices`：最终没有返回候选记录的方向。覆盖在完整上下文裁剪之后计算，受范围或预算限制的候选不能冒充已交付证据。
- 完整调试结果同时保留去重后的 `refinement_queries` 和原参数位置 `refinement_input_indices`（从0起）。精简模式只保留两者中序列化UTF8字节更短的一种；位置列表仍与去重后的方向逐项对应，重复输入不会使 `query_index` 指错方向。没有合法位置映射的旧结果继续回显正文。

`coverage_means=returned_candidates_only`：有候选不代表资料充分、事实正确或所有隐含含义都已理解。原问题可能是总括性问句，因此索引0未覆盖而分项有候选是正常情况。访问日志仅记方向数量，不记查询假设正文。跨语言阈值仍需独立校准，方向越多也可能增加误报；不能把换词后多返回记录直接当作准确率提高。

`--semantic-min-score` 默认0.875，`--semantic-min-margin` 默认0。它们来自早期小规模实验，**不是跨语言/所有项目的通用最优阈值**。显式设置范围分别为 [-1,1]、[0,2]；降低阈值会增加误报。实际功能检查另外用0.80证明过一个跨语言、无词法命中的路径，这不代表默认阈值已经全面解决英文问题。

### 显式启用的本地成对相关性兜底（`--semantic-reranker`）

当复杂改写或跨语言提问未达到默认稠密阈值（0.875）时，直接放宽全局阈值会显著增加误报。CLI 与 MCP 现支持显式附加已固定哈希的本地 cross-encoder 模型目录（`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`，修订 `1427fd652930e4ba29e8149678df786c240d8825`）：

```powershell
& $semanticPython -m scripts.hfa_manager recall ./semantic-sandbox --query 'How do we avoid charging twice when a payment event is delivered again?' --mode semantic --semantic-model "$semanticHome\model" --semantic-reranker "$semanticHome\reranker-model"
& $semanticPython -m scripts.hfa_mcp --memory-dir ./semantic-sandbox --semantic-model "$semanticHome\model" --semantic-reranker "$semanticHome\reranker-model" --print-config
```

- **默认关闭且要求先启用 `--semantic-model`**：不自动下载模型，不修改默认词法或单模型语义行为；每次请求同样核对 `manifest.json`、`model.onnx` 与 `tokenizer.json` 的固定 SHA-256。
- **保留原有命中，只对空方向做有界兜底**：已通过第一阶段稠密门槛的查询方向保持原结果不变；仅当某个方向在第一阶段无命中时，才从当前主线 ACTIVE、未被替代且稠密相似度 `>= 0.75` 的前8条候选中送入成对打分（单次召回最多24对，逐条 singleton ONNX 推理以避免量化批次漂移）。若调用方显式提供了 `refinement_queries`，只对未命中的具体补查方向兜底，不对泛化的第0号原问题重复打分。
- **双重门槛与保守缺失字段否决**：每个兜底方向最多补入1条记录，要求 `pair_score >= 0.6` 且领先第二名 `>= 0.2`。若问题明确索取责任人（who/谁）、金额/预算、时长、百分比、64位哈希、具体年份或“实测而非计划”数值，而最佳候选中不存在对应字面格式，则直接否决该方向，绝不退而求其次把第二名提上来。
- **可审计诊断与诚实边界**：仅靠重排补入的记录标记 `match.method=semantic_rerank`，并附带 `match.rerank_support`（含 `query_index` 与 `pair_score`）；已由第一阶段命中的记录保留 `method=semantic`。`retrieval.reranker` 报告模型版本、实际打分对数、接受的方向索引及权重复用状态。成对相关性加有限字段检查仍只是候选筛选，不是通用事实蕴含证明，也不能解决所有跨语言或隐式改写漏检。

### 相同内容的编码稳定性

固定量化模型此前把多条文本放入同一推理批次，实测相同文本会因同批内容或长度变化而得到不同向量，导致全量重建与逐条增量建索引不一致。现在每条文本独立推理，同时复用已加载模型。512 token 截断、模型文件、相似度门槛和召回预算保持原样。

编码流程标识更新为 `e5-query-passage-mean-l2-512-singleton-v2`；旧向量缓存首次使用时会按现有流程重建，避免把旧、新编码结果混用。只重建派生向量，不改原始记忆。短文本批量推理次数会增加，混合长短文本的填充量可能下降；性能取决于文本分布，不能声称普遍更快或节省模型额度。

缓存文件为 `index/semantic-M<n>.json`，包含有效记录 ID、内容指纹和向量，不含正文副本，但向量也应按敏感派生数据对待。索引时覆盖该主线当前有效记录，按内容复用；主线切换使用不同缓存文件。查询按默认热路径核对真源：大小或 mtime 变了才重读；同样大小且 mtime 被恢复的修改不会在这里暴露。失效条目不会因为物理文件尚在而返回。索引加载/校验与排序仍为线性规模，没有 ANN 或大规模性能承诺。

CLI 单独运行仍有冷启动；显式开启语义模式的 MCP 现在复用一个按需启动的独立工作进程，连续召回复用已加载模型。独立同条件竞品评测和长期真实使用仍是后续工作。本轮不为追功能表默认开启或迁移用户数据。

## MCP 模型复用与故障边界

- 第一次实际语义召回才加载模型；工具发现、健康检查、要求澄清的回答不会启动模型。服务重开后的第一次仍需冷启动。
- 一个 MCP 服务绑定一个记忆目录和一个模型目录。工作进程只接受召回参数，不能被工具参数改成其他目录、阈值或写入命令。不同服务不共享模型进程。
- 仅复用编码器，不缓存查询答案。每次仍校验固定模型文件的完整哈希、有效记录和预算。记忆真源沿用默认召回的大小/mtime 清单，不是每次重读全部 L1；同样大小且 mtime 被恢复的修改要 `--full-verify` 或 `reindex --verify`。外部新增、替代和归档在清单失配后按当前快照生效。
- `retrieval.encoder_reused` 表示本次是否沿用已加载编码器，`encoder_loads` 是当前工作进程中的成功加载次数。磁盘向量复用另看 `cache_status` / `encoded_documents`，两者不同。
- 工作进程串行推理，避免同一 tokenizer 被并发调用。排队加执行默认最多120秒；排队超时不会发送请求，执行超时会终止该进程，下次显式调用重新启动。
- 卡死、崩溃、非法回复返回明确错误，不回退成零命中，也不自动重放请求。失败前可能已经提交回合计数，重试必须沿用真实 `conversation_id` / `turn_id`。
- 正常服务关闭会回收工作进程。关闭时等待正在处理的有界调用结束；停止客户端等待不保证已完成的计数写入被撤销。进程被强杀时，既有事务恢复规则仍适用。
- 写入与健康检查继续使用原有 CLI 子进程，默认词法服务不启动常驻语义模型。

### 子句级事实约束绑定（`--bind-claims` / `claim_binding`）

为降低“实体匹配但版本、年份、金额、时长或新旧状态混淆”的错配风险，纯标准库模块 `scripts/hfa_binding.py` 已接入主线 CLI、worker 与 MCP（默认关闭，不改变既有默认召回行为）：

- **显式开启**：在记忆目录 `config.json` 中设置 `"claim_binding": true`（仅接受布尔值），或在 `recall` / `route` 时传入 `--bind-claims` / `--no-bind-claims` 与可选 `--reference-date YYYY-MM-DD`；MCP 启动支持 `--bind-claims` / `--no-bind-claims` 设服务默认值，工具 `recall_memory` 支持单次传入 `bind_claims` 与 `reference_date`。
- **拦截位置与审计结果**：在按 `topic_limit` 与 `recall_token_budget` 截断之前，对当前主线允许或已 `--unlock` 解锁的候选正文（`title` 与 `summary`）执行子句级约束校验。错配候选会退回 `deferred` 并附 `reason: "claim_binding:<status>"`；结果新增 `claim_binding` 摘要（`--compact` 保留精简统计）。与 `--semantic-reranker` 同时使用时，`reference_date` 会透传至成对重排候选筛选。
- **诚实边界**：基于确定性的术语、数值/单位（时长含 `ms`、百分比、金额、64 位哈希、年份、`changed to` / `now` 与相对日期）校验，不调用生成式大模型，也不是通用 NLI 蕴含证明。

性能复验脚本：`experiments/local_semantic/benchmark_mcp_reuse.py`。用两个源码目录、同一本地模型和临时合成记忆比较真实 MCP 调用；磁盘向量预建，服务启动、首次查询与后续查询分开计时，同时核对结果正文、来源、排序和预算。它测调用开销，不是检索准确率或竞品排名。
