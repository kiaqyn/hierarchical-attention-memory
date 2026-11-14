"""Run with the optional MCP environment; exercises the actual stdio subprocess."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import Client, StdioServerParameters
    try:
        from mcp.client.stdio import stdio_client
    except ImportError:
        stdio_client = None
except ImportError:
    Client = StdioServerParameters = stdio_client = None

ROOT = Path(__file__).resolve().parent.parent


def _open_client(params):
    if hasattr(params, "__aenter__") or stdio_client is None:
        return Client(params)
    return Client(stdio_client(params))


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(Client is None, "optional mcp SDK not installed")
    async def test_real_client_summary_delivery_measurement_and_stable_turn_identity(self):
        with tempfile.TemporaryDirectory(prefix="hfa-mcp-summary-") as temp:
            memory=Path(temp)/"memory"
            params=StdioServerParameters(command=sys.executable,
                args=["-B",str(ROOT/"scripts/hfa_mcp.py"),"--memory-dir",str(memory)],
                env={"PYTHONUTF8":"1","PYTHONDONTWRITEBYTECODE":"1"})
            async with _open_client(params) as client:
                await client.call_tool("memory_health",{})
                path=memory/"summary.md"
                marker="MCP_ONLY_SUMMARY_RULE_ZEBRA_999"
                path.write_text(path.read_text(encoding="utf8")+"\n- "+marker,encoding="utf8")
                args={"query":"previous unknown concept", "conversation_id":"one-session", "turn_id":"one-turn"}
                first=(await client.call_tool("recall_memory",args)).structured_content
                second=(await client.call_tool("recall_memory",args)).structured_content
                self.assertEqual(first["memory_text"].count(marker),1)
                self.assertEqual(first["turn_count"],1)
                self.assertEqual(second["turn_count"],1)
                self.assertTrue(first["turn_count_exact"])
                self.assertTrue(first["search_feedback"])
                self.assertFalse(second["search_feedback"])
                self.assertLessEqual(first["payload_tokens"]["count"],first["payload_budget"])
                self.assertIsNone(first["delivery"]["host_accepted"])
                self.assertIsNone(first["delivery"]["model_used"])

    def test_print_config_works_without_sdk_or_initialized_memory(self):
        with tempfile.TemporaryDirectory(prefix="hfa-mcp-cfg-") as temp:
            uninitialized = Path(temp) / "not_yet_created"
            proc = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/hfa_mcp.py"),
                 "--memory-dir", str(uninitialized), "--print-config"],
                capture_output=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            entry = payload["mcpServers"]["hfa-project-memory"]
            self.assertEqual(entry["args"][-1], str(uninitialized.resolve()))

            # Zero-config invocation without --memory-dir resolves HFA_MEMORY_DIR or ./memory
            env = os.environ.copy()
            env["HFA_MEMORY_DIR"] = str(uninitialized)
            proc_zero = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/hfa_mcp.py"), "--print-config"],
                capture_output=True, encoding="utf-8", env=env, timeout=30,
            )
            self.assertEqual(proc_zero.returncode, 0, proc_zero.stderr)
            payload_zero = json.loads(proc_zero.stdout)
            self.assertEqual(
                payload_zero["mcpServers"]["hfa-project-memory"]["args"][-1],
                str(uninitialized.resolve()),
            )

    @unittest.skipIf(Client is None, "optional mcp SDK not installed")
    async def test_real_client_write_recall_retry_update_restart_and_error(self):
        with tempfile.TemporaryDirectory(prefix="hfa-mcp-") as temp:
            # Start without pre-running `init` and without `--memory-dir` to exercise zero-config auto-init
            memory = Path(temp) / "memory"
            params = StdioServerParameters(command=sys.executable,
                args=["-B", str(ROOT / "scripts/hfa_mcp.py")],
                env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "HFA_MEMORY_DIR": str(memory)})
            async with _open_client(params) as client:
                listed = await client.list_tools()
                self.assertEqual({t.name for t in listed.tools}, {"remember_decision", "recall_memory", "memory_health"})
                health = await client.call_tool("memory_health", {})
                self.assertTrue(health.structured_content["healthy"])
                args = {"title": "报表导出", "hit_words": ["export-ticket"],
                        "conclusion": "中文乱码已通过 UTF-8 修复。", "source": "test:export-verified", "request_id": "export-1"}
                saved = await client.call_tool("remember_decision", args)
                self.assertFalse(saved.is_error)
                old_id = saved.structured_content["id"]
                repeated = await client.call_tool("remember_decision", args)
                self.assertTrue(repeated.structured_content["duplicate"])
                found = await client.call_tool("recall_memory", {"query": "中文变问号"})
                self.assertIn("UTF-8", found.structured_content["memory_text"])
                self.assertEqual(found.structured_content["matches"][0]["id"], old_id)
                self.assertNotIn("text", found.structured_content["matches"][0])
                conflict = await client.call_tool("remember_decision", {**args, "conclusion": "Changed."})
                self.assertTrue(conflict.is_error)
                self.assertIn("request_id already used", conflict.content[0].text)
                for invalid in ({"source": ""}, {"request_id": " "}, {"hit_words": ["a,b"]}, {"supersedes": ["../../out"]}):
                    rejected = await client.call_tool("remember_decision", {**args, **invalid, "request_id": invalid.get("request_id", "event-bad")})
                    self.assertTrue(rejected.is_error, invalid)
                updated = await client.call_tool("remember_decision", {
                    **args, "conclusion": "乱码修复改为 UTF-8-SIG，兼容表格软件。",
                    "request_id": "export-2", "supersedes": [old_id]})
                self.assertFalse(updated.is_error)
                new_id = updated.structured_content["id"]
                # Arguments beginning with '-' are data and cannot add CLI flags.
                strange = await client.call_tool("recall_memory", {"query": "--help"})
                self.assertFalse(strange.is_error)
                unknown = await client.call_tool("recall_memory", {"query": "今晚吃什么"})
                self.assertEqual(unknown.structured_content["matches"], [])
                self.assertIn("核心记忆摘要", unknown.structured_content["memory_text"])
                self.assertFalse(unknown.structured_content["turn_count_exact"])
            explicit_params = StdioServerParameters(command=sys.executable,
                args=["-B", str(ROOT / "scripts/hfa_mcp.py"), "--memory-dir", str(memory)],
                env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            async with _open_client(explicit_params) as restarted:
                result = await restarted.call_tool("recall_memory", {"query": "garbled text"})
                self.assertEqual([m["id"] for m in result.structured_content["matches"]], [new_id])
                self.assertIn("UTF-8-SIG", result.structured_content["memory_text"])
                health = await restarted.call_tool("memory_health", {})
                self.assertEqual(health.structured_content["total_topics"], 2)
                self.assertTrue((memory / f"chunks/{old_id}.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
