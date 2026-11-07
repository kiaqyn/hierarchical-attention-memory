# HFA 3：开始使用

HFA 把可复用的项目决定保存在本地，并限制送进模型的记忆正文。它不保证记住所有聊天，也不是语义理解。没找到只表示这次没找到。

## 三步就够

Python 3.10+。在项目目录：

```powershell
python -m pip install .
hfa init ./my_memory
hfa add-topic ./my_memory --title "发布规则" --hit-words "发布规则" --conclusion "发布前先确认。"
hfa recall ./my_memory --query "发布规则"
hfa doctor ./my_memory
```

`hfa init` 只建立一条主线、一份核心摘要和空的记录目录。保存和查找不需要先学纪元、分支锁或墓碑。记录多了会自动换纪元。核心摘要仍会在查找结果里出现，所以未命中不是零 Token。

`doctor` 只报告目录和依赖是否就绪，不打印记忆正文。`status --json` 同样默认不带 `summary.md`；要看摘要正文用 `status --json --include-text`。本地就绪不表示宿主已经接入，也不表示模型用了这些文字。

## 接到 AI 软件

MCP 需要 Python 3.11+。下面两条只打印配置，不改现有设置：

```powershell
python -m pip install ".[all]"
hfa-mcp --memory-dir ./my_memory --compact --print-config
hfa-hook --memory-dir ./my_memory --print-hooks
```

把条目合并进宿主后重载。先用上面的合成目录试查、存、以及“这轮不要保存”，再接真实项目。

普通助手回复默认不自动入库。宿主要在该轮传入 `memoryWriteAllowed: true`，或者你在 `config.json` 里把 `write_policy` 改成 `auto` 后再 `reindex`。`auto` 仍会拒绝“不要保存”、`memoryWriteAllowed: false`、闲聊和未验证文本。它不能证明结论属实。`explicit` 则只接受结构化决定。不改配置时行为与以前相同。

## 遇到问题

- 索引过期：先备份，`hfa reindex ./my_memory`，再 `hfa doctor`。
- 改了 `config.json` 之后也要 `reindex`，配置属于真源指纹。
- 找不到决定：可以换一次说法。没找到不等于不存在。
- 默认最多返回 2 条。支线需要明确的解锁理由，模型不能自己扩大范围。

## 进阶，默认用不到

`hfa init --example` 会放上教学用的分支、墓碑和示例记录，不要把它们当成你的项目事实。

分支锁定、墓碑、替代链、回合次数和可选本地向量都还在。查找的热路径用文件大小和 mtime 跳过重复读盘；大小和 mtime 都被原样恢复的篡改要靠 `recall --full-verify` 或 `reindex --verify` 才会发现。新目录默认保留最近 200 个会话和 2000 条访问记录，带解锁理由的访问默认不删。旧目录没有这两项配置时仍不裁剪。

教学页 `visualizer/index.html` 不连接磁盘。

详见 [AI 接入](ai-integration.md)、[分方向补查](query-refinement.md) 和 [可选语义后端](local-semantic.md)。
