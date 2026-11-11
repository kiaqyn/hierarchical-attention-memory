"""Architecture contracts: write policy, stat cache, retention, minimal init."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from validate_hfa import ROOT, hfa, invoke, snapshot

HOOK = ROOT / "scripts/hfa_hook.py"
CLI = ROOT / "scripts/hfa_manager.py"


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hfa-arch-")
        self.mem = Path(self.tmp.name) / "memory"
        invoke(hfa.cmd_init, self.mem)

    def tearDown(self):
        self.tmp.cleanup()

    def test_minimal_init_recall_and_save_need_no_branch_or_tombstone(self):
        target = Path(self.tmp.name) / "plain"
        proc = subprocess.run([sys.executable, "-B", str(CLI), "init", str(target)],
                              capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((target / "branches").exists())
        self.assertFalse((target / "tombstones.jsonl").exists())
        self.assertTrue((target / "summary.md").is_file())
        added = subprocess.run([sys.executable, "-B", str(CLI), "add-topic", str(target),
                                "--title", "发布规则", "--hit-words", "release-rule",
                                "--conclusion", "发布前先确认。", "--json"],
                               capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(json.loads(added.stdout)["id"], "chunk_0001")
        recalled = subprocess.run([sys.executable, "-B", str(CLI), "recall", str(target),
                                   "--query", "release-rule"],
                                  capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(recalled.returncode, 0, recalled.stderr)
        body = json.loads(recalled.stdout)
        self.assertIn("发布前先确认", body["recall_text"])
        self.assertIn("Core project summary", body["context_text"])
        status = subprocess.run([sys.executable, "-B", str(CLI), "status", str(target), "--json"],
                                capture_output=True, encoding="utf-8", timeout=30)
        report = json.loads(status.stdout)
        self.assertNotIn("summary_text", report)
        self.assertEqual(report["problems"], [])
        doctor = subprocess.run([sys.executable, "-B", str(CLI), "doctor", str(target), "--json"],
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertNotIn("待记录", doctor.stdout)

    def test_fast_recall_matches_full_verify_and_mtime_swap_is_not_a_guarantee(self):
        invoke(hfa.cmd_add_topic, self.mem, "Cache fact", "cache-fact", "CACHE_FACT_ORIGINAL")
        fast = hfa.cmd_route(self.mem, "cache-fact", return_result=True)
        full = hfa.cmd_route(self.mem, "cache-fact", return_result=True, full_verify=True)
        self.assertEqual([item["id"] for item in fast["selected"]], [item["id"] for item in full["selected"]])
        self.assertIn("CACHE_FACT_ORIGINAL", fast["recall_text"])
        path = self.mem / "chunks/chunk_0002.json"
        stat = path.stat()
        text = path.read_text(encoding="utf-8")
        self.assertIn("CACHE_FACT_ORIGINAL", text)
        swapped = text.replace("CACHE_FACT_ORIGINAL", "CACHE_FACT_SWAPPED!")
        self.assertEqual(len(swapped.encode("utf-8")), len(text.encode("utf-8")))
        path.write_text(swapped, encoding="utf-8", newline="\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        trusted = hfa.cmd_route(self.mem, "cache-fact", return_result=True)
        self.assertIn("CACHE_FACT_ORIGINAL", trusted["recall_text"])
        self.assertNotIn("CACHE_FACT_SWAPPED", trusted["recall_text"])
        with self.assertRaises(hfa.MemoryError):
            hfa.cmd_route(self.mem, "cache-fact", return_result=True, full_verify=True)

    def test_ordinary_edit_is_still_stale_without_restoring_mtime(self):
        invoke(hfa.cmd_add_topic, self.mem, "Cache fact", "cache-fact", "CACHE_FACT_ORIGINAL")
        path = self.mem / "chunks/chunk_0002.json"
        path.write_text(path.read_text(encoding="utf-8").replace("ORIGINAL", "EDITED"), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            hfa.cmd_route(self.mem, "cache-fact", return_result=True)

    def test_retention_keeps_unlock_audit_and_rolls_back_with_the_write(self):
        path = self.mem / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["session_retention"] = {"max_conversations": 2}
        config["access_retention"] = {"max_events": 2, "keep_unlocks": True}
        path.write_text(json.dumps(config), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        for name in ("c0", "c1", "c2"):
            hfa.cmd_route(self.mem, "HFA初始化", conversation_id=name, turn_id="t", return_result=True)
        hfa.cmd_route(self.mem, "IDE", unlock="keep this audit", conversation_id="audit",
                      turn_id="t", return_result=True)
        hfa.cmd_route(self.mem, "HFA初始化", conversation_id="c3", turn_id="t", return_result=True)
        ledger = json.loads((self.mem / "history/session-state.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(ledger["conversations"]), 2)
        lines = [json.loads(line) for line in (self.mem / "history/access.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(line.get("unlock_reason") == "keep this audit" for line in lines))
        ordinary = [line for line in lines if not line.get("unlock_reason")]
        self.assertLessEqual(len(ordinary), 2)
        before = snapshot(self.mem)
        original = hfa._commit
        def fail(root, updates):
            if "history/access.jsonl" in updates:
                raise OSError("synthetic access failure")
            return original(root, updates)
        with patch.object(hfa, "_commit", fail):
            with self.assertRaises(OSError):
                hfa.cmd_route(self.mem, "HFA初始化", conversation_id="c4", turn_id="t", return_result=True)
        self.assertEqual(snapshot(self.mem), before)

    def test_invalid_write_policy_fails_closed_on_status(self):
        path = self.mem / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["write_policy"] = "always"
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            hfa._config(self.mem)


class HookPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hfa-policy-")
        self.mem = Path(self.tmp.name) / "memory"
        proc = subprocess.run([sys.executable, "-B", str(CLI), "init", str(self.mem)],
                              capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def set_policy(self, policy):
        path = self.mem / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["write_policy"] = policy
        path.write_text(json.dumps(config), encoding="utf-8")

    def fire(self, payload):
        proc = subprocess.run([sys.executable, "-B", str(HOOK), "--memory-dir", str(self.mem), "--phase", "post"],
                              input=json.dumps(payload), capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), proc.stderr

    def prose(self, **extra):
        body = {
            "conversationId": "policy",
            "turnId": "1",
            "lastUserInput": "请核对导出",
            "lastAssistantResponse": "结论：已将导出编码固定为 UTF-8。",
        }
        body.update(extra)
        return body

    def test_default_host_policy_does_not_save_plain_prose(self):
        result, _ = self.fire(self.prose())
        self.assertEqual(result, {})
        self.assertFalse((self.mem / "chunks/chunk_0001.json").exists())

    def test_explicit_ignores_host_true_and_auto_still_obeys_denial(self):
        self.set_policy("explicit")
        result, _ = self.fire(self.prose(memoryWriteAllowed=True))
        self.assertEqual(result, {})
        saved, _ = self.fire({"conversationId": "policy", "turnId": "2", "decision": {
            "title": "结构化", "hit_words": ["utf-8"], "conclusion": "导出使用 UTF-8。"}})
        self.assertIn("saved", saved)
        self.set_policy("auto")
        denied, _ = self.fire(self.prose(turnId="3", lastUserInput="这轮不要保存", memoryWriteAllowed=True))
        self.assertEqual(denied, {})
        blocked, _ = self.fire(self.prose(turnId="4", memoryWriteAllowed=False))
        self.assertEqual(blocked, {})
        chatter, _ = self.fire(self.prose(turnId="5", lastAssistantResponse="今天晚饭建议吃西红柿鸡蛋面，既简单又营养。"))
        self.assertEqual(chatter, {})
        allowed, _ = self.fire(self.prose(turnId="6"))
        self.assertIn("saved", allowed)
        ids = sorted(path.name for path in (self.mem / "chunks").glob("*.json"))
        self.assertEqual(ids, ["chunk_0001.json", "chunk_0002.json"])


if __name__ == "__main__":
    unittest.main()
