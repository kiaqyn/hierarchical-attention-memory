# 对象、指标、时间与数值绑定实验

状态：**仅本地实验，未启用到日常 HFA。** 原始 L1、真实记忆、Hook/MCP 和全局配置均不改动。

## 改善了什么

过去只检查“原文里有没有金额/百分比”。现在增加一个候选后置过滤阶段，检查所问对象、指标和时间是否与数值出现在同一明确分句：

- 问 Atlas 成功率，Atlas 重试率不能顶替。
- 问 Vega 预算，Nova 的预算不能顶替。
- 问 2026 年 3 月的数据，不能拿 2025 年或 4 月的数据拼接。
- 月预算不能拿年预算代替；计划值、否定值、没有记录的数值不能冒充实测证据。
- 同一条记忆有多个数字时，保存实际匹配分句的数字和精确原文偏移。`从60秒改为300秒` 引用新值，不引用旧值。

`claim_binding.py` 提供 `binding_support()` 和 `filter_bound_candidates()`。过滤器接收此前已经过项目范围、ACTIVE、替代关系和预算过滤的最多两条候选，**只删除，不新增或重排**，不会扩大权限或上下文预算。

`evaluate_v21.py --bind-claims` 已接入该阶段；不带参数仍运行旧 V2.1 策略。新策略在报告中叫 `v3_bound`，其前序检索分数和开发集标定不变。原阈值仍为 0.4 / 分差 0.2。

## 本次证据

新增 40 个“问题＋候选原文”配对，编写并冻结后再实现检查器：20 个应通过，20 个应拒绝。旧字段检查放过 19/20 个错误配对；新检查器最终拒绝这 20 个，保留全部 20 个正确配对，并引用了预期数值。

这是开发者编写的规则功能测试，不是新的端到端检索成功率。编写者事先知道题目；查看首轮结果后修复了英文句首词、过去式问法、缓存期限等解析缺陷，因此最终结果也不称独立盲测。题目、标签和阈值未修改；首轮结果保留在 `results_binding/first-run.json`。

对既有候选回放：

- 固定 40 题：正确第一来源保持 12/20，前两条包含正确来源保持 14/20；另 20 道无答案题的候选误报 2→0。
- 已知 60 题：正确来源保持 20/30，无答案候选误报仍为 2/30，未解决。
- 更早已知 52 题：正确来源保持 22/36，无答案候选误报 2→0/16。
- 三组都没有丢掉原来正确的候选。固定 40 题仍有两道正确来源排第二，且六道有答案题没有找到答案；不能声称全部问题已修好。

这些数量不相加为独立样本。候选误报不等于模型已编造最终答案；本实验不生成最终回答。新增 22 项检查，加上实验原有 13 项，共 35 项不同单元检查通过。

## 使用与复现

在仓库根目录，沿用 [语义原型](README.md) 的隔离环境和模型下载步骤。

```powershell
$semanticHome = Join-Path $env:LOCALAPPDATA 'HFA-Semantic-Prototype'
$semanticPython = "$semanticHome\env\Scripts\python.exe"
# 不重新调用模型：检查固定配对，并对保存的已知候选进行后置过滤回放。
& $semanticPython -B experiments/local_semantic/evaluate_binding.py --fixture experiments/local_semantic/fixture_binding --cached-report experiments/local_semantic/results_accuracy/verified/result-v21.json --output "$semanticHome\binding-replay"
# 实际重新运行本地 encoder/reranker，并在同一次检索中应用后置检查。
& $semanticPython -B experiments/local_semantic/evaluate_v21.py --baseline . --fixture experiments/local_semantic/fixture_v21 --encoder "$semanticHome\model" --reranker "$semanticHome\reranker" --output "$semanticHome\binding-inference" --v1-calibration experiments/local_semantic/results/calibration.json --legacy-fixture experiments/local_semantic/fixture_v2 --old-fixture experiments/local_semantic/fixture --bind-claims
& $semanticPython -B -m unittest discover -s experiments/local_semantic -p 'test_*.py' -v
```

本地模型重新评分不需要付费 API。评测仅拦截自身 Python socket 连接，系统网络不变。评测代码始终新建合成临时记忆，不接收真实记忆目录参数。

## 必须知道的边界

- 只识别有限显式线索：带引号的名称、部分英文专名、有限中英指标词、明确年月与周期。不是通用实体识别或语义蕴含模型。
- 不会自动推断 Mercury＝水星，也不能可靠识别所有中文普通名词对象。没有可识别指标时，不能靠这层保证“字体修复省了多少钱”不会误用别的预算；已知 60 题中此类误报仍在。
- 跨句省略、代词、隐含含义可能被保守拒绝；未做更广泛真实语言召回验收。没有证据支撑“任何表达都不误伤”。
- 非数值问题沿用原检索，明确标记 `not_checked_non_numeric_request`，不能算通过了对象/时间核验。
- `allowed=True` 只表示这个有限检查器没有拒绝，不等于资料足够回答。未知单位、汇率、统计口径、分母、复杂时间区间与互相矛盾的记录仍可能出错。
- “去年/今年”要求调用者传入明确 `datetime.date` 参考日期；缺失时拒绝建立绑定，不静默套用运行日期。
- 没有提高模型能力、没有增加查询次数、没有完成真实宿主接入或向量生命周期验收。仍不建议直接默认启用。

查看 `results_binding/final.json` 的逐题原因、精确原文跨度和所有未解决项；`results_binding/integrated.json` 为本地模型实际重跑并接入过滤器的结果。
