# Hierarchical Attention Memory · HFA 2.2

一个本地文件记忆管理原型：以 L0 目录限制项目范围，以 L1 JSON 保存记忆，用可重建的索引返回少量片段。包含 Python CLI、可选的本地 MCP 工具接口和独立浏览器沙盘。不会自行调用模型，也不保证长对话中的模型表现。

## 快速开始

需要 Python 3.10 或以上；基本功能仅使用标准库。在项目根目录运行：

```powershell
python scripts/hfa_manager.py init ./my_memory
python scripts/hfa_manager.py status ./my_memory --json
python scripts/hfa_manager.py add-topic ./my_memory --title "编码修复" --hit-words "GBK乱码, 编码修复" --conclusion "使用 UTF-8 写入和解码。"
python scripts/hfa_manager.py recall ./my_memory --query "之前 GBK 乱码 怎么修的"
python scripts/hfa_manager.py reindex ./my_memory --verify
```

打开 `visualizer/index.html` 可体验浏览器沙盘。沙盘数据仅存在页面内存中，未连接 CLI、磁盘记忆或模型服务；重置按钮只重置本页。

## 文件与真源

```text
memory/
  config.json                  # 主线、预算、每纪元主题数上限
  directory.md                 # L0：节点、路径、状态、命中词
  chunks/chunk_0001.json        # L1：唯一可持久化记忆真源
  index/hitwords.json           # L0/L1 派生索引，含 source_digest
  index/projects.json          # L0 派生节点表，含 source_digest
  index/search.json            # 标题、正文与同义词的派生检索特征
  history/epoch_01/summaries.md # L1 生成的可读视图
  history/epoch_01/framework.md # 纪元 ACTIVE / SEALED 状态
  history/stream.md            # 可选里程碑日志
  history/access.jsonl         # CLI 召回访问记录
  history/legacy-backups/      # 被替换的旧版非生成摘要原文
  tombstones.jsonl             # 逻辑归档审计记录
  main/ profile/ branches/     # L0 指向的说明文件
```

L1 文件 ID 在整棵记忆树内递增，跨纪元不重置。`Topic#01` 等显示编号仅在所属纪元内有效；引用历史时使用全局 `chunk_XXXX` ID。

模板默认每纪元最多 50 条；配置 `max_topics_per_epoch` 可调整。写入下一条时，新建纪元、封存旧纪元并切换 L0 指针，旧 L1 文件与旧摘要均保留。

旧版本的两种 JSON 字段拼写可直接读取。重建时，旧版非生成 Markdown 摘要会先按内容哈希备份；Markdown 独有内容不会被自动提升成可信 L1。旧版本已经覆盖的内容需要从原摘要或备份人工核对恢复。

## 召回与专注锁

`route` 和 `recall` 是同一个命令。它们核验主线文件、L0/L1 内容指纹和索引，再从 L1 返回片段；修改生成的 Markdown 不会改变召回事实。

- L0 必须恰好有一个 `ACTIVE` 的 `M<n>` 主线。
- 同主线的 L1 片段可直接返回。非主线、支线和墓碑只提示命中，需 `--unlock` 明确说明用途才展开。
- 默认最多选择两项，总召回载荷受配置预算限制。超出项进入 `deferred`，不返回整份摘要池。
- 默认混合检索：精确关键词优先，再用标题/正文的英文词、中文双字片段和有限同义词补充；按相关性排序，同分时较新记录优先。`selected[].match` 解释命中依据，分数不是正确率。
- 支持全角字符、空白归一化与简单否定/转折；弱匹配会放弃。它仍是词法检索，不是通用语义理解。`--mode keyword` 可使用仅关键词模式对照。
- 内置六组同义词（乱码、部署、备份、并发、超时、回滚）；项目可以在 `config.json` 添加 `retrieval_aliases`，例如 `[["保险箱", "知识仓库"]]`，修改后重建索引。
- 明确被新记录替代的旧记录不再作为当前答案返回，即使指定 `--unlock`。旧文件和来源仍保留，生成摘要会标记替代关系。
- `profile_drift` 仅提示可能的偏好变更，不自动写入画像。`lateral_attention_triggered` 是供上层代理参考的布尔信号，不表示已经调用其他模型。

```powershell
python scripts/hfa_manager.py tombstone ./my_memory --id S1 --reason "已完成，保留审计"
python scripts/hfa_manager.py recall ./my_memory --query "IDE 配置" --unlock "复核旧工具链方案"
```

返回的 `routed_files` 是实际相对路径；`selected` 含全局 ID、路径和片段。`resident_references` 只是主线和画像的路径引用，不会悄悄把这些文件正文塞进载荷。

## 更新决定与重试去重

