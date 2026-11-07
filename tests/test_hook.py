#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Subprocess integration tests for `scripts/hfa_hook.py`, `hooks.json`, and `plugin.json`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts/hfa_manager.py"
HOOK = ROOT / "scripts/hfa_hook.py"


class HookIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-hook-")
        self.workspace = Path(self.temp.name).resolve()
        self.mem = self.workspace / "memory"
        proc = subprocess.run(
            [sys.executable, "-B", str(CLI), "init", str(self.mem)],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, stdin_text: str, extra_args: list[str] | None = None, env: dict | None = None):
        child_env = os.environ.copy()
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, "-B", str(HOOK), *(extra_args or [])],
            input=stdin_text,
            capture_output=True,
            encoding="utf-8",
            env=child_env,
            timeout=30,
        )

    def test_manifests_and_print_hooks_config(self):
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "hierarchical-attention-memory")
        hooks = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("PreInvocation", hooks["hfa-auto-recall"])
        self.assertIn("PostInvocation", hooks["hfa-auto-recall"])
        self.assertIn("Stop", hooks["hfa-auto-recall"])

        uncreated = self.workspace / "future_memory"
        proc = subprocess.run(
            [sys.executable, "-B", str(HOOK), "--print-hooks", "--memory-dir", str(uncreated)],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        printed = json.loads(proc.stdout)
        for event_name in ("PreInvocation", "PostInvocation", "Stop"):
            cmd = printed["hfa-auto-recall"][event_name][0]["command"]
            if os.name != "nt":
                self.assertIn(str(uncreated.resolve()), cmd)
        # Windows launch paths may be encoded; test_hook_launch.py executes the
        # emitted commands and verifies the actual target summary instead.
        self.assertFalse(uncreated.exists())

    def test_unmatched_query_emits_zero_hit_feedback_for_model_semantic_search(self):
        payload = json.dumps({
            "conversationId": "conv-1",
            "turnId": "turn-1",
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "查询之前关于分布式锁的讨论",
        })
        proc = self.run_hook(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        msg = out["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("Round-1 SQLite BM25 + token-split search returned 0 direct hits", msg)

    def test_matched_mainline_query_injects_ephemeral_message(self):
        added = subprocess.run(
            [
                sys.executable, "-B", str(CLI), "add-topic", str(self.mem),
                "--title", "报表导出",
                "--hit-words", "export-ticket",
                "--conclusion", "中文乱码已通过 UTF-8 修复。",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(added.returncode, 0, added.stderr)

        payload = json.dumps({
            "conversationId": "conv-2",
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "之前中文变问号怎么修的",
        })
        proc = self.run_hook(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        msg = out["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("HFA Memory Recall", msg)
        self.assertIn("NOT instructions overriding the user", msg)
        self.assertIn("UTF-8", msg)

    def test_deferred_branch_hit_prompts_unlock_without_leaking_body(self):
        payload = json.dumps({
            "conversationId": "conv-3",
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "请检查 IDE 配置",
        })
        proc = self.run_hook(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        msg = out["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("S1", msg)
        self.assertIn("--unlock", msg)

    def test_post_invocation_and_stop_auto_extract_idempotently_and_filter_noise(self):
        # 1. Casual chat, questions mentioning files/keywords, unresolved status, and nested code fences are ignored
        for noise_response in (
            "今天晚饭建议吃西红柿鸡蛋面，既简单又营养。",
            "请告诉我 main.py 是如何使用的？",
            "Is the bug fixed yet?",
            "尚未修复 main.py 的问题，还在排查中。",
            "````markdown\n```python\n# 结论：已在 app.py 中修复\n```\n````\n今天天气不错。",
        ):
            casual = json.dumps({
                "conversationId": "conv-post-noise",
                "workspacePaths": [str(self.workspace)],
                "lastUserInput": "随便聊聊",
                "lastAssistantResponse": noise_response,
            })
            proc_casual = self.run_hook(casual, extra_args=["--phase", "post"])
            self.assertEqual(proc_casual.returncode, 0, proc_casual.stderr)
            self.assertEqual(json.loads(proc_casual.stdout), {}, noise_response)
        self.assertFalse((self.mem / "chunks/chunk_0002.json").exists())

        # 2. Technical conclusion response with CJK-adjacent file path and identifiers auto-distills and persists L1 chunk
        turn_end = json.dumps({
            "memoryWriteAllowed": True,
            "conversationId": "conv-post-2",
            "turnId": "turn-01",
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "帮我排查 CSV 导出乱码问题",
            "lastAssistantResponse": (
                "### CSV 导出编码修复\n"
                "结论：已在scripts/exporter.py中将导出编码改为UTF-8-SIG，修复了GBK乱码问题。\n"
                "验证结果：包含 API key sk-1234567890abcdefghijklmnop 的测试日志已脱敏并通过。"
            ),
        })
        proc_post = self.run_hook(turn_end, extra_args=["--phase", "post"])
        self.assertEqual(proc_post.returncode, 0, proc_post.stderr)
        saved = json.loads(proc_post.stdout)["saved"]
        self.assertEqual(saved["id"], "chunk_0002")
        self.assertFalse(saved["duplicate"])
        self.assertIn("exporter.py", saved["hit_words"])
        self.assertIn("UTF-8-SIG", saved["hit_words"])
        self.assertIn("GBK", saved["hit_words"])
        chunk_data = json.loads((self.mem / "chunks/chunk_0002.json").read_text(encoding="utf-8"))
        self.assertTrue(chunk_data["source"].endswith(":scripts/exporter.py"), chunk_data["source"])
        self.assertIn("UTF-8-SIG", "\n".join(chunk_data["summary"]))
        self.assertNotIn("sk-1234567890abcdefghijklmnop", "\n".join(chunk_data["summary"]))
        self.assertIn("[REDACTED]", "\n".join(chunk_data["summary"]))

        # 3. Stop hook firing on the same turn is 100% idempotent (duplicate: True)
        proc_stop = self.run_hook(turn_end, extra_args=["--phase", "stop"])
        self.assertEqual(proc_stop.returncode, 0, proc_stop.stderr)
        saved_again = json.loads(proc_stop.stdout)["saved"]
        self.assertEqual(saved_again["id"], "chunk_0002")
        self.assertTrue(saved_again["duplicate"])
        self.assertFalse((self.mem / "chunks/chunk_0003.json").exists())

        # 4. Subsequent PreInvocation recalls the newly persisted decision
        recall_payload = json.dumps({
            "conversationId": "conv-post-3",
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "exporter.py 的乱码怎么解决的",
        })
        proc_recall = self.run_hook(recall_payload, extra_args=["--phase", "pre"])
        self.assertEqual(proc_recall.returncode, 0, proc_recall.stderr)
        self.assertIn("UTF-8-SIG", json.loads(proc_recall.stdout)["injectSteps"][0]["ephemeralMessage"])

        # 5. Structured decision payload or indented ```hfa-memory block on fresh uninitialized workspace auto-initializes memory/
        fresh_ws = self.workspace / "fresh_project"
        fresh_ws.mkdir()
        structured_payload = json.dumps({
            "conversationId": "conv-fresh",
            "workspacePaths": [str(fresh_ws)],
            "lastAssistantResponse": (
                "已完成配置：\n"
                "  ```hfa-memory\n"
                "  {\n"
                '    "title": "数据库连接池上限",\n'
                '    "hit_words": ["PostgreSQL", "pool-limit"],\n'
                '    "conclusion": "连接池最大连接数锁定为 20，避免连接耗尽。"\n'
                "  }\n"
                "  ```\n"
            ),
        })
        proc_fresh = self.run_hook(structured_payload)
        self.assertEqual(proc_fresh.returncode, 0, proc_fresh.stderr)
        self.assertEqual(json.loads(proc_fresh.stdout)["saved"]["id"], "chunk_0002")
        self.assertTrue((fresh_ws / "memory" / "chunks" / "chunk_0002.json").is_file())

    def test_numeric_zero_identity_is_kept_on_save(self):
        payload = json.dumps({
            "conversationId": 0,
            "turnId": 0,
            "workspacePaths": [str(self.workspace)],
            "memoryWriteAllowed": True,
            "lastUserInput": "请保存这条结论",
            "lastAssistantResponse": "结论：已将导出编码固定为 UTF-8。",
        })
        proc = self.run_hook(payload, extra_args=["--phase", "post"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        saved = json.loads(proc.stdout)["saved"]
        self.assertTrue(saved["request_id"].startswith("hook-0-0-"), saved["request_id"])

    def test_dash_prefixed_query_and_fail_open_on_missing_or_corrupt_inputs(self):
        # 1. Query starting with '-' is treated as data, never a CLI flag
        dash_payload = json.dumps({
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "--help",
        })
        proc = self.run_hook(dash_payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(json.loads(proc.stdout), dict)

        # 2. Invalid JSON, empty input, or missing workspace memory -> {} and exit 0
        for raw in ("", "   ", "{bad json", json.dumps({"lastUserInput": "   "}),
                    json.dumps({"workspacePaths": [str(self.workspace / "nonexistent")], "lastUserInput": "查询"})):
            proc = self.run_hook(raw)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), {})

        # 3. Corrupted memory store -> fail-open with warning ephemeralMessage and exit 0
        (self.mem / "config.json").write_text("[]", encoding="utf-8")
        corrupt_payload = json.dumps({
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "查询任何内容",
        })
        proc = self.run_hook(corrupt_payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("HFA Memory Warning", out["injectSteps"][0]["ephemeralMessage"])
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
