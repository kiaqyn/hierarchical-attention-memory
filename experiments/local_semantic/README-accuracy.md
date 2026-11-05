# 更准确的本地召回：实验 V2.1

状态：仅实验。未修改日常 HFA、Hook、MCP、全局配置或真实记忆。评测使用纯合成数据，不生成最终回答。

## 改了什么

1. 在 E5 语义候选之后，加一个本地 cross-encoder，直接给“问题＋原文”评分。CPU 推理，不调用模型 API。
2. 对明确要求的金额、百分比、时长、哈希、容量、批准人和年份，检查原文是否有相应片段，保存精确引用位置。没有具体数值的方案说明不能冒充实测结果。
3. 提供严格与保留两种实验策略。严格策略最多一条；保留策略保留旧检索中通过有限字段检查的最多两条候选，旧结果为空时才用重排补充。这样避免新模型低分把旧检索的强项直接抹掉，但也可能保留旧误报。

**字段存在和相关性高，都不等于资料足以回答。** `select_verified` 是实验函数名，不是事实已被证实的保证；无识别字段时明确标记 `relevance_only_unverified`。

## 40 道固定题的结果

其中 20 道有答案、20 道无答案或需要澄清。旧混合方案、严格方案、保留方案在同一 24 条合成语料上比较：

- 正确资料排第一：旧方案 6/20；严格方案 11/20；保留方案 12/20。
- 前两条包含正确资料：旧方案 8/20；严格方案 11/20；保留方案 14/20。
- 无答案仍返回候选：旧方案 4/20；严格方案 0/20；保留方案 2/20。
- 保留方案中文 6/10（旧方案 6/10），英文 6/10（旧方案 0/10）。
- 三组回归中，没有丢掉旧方案原来排第一或前两条中已有的正确来源。这是本批样本的事实，不是对未来问题的保证。

“无答案返回候选”不等于模型已经编造最终回答。严格方案本批没有此类返回，但仍漏掉 9/20 道有答案题，不能称作 100% 准确。

仍然会错：问 Excel 许可证价格，保留了 OCR 月预算；问支付成功率，保留了请求重试率。程序查到了金额或比例，却没可靠判断它属于哪个对象、哪个指标。另有两道有答案题的正确来源仍排第二，第一条不对。

## 数据与迭代边界

- 开发集 66 题，包含已看过的旧题；阈值只在该开发集选择，为 0.4、分差 0.2。
- 第一轮新 60 题暴露中文退化和 price-cache 被误判为金额的问题，保留原始报告，随后它成为已知回归集。
- 再冻结新的 40 题后运行 V2.1。之后发现输出只保留第一候选、错误截掉旧第二来源的实现问题，修成最多两条，再运行同一批题。这个修复没有调整分数、阈值、标签；最终报告仍应当称为固定回归结果，不能声称最终代码从未接触过这些测试题。
- 旧 60 题和更早 52 题另列已知回归，不合并成独立盲测题数。题目均为开发者编写的合成案例，尚无独立真实用户评测。
- 语料用不相关的 fixture ID 作为 hitwords，刻意观察正文改写与跨语言能力，不代表日常人工整理命中词后的总体效果。

结果在 `results_accuracy/verified/result-v21.json`；逐题原始分数、字段原文位置、模型版本与边界均保留。初次 60 题和修复前 40 题结果另存，不能用当前修复后的代码冒充逐字复现历史缺陷版本。

## 成本和范围

先取最多 8 个语义候选，并补入词法候选，去重后最多 10 个进行本地重排。**内部计算比 V1 更多**，最终输出仍受 600 正文计量预算限制。严格最多一条、保留最多两条；范围、ACTIVE、归档及替代过滤沿用原边界。

本机该次测量：40 道题的观察与评分约 6.94 秒、320 对本地评分，reranker 装载约 0.99 秒。这不是生产服务延迟或不同机器的承诺。新增权重与 tokenizer 约 136 MB，保存在仓库外。

13 项不同单元检查通过；保存结果的 152 道决策重放一致，并核对 49 处精确原文引用。该重放不是 152 道新测试，也没有再次调用模型。评测进程拦截 Python socket 连接，未禁用系统网络。

## 复现

在仓库根目录执行 PowerShell。沿用 V1 README 的隔离 Python 环境和 encoder 下载步骤，再下载固定修订的 reranker。不要使用真实记忆目录。

```powershell
$semanticHome = Join-Path $env:LOCALAPPDATA 'HFA-Semantic-Prototype'
$semanticPython = "$semanticHome\env\Scripts\python.exe"
& $semanticPython experiments/local_semantic/download_reranker.py "$semanticHome\reranker"
& $semanticPython -B experiments/local_semantic/evaluate_v21.py --baseline . --fixture experiments/local_semantic/fixture_v21 --encoder "$semanticHome\model" --reranker "$semanticHome\reranker" --output "$semanticHome\accuracy-results" --v1-calibration experiments/local_semantic/results/calibration.json --legacy-fixture experiments/local_semantic/fixture_v2 --old-fixture experiments/local_semantic/fixture
& $semanticPython -B -m unittest discover -s experiments/local_semantic -p 'test_*.py' -v
& $semanticPython -B experiments/local_semantic/replay_v21.py "$semanticHome\accuracy-results\result-v21.json"
```

评测永远新建自己的合成临时记忆；`--baseline` 是代码目录。更换基线版本可能改变结果，应核对报告的基线信息。当前评测基线 manager SHA-256 为 `4c2d75b5fe8c6b8b72e81f128f6e6031ef13092f5b3f61f4a47928963efbde2f`。

## 尚未解决

对象/指标与数值的可靠对应、无限种隐含含义、真实宿主端到端行为、向量生命周期同步和真实数据规模性能均未验收。不默认启用。下一步应先用独立写的新题评价“对象＋指标＋时间＋数值”的绑定，避免继续针对已看过的错题堆特例。

模型来源：[官方固定模型](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1)；方法背景：[Sentence Transformers retrieve & rerank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)。相关性重排本身不提供事实蕴含保证。


## 显式事实绑定实验

新增“对象、指标、时间与数值必须在同一明确分句”的可选后置过滤；用 `--bind-claims` 运行。详见 [绑定检查说明](README-binding.md)。这是有限线索核查，日常 HFA 尚未启用；旧结果保留为历史对照。
