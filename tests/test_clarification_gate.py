"""Narrow pre-query guard: actual stdio execution, false positives and boundaries."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from test_mcp import Client, StdioServerParameters, _open_client

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hfa_mcp_guard", ROOT/"scripts/hfa_mcp.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class ClarificationGateTests(unittest.TestCase):
    def test_ambiguous_gui_variants_require_clarification(self):
        for query in ("之前那个画面一直原地打转的问题，最后采用了什么办法？",
                      "屏幕不停转圈怎么办", "界面转个不停", "转圈的那个画面怎么修",
                      "The screen is spinning. What fixed it?",
                      "画面转圈，是加载还是相机的问题？"):
            with self.subTest(query=query):
                self.assertIn("等待加载", adapter._clarification_question(query))

    def test_explicit_loading_or_camera_context_does_not_block(self):
        for query in ("应用启动后画面打转，进不了首页", "等待加载时屏幕转圈",
                      "切换相机后画面一直转圈", "画面本身在旋转，镜头打转",
                      "screen spinning while loading", "screen spins after camera movement"):
            with self.subTest(query=query):
                self.assertIsNone(adapter._clarification_question(query))

    def test_rejected_alternative_does_not_mask_the_explicit_answer(self):
        for query in ("画面打转，不是相机视角，是等待加载的小圆圈",
                      "屏幕转圈，并非加载而是相机镜头不停转",
                      "screen spinning, not camera motion but loading spinner"):
            with self.subTest(query=query):
                self.assertIsNone(adapter._clarification_question(query))

    def test_non_gui_and_unrelated_questions_are_unaffected(self):
        for query in ("橙色夜莺的饲养预算是多少", "自行车轮子转圈", "跑步原地打转",
                      "窗口里的圆圈怎样改颜色", "screen spinlock implementation", "startup hang renderer",
                      "不要查画面打转，只查终端配置", "别查屏幕转圈，而是查终端配置",
                      "do not search screen spinning, recall terminal settings",
                      "之前应用打开后一直只有等待的小圆圈，迟迟进不了首页，最终怎么处理的？"):
            with self.subTest(query=query):
                self.assertIsNone(adapter._clarification_question(query))


class ClarificationGateMCPTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(Client is None, "optional MCP runtime unavailable")
    async def test_blocked_call_does_not_search_or_advance_then_clear_call_runs(self):
        with tempfile.TemporaryDirectory(prefix="hfa-clarify-gate-") as tmp:
            memory = Path(tmp).resolve()/"memory"
            params = StdioServerParameters(command=sys.executable,
                args=["-B", str(ROOT/"scripts/hfa_mcp.py"), "--memory-dir", str(memory)],
                env={"PYTHONUTF8":"1", "PYTHONDONTWRITEBYTECODE":"1"})
            async with _open_client(params) as client:
                self.assertTrue((await client.call_tool("memory_health", {})).structured_content["healthy"])
                before = {p.relative_to(memory):p.read_bytes() for p in memory.rglob("*") if p.is_file()}
                blocked = (await client.call_tool("recall_memory", {
                    "query":"之前那个画面一直原地打转的问题，最后采用了什么办法？",
                    "conversation_id":"synthetic-session", "turn_id":"synthetic-turn"})).structured_content
                self.assertEqual(blocked["status"], "clarification_required")
                self.assertFalse(blocked["recall_executed"])
                self.assertEqual(blocked["next_action"], "ask_user")
                self.assertIsNone(blocked["zero_hits"])
                self.assertFalse(blocked["memory_text"])
                self.assertIn("等待加载", blocked["clarification_question"])
                self.assertEqual(before, {p.relative_to(memory):p.read_bytes() for p in memory.rglob("*") if p.is_file()})
                self.assertFalse((memory/"history/access.jsonl").exists())
                clear = (await client.call_tool("recall_memory", {
                    "query":"应用启动后画面转圈，等待加载，进不了首页",
                    "conversation_id":"synthetic-session", "turn_id":"synthetic-turn"})).structured_content
                self.assertEqual(clear["status"], "retrieved")
                self.assertTrue(clear["recall_executed"])
                self.assertTrue(clear["zero_hits"])
                self.assertEqual(clear["recall_count"], 1)
                self.assertEqual(clear["turn_count"], 1)
                self.assertEqual(len((memory/"history/access.jsonl").read_text(encoding="utf8").splitlines()), 1)
                invalid = await client.call_tool("recall_memory", {"query":" "})
                self.assertTrue(invalid.is_error)
                oversized = await client.call_tool("recall_memory", {"query":"画面打转"*2001})
                self.assertTrue(oversized.is_error)


if __name__ == "__main__":
    unittest.main()
