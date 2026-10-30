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

        uncreated = self.workspace / "future_memory"
        proc = subprocess.run(
            [sys.executable, "-B", str(HOOK), "--print-hooks", "--memory-dir", str(uncreated)],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        printed = json.loads(proc.stdout)
        cmd = printed["hfa-auto-recall"]["PreInvocation"][0]["command"]
        self.assertIn(str(uncreated.resolve()), cmd)

    def test_unmatched_query_emits_empty_json_with_zero_tokens(self):
        payload = json.dumps({
            "conversationId": "conv-1",
            "workspacePaths": [str(self.workspace)],
            "lastUserInput": "今天晚饭吃什么",
        })
        proc = self.run_hook(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {})

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
