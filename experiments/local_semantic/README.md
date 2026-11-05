# 可选本地语义检索原型

状态：**仅实验，未接入日常 Hook、MCP 或真实记忆。** 核心 HFA 仍无此模型依赖。

使用官方 multilingual-e5-small 的量化 ONNX 权重，在本机 CPU 生成向量；保留词法候选，使用 RRF 合并。相似度和第一、二名分差可以要求弃答。结果只表示检索候选，不生成最终回答，不证明资料足够回答。

## 本次实验结果

2026-11-02，先冻结 21 条合成记录、30 条开发题、52 条测试题，再只用开发集选择阈值。额外包含 HFA 初始化记录；支线、归档、被替代的记录在生成向量前排除。

- 有答案的 36 条测试题：当前词法检索找到 4 条；不设门槛的语义检索找到 33 条；保守混合原型找到 12 条。
- 无答案或需澄清的 16 条题：三者分别对 3、13、3 条返回候选。返回候选不等于模型已经编造答案，但存在证据不足风险。
- 保守混合新增找回 8 条，没有丢掉词法原本找对的题；没有新增无答案返回的题。
- 中文题从 4/18 到 12/18；英文题从 0/18 到 0/18。开发题主要为中文，阈值未泛化到英文，不能声称已解决跨语言改写。
- 仍有 3 个问题仅获得相关资料，例如问“支付接口上月实际重复扣款率”，只返回幂等方案，不能据此回答扣款率。
- 阈值为开发集选出的约 0.875，分差为 0；未按测试集结果继续调参。开发者合成数据不是独立外部盲测，也不是实际用户成功率。

本实验刻意使用换说法与跨语言问题；命中词只设置为不相关的 fixture ID，用来隔离正文召回能力。它不能代表日常人工整理了命中词后的总体命中率。

完整分题结果在 `results/result.json`。原始分数、模型修订号、文件校验值、数据集哈希、CPU 测量均可复查。5 项边界/数值/融合单元检查通过，实际模型推理也已运行。

## 运行方式

在仓库根目录执行 PowerShell。依赖与权重放到仓库外，避免误提交模型文件：

```powershell
$semanticHome = Join-Path $env:LOCALAPPDATA 'HFA-Semantic-Prototype'
python -m venv "$semanticHome\env"
$semanticPython = "$semanticHome\env\Scripts\python.exe"
& $semanticPython -m pip install --only-binary=:all: -r experiments/local_semantic/requirements.lock.txt
& $semanticPython experiments/local_semantic/download_model.py "$semanticHome\model"
& $semanticPython -B experiments/local_semantic/evaluate.py --baseline . --fixture experiments/local_semantic/fixture --model "$semanticHome\model" --output "$semanticHome\results"
& $semanticPython -B -m unittest discover -s experiments/local_semantic -p test_local_semantic.py -v
```

下载脚本只获取公开模型文件，并记录固定修订和 SHA-256。首次下载需要联网；评测载入本地文件，Python 网络连接在评测进程内被拦截，系统网络和安全设置未改变。没有推理 API、账号凭据或付费调用。

`evaluate.py` 不接收真实记忆目录参数；它始终在任务独有临时目录建立合成记忆。调用当前 HFA 代码作为基线，报告记录基线源码哈希，因此将来换版本重测时要区分版本差异。本次固定数据已有 SHA-256；`make_fixture.py` 拒绝覆盖内容不同的冻结题目。

## 范围与未完成部分

- 同范围、ACTIVE、未被替代的记录才参与语义索引。每种方案同样限 2 条、600 正文计量预算；没有扩大支线权限。
- 只实现离线候选检索对照。尚无真实宿主接入、向量缓存事务、新增/修改/删除时同步、模型升级迁移或 reranker。
- 没有实现“资料足够回答吗”的可靠核验。相似度不是答案正确率；高分可能只有同主题、缺少具体事实。
- 开发集与测试集仍是同一开发者编写。下一轮应使用新的跨语言开发集和独立测试题，继续同时观察漏检与无答案候选；不应回头调整本次测试标签来美化结果。
- 原型仅保存在实验目录；没有改写日常 HFA、全局配置或真实记忆。**目前不建议默认开启。**

模型与实现依据：

- https://huggingface.co/intfloat/multilingual-e5-small
- https://arxiv.org/abs/2402.05672
- https://qdrant.tech/documentation/search-tuning/hybrid-search/


## 准确度改进实验 V2.1

以上为 V1 历史结果。新增本地重排、有限字段核查及中英回归，见 [准确度实验说明](README-accuracy.md)。仍未接入日常 HFA。


## 显式事实绑定实验

新增“对象、指标、时间与数值必须在同一明确分句”的可选后置过滤；用 `--bind-claims` 运行。详见 [绑定检查说明](README-binding.md)。这是有限线索核查，日常 HFA 尚未启用；旧结果保留为历史对照。
