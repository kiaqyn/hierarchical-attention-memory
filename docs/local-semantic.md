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

`--semantic-min-score` 默认0.875，`--semantic-min-margin` 默认0。它们来自早期小规模实验，**不是跨语言/所有项目的通用最优阈值**。显式设置范围分别为 [-1,1]、[0,2]；降低阈值会增加误报。实际功能检查另外用0.80证明过一个跨语言、无词法命中的路径，这不代表默认阈值已经全面解决英文问题。

这一轮不把实验重排/有限数值绑定自动搬进日常后端；那些仍在 `experiments/local_semantic`。当前集成重点是数据生命周期与调用链正确，不声称取得与成熟竞品同条件的准确率优势。

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

性能复验脚本：`experiments/local_semantic/benchmark_mcp_reuse.py`。用两个源码目录、同一本地模型和临时合成记忆比较真实 MCP 调用；磁盘向量预建，服务启动、首次查询与后续查询分开计时，同时核对结果正文、来源、排序和预算。它测调用开销，不是检索准确率或竞品排名。
