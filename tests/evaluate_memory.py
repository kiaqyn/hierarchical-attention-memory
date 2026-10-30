"""Small synthetic retrieval comparison and 1000-operation durability replay.

Not an LLM benchmark, human study, or proof of semantic understanding. Corpus and
queries are fixed and visible. All writes occur in an isolated temporary memory.
"""
import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

from validate_hfa import hfa, invoke

CORPUS = [
    ("报表导出", "乱码, CSV, export-case", "中文乱码已通过 UTF-8 修复，CSV 导出必须指定编码。"),
    ("生产发布", "部署, release-case", "部署服务先运行测试，再更新容器。"),
    ("缓存有效期", "Redis, cache, redis-ttl", "Redis cache expiry is 60 seconds; invalidate cache after updates."),
    ("接口等待", "超时, api-case", "请求超时后最多重试两次，使用指数退避。"),
    ("文件保护", "备份, 数据库, file-case", "备份数据库，每天保留一个副本。"),
    ("多人编辑", "并发, edit-case", "并发更新需要文件锁，避免记录丢失。"),
    ("发行失败", "回滚, undo-case", "出现故障就回滚，恢复上一版本。"),
    ("图片导出", "PNG, image-case", "导出 PNG 时保留透明背景。"),
    ("构建设置", "Windows, build-case", "Windows 构建需要 Node 20，运行 npm ci。"),
    ("界面适配", "小屏幕, layout-case", "小屏幕导航使用折叠菜单，按钮最小高度 44px。"),
    ("付款请求", "支付, payment-case", "支付接口使用幂等键，避免重复扣款。"),
    ("错误追踪", "日志, logs-case", "错误日志记录请求编号，不保存密码。"),
]
QUERIES = [
    ("export-case", 0), ("中文变问号", 0), ("garbled text", 0), ("CSV 导出编码", 0),
    ("上线步骤", 1), ("deployment", 1), ("redis cache expiry", 2), ("cache invalidate", 2),
    ("timeout", 3), ("请求等太久", 3), ("backup", 4), ("数据库副本", 4),
    ("同时写入", 5), ("concurrency", 5), ("撤销更改", 6), ("rollback", 6),
    ("PNG 透明背景", 7), ("Windows Node 构建", 8), ("小屏幕折叠菜单", 9),
    ("重复扣款幂等键", 10), ("请求编号错误日志", 11),
    ("今天晚饭吃什么", None), ("继续处理之前那个问题", None),
    ("不要查乱码和部署", None), ("do not retrieve backup", None),
    ("provider authentication", None), ("火星大气成分", None),
    ("what did we decide before", None), ("weather in London", None),
    ("backup singer audition", None),
    ("输出文件全是看不懂的符号", 0), ("撤回这次更新恢复到昨天那版", 6),
]


def evaluate():
    with tempfile.TemporaryDirectory(prefix="hfa-quality-") as temp:
        memory = Path(temp) / "memory"
        invoke(hfa.cmd_init, memory)
        for title, word, body in CORPUS:
            invoke(hfa.cmd_add_topic, memory, title, word, body)
        report = {"kind": "synthetic, developer-authored cases; no model calls", "corpus_size": len(CORPUS), "modes": {}}
        for mode in ("keyword", "hybrid"):
            cases = []
            for query, expected in QUERIES:
                _, output = invoke(hfa.cmd_route, memory, query, None, None, mode)
                result = json.loads(output)
                ids = [c["id"] for c in result["selected"]]
                expected_id = f"chunk_{expected + 2:04d}" if expected is not None else None
                cases.append({"query": query, "expected": expected_id, "actual": ids,
                              "pass": expected_id in ids if expected_id else not ids,
                              "budget_ok": result["recall_tokens"]["count"] <= result["recall_token_budget"]})
            positive = [c for c in cases if c["expected"]]
            negative = [c for c in cases if not c["expected"]]
            report["modes"][mode] = {"hits": sum(c["pass"] for c in positive), "positive_cases": len(positive),
                "false_hits": sum(not c["pass"] for c in negative), "negative_cases": len(negative),
                "extra_results": sum(len([x for x in c["actual"] if x != c["expected"]]) for c in positive),
                "budget_ok": all(c["budget_ok"] for c in cases), "cases": cases}
        return report


def replay(turns=1000):
    with tempfile.TemporaryDirectory(prefix="hfa-replay-") as temp:
        memory = Path(temp) / "memory"
        invoke(hfa.cmd_init, memory)
        files, current, latencies = {}, None, []
        writes = reads = retries = updates = 0
        started = time.monotonic()
        for turn in range(turns):
            tick = time.monotonic()
            if turn % 10 == 0:
                source = f"synthetic:turn-{turn}"
                supersedes = [current] if current else []
                args = (memory, "当前发布决定", "release-rule", f"经过确认的当前版本是 v{turn}。", None, None,
                        source, supersedes, f"turn-{turn}", True)
                _, output = invoke(hfa.cmd_add_topic, *args)
                saved = json.loads(output)
                current = saved["id"]
                files[saved["path"]] = hashlib.sha256((memory / saved["path"]).read_bytes()).hexdigest()
                writes += 1
                updates += bool(supersedes)
            elif turn % 10 == 1:
                _, output = invoke(hfa.cmd_add_topic, *args)
                assert json.loads(output)["duplicate"]
                retries += 1
            else:
                _, output = invoke(hfa.cmd_route, memory, "release-rule")
                result = json.loads(output)
                assert [c["id"] for c in result["selected"]] == [current]
                assert result["recall_tokens"]["count"] <= result["recall_token_budget"]
                reads += 1
            latencies.append((time.monotonic() - tick) * 1000)
        assert all(hashlib.sha256((memory / p).read_bytes()).hexdigest() == digest for p, digest in files.items())
        rc, state = invoke(hfa.cmd_status, memory, True)
        assert rc == 0
        return {"operations": turns, "writes": writes, "retries": retries, "reads": reads, "explicit_updates": updates,
                "all_original_bytes_preserved": True, "current_decision_correct_on_all_reads": True,
                "active_epoch": json.loads(state)["active_epoch"], "total_topics": json.loads(state)["total_topics"],
                "elapsed_seconds": round(time.monotonic()-started, 3),
                "latency_ms_median": round(statistics.median(latencies), 3),
                "latency_ms_p95": round(sorted(latencies)[int(len(latencies)*.95)-1], 3),
                "scope": "1000 simulated tool operations, not 1000 real conversation turns"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {"retrieval": evaluate(), "replay": replay()}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