```powershell
python scripts/hfa_manager.py add-topic ./my_memory --title "发布规则" --hit-words "发布规则" --conclusion "发布前先确认。" --source "chat:用户确认位置" --request-id "release-rule-001" --json
```

同一事件用相同 `--request-id` 重试，只返回原来的 ID，不重复写入；同 ID 换内容会明确报错。每次不同事件应使用新 ID。不提供此参数时，保留旧 CLI 行为，不自动猜测两段文字是否代表同一件事。

需要改变已有决定时，用 `--supersedes chunk_XXXX` 指明真实旧 ID，可重复该参数替代多条。只有当前主线中尚未被替代的有效记录能被替代；旧 L1 文件不修改，历史摘要也会同步标出“已替代”。失败回滚时，旧决定仍有效。

`--source` 是单行证据指针，最多 240 字符；它和结论一起计入召回预算。程序不自动验证来源真实性。只保存明确且已核对的变更，不自动“理解”一句话然后改写长期偏好。

## 让 AI 真正调用（MCP 与 PreInvocation 钩子）

- **PreInvocation 自动检索钩子（纯标准库）**：`scripts/hfa_hook.py` 配合 `hooks.json` / `plugin.json`，在支持 `PreInvocation` 生命周期钩子的宿主中自动读取 `lastUserInput` 并调用 `recall`。无命中输出 `{}`（零额外 Token）；命中或存在 `deferred` 线索时通过单轮 `ephemeralMessage` 注入，异常或未初始化时 fail-open 退出 `0` 绝不阻塞对话。运行 `python -B scripts/hfa_hook.py --print-hooks --memory-dir ./my_memory` 可生成绝对路径配置。
- **可选 MCP 接口**：`scripts/hfa_mcp.py` 提供 `recall_memory`、`remember_decision`、`memory_health`。安装、生成绝对路径启动项和代理工作约定见 [接入说明](docs/ai-integration.md)。接口只操作启动时绑定的记忆目录，不调用模型、不上传历史。宿主启用并实际调用后才会参与聊天；浏览器沙盘仍独立运行。

## 升级旧目录

2.2 增加了 `index/search.json` 和更明确的历史摘要状态。保留旧目录，先运行 `reindex`，再运行 `status --json`；没有重建时召回会明确拒绝过期索引。L1 原有字段保持兼容，不需要重写旧 JSON。

## Token 统计

可选安装精确分词依赖：

```powershell
python -m pip install -r requirements-optional.txt
```

安装后使用 `o200k_base` 编码完整文本，包含链接目标、括号、标题与标点。未安装或词表不可用时，使用按 CJK 字符、英文与符号分段校准的保守启发式估算 (`heuristic_estimate`)，并明确输出 `exact: false`，不冒充精确 tokens。

- R：返回字段 `router_text` 的全文。默认预算 600。
- C：返回字段 `recall_text` 的全文。默认预算 600。
- 配置来自 `config.json`；超出 R 则拒绝召回，超出 C 的候选项延后。

这两个预算不包含 JSON 元数据、工具调用封装或额外读取主线/画像产生的消耗，因此不能当作整轮模型计费量。沙盘使用明确标注的启发式估算，不作为 CLI 分词器结果。

## 一致性与恢复

所有 CLI 读写都经过线程锁和有超时的进程锁。写入先记录回滚日志，再以临时文件、`fsync`、原子替换更新目标文件；全部成功后标记提交。普通 I/O 错误会回滚；进程意外结束后，下一个 CLI 请求先恢复未完成事务，再允许读取。

不能恢复时保留日志并阻止读取，避免把半写状态当成成功。直接绕开 CLI 读取/修改文件不参与这个多文件事务协议；本项目不宣称具备断电级持久性保证。

```powershell
python scripts/hfa_manager.py reindex ./my_memory
python scripts/hfa_manager.py summarize ./my_memory --check
python scripts/hfa_manager.py status ./my_memory --json
```

`reindex` 和 `summarize` 均从 L0/L1 重建索引与摘要；`--verify` / `--check` 只检查，返回非零表示不一致。手动编辑 L0、L1 或配置后应先重建。为检查外部编辑，当前仍会扫描 L1 计算指纹，尚未优化大规模检索性能。

`init --force` 只补充缺失模板，不删除记忆目录，也不覆盖已有正文。

## 验证

```powershell
python -B tests/validate_hfa.py
python -B tests/test_memory_quality.py
python -B tests/evaluate_memory.py
```

测试覆盖跨纪元保留、真实多进程并发、I/O 回滚、提交前后进程中断恢复、派生视图篡改、完整 Token 统计、预算、路由边界和 Windows 编码。

评估脚本的案例和预期结果公开在源码中；它是人工构造的回归集，不代表独立真实用户测试。

MIT License；不承诺固定 Token 节省率或未经测量的模型认知效果。
