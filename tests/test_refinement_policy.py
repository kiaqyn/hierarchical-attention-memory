"""Synthetic real CLI/MCP follow-up checks; no model or private memory."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from validate_hfa import hfa, invoke
from test_mcp import Client, StdioServerParameters, _open_client

ROOT = Path(__file__).resolve().parent.parent

class RefinementPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-refinement-")
        self.mem = Path(self.temp.name).resolve() / "memory"
        invoke(hfa.cmd_init, self.mem)
        rc, out = invoke(hfa.cmd_add_topic, self.mem, "Startup note", "boot-ticket",
                         "startup hang fixed by pinning renderer to 4.2; evidence demo:boot")
        self.assertEqual(rc, 0, out)

    def tearDown(self):
        self.temp.cleanup()

    def recall(self, query, turn="one", conv="refinement", allow=True):
        rc, out = invoke(hfa.cmd_route, self.mem, query, None, None, "hybrid", conv, turn, allow)
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def test_miss_then_expanded_query_hits_same_turn(self):
        first = self.recall("那个画面一直原地打转")
        self.assertTrue(first["zero_hits"])
        self.assertIn("3-5", first["search_feedback"])
        self.assertIn("ONE follow-up", first["context_text"])
        second = self.recall("startup hang renderer")
        self.assertFalse(second["zero_hits"])
        self.assertIn("renderer to 4.2", second["recall_text"])
        self.assertEqual((first["turn_count"], second["turn_count"]), (1, 1))
        self.assertEqual(second["turn_search_count"], 2)
        self.assertFalse(second["search_feedback"])

    def test_second_miss_does_not_reissue_retry_hint(self):
        first = self.recall("以前提过的紫色北极熊")
        second = self.recall("unknown-zyx-geode")
        self.assertTrue(first["zero_hits"] and second["zero_hits"])
        self.assertTrue(first["search_feedback"])
        self.assertFalse(second["search_feedback"])
        self.assertFalse(second["selected"])

    def test_queries_do_not_persist_generated_aliases_or_change_sources(self):
        originals = {p: p.read_bytes() for p in [self.mem/"config.json", *sorted((self.mem/"chunks").glob("*.json"))]}
        self.recall("那个画面一直原地打转")
        self.recall("startup hang renderer")
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})
        self.assertEqual({p for p in (self.mem/"chunks").glob("*.json")}, set(originals)-{self.mem/"config.json"})

    def test_missing_identity_does_not_claim_exact_retry_tracking(self):
        out = self.recall("unknown-zyx-geode", turn=None, conv=None)
        self.assertFalse(out["turn_count_exact"])
        self.assertIsNone(out["turn_count"])
        self.assertFalse(out["search_feedback"])
        self.assertIn("本次未找到，不代表记录不存在", out["context_text"])
        self.assertIn("先问一句", out["context_text"])
        self.assertIn("停止检索", out["context_text"])
        self.assertLessEqual(out["context_tokens"]["count"], out["context_token_budget"])

    def test_no_refinement_respects_caller_choice(self):
        out = self.recall("unknown-zyx-geode", allow=False)
        self.assertTrue(out["zero_hits"])
        self.assertFalse(out["search_feedback"])

    def test_complete_payload_remains_within_budget(self):
        for query in ("那个画面一直原地打转", "startup hang renderer"):
            out = self.recall(query)
            self.assertLessEqual(out["context_tokens"]["count"], out["context_token_budget"])
            self.assertEqual(hfa.measure_tokens(out["context_text"]), out["context_tokens"])

    def test_successful_first_recall_does_not_claim_a_zero_hit(self):
        out = self.recall("startup hang renderer")
        self.assertFalse(out["zero_hits"])
        self.assertFalse(out["search_feedback"])
        self.assertIn("renderer to 4.2", out["recall_text"])
        self.assertNotIn("检索提醒", out["context_text"])

    def test_caution_drops_before_rejecting_valid_core_budget(self):
        initial = self.recall("unknown-zyx-geode", turn=None, conv=None)
        core = "\n\n".join(part for part in initial["context_text"].split("\n\n")
                              if not part.startswith("检索提醒："))
        core_budget = hfa.measure_tokens(core)["count"]
        config_path = self.mem/"config.json"
        config = json.loads(config_path.read_text(encoding="utf8"))
        config.update(summary_token_budget=initial["summary_tokens"]["count"],
                      context_token_budget=core_budget)
        config_path.write_text(json.dumps(config), encoding="utf8")
        rc, log = invoke(hfa.cmd_reindex, self.mem, False)
        self.assertEqual(rc, 0, log)
        out = self.recall("unknown-zyx-geode", turn=None, conv=None)
        self.assertTrue(out["zero_hits"])
        self.assertIn("Core project summary", out["context_text"])
        self.assertNotIn("检索提醒", out["context_text"])
        self.assertLessEqual(out["context_tokens"]["count"], core_budget)

class RefinementMCPTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(Client is None, "optional MCP runtime unavailable")
    async def test_actual_stdio_tool_policy_and_two_lookups(self):
        with tempfile.TemporaryDirectory(prefix="hfa-refine-mcp-") as tmp:
            memory = Path(tmp).resolve()/"memory"
            for args in (["init", str(memory)], ["add-topic", str(memory), "--title=Startup note",
                         "--hit-words=boot-ticket", "--conclusion=startup hang fixed by pinning renderer to 4.2; evidence demo:boot"]):
                proc = subprocess.run([sys.executable, "-B", str(ROOT/"scripts/hfa_manager.py"), *args],
                                      capture_output=True, encoding="utf8", timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stdout+proc.stderr)
            params = StdioServerParameters(command=sys.executable,
                args=["-B", str(ROOT/"scripts/hfa_mcp.py"), "--memory-dir", str(memory)],
                env={"PYTHONUTF8":"1", "PYTHONDONTWRITEBYTECODE":"1"})
            async with _open_client(params) as client:
                tools = await client.list_tools()
                tool_list = tools.tools if hasattr(tools, "tools") else tools
                descriptions = [t.description for t in tool_list if t.name=="recall_memory"]
                self.assertEqual(len(descriptions), 1)
                self.assertIn("3-5", descriptions[0])
                self.assertIn("not a server-enforced call cap", descriptions[0])
                # Ambiguous GUI wording has its own guard tests. This test checks
                # ordinary miss/refinement and must actually reach retrieval.
                args = {"query":"unknown-zyx-geode", "conversation_id":"refine-mcp", "turn_id":"one"}
                first = (await client.call_tool("recall_memory", args)).structured_content
                args["query"] = "startup hang renderer"
                second = (await client.call_tool("recall_memory", args)).structured_content
                self.assertTrue(first["zero_hits"])
                self.assertIn("3-5", first["memory_text"])
                self.assertFalse(second["zero_hits"])
                self.assertIn("renderer to 4.2", second["memory_text"])
                self.assertEqual(first["turn_count"], second["turn_count"])
                self.assertEqual(second["recall_count"], 2)
                self.assertIsNone(second["delivery"]["model_used"])
                missing_ids = (await client.call_tool("recall_memory", {"query":"unknown-zyx-geode"})).structured_content
                self.assertFalse(missing_ids["turn_count_exact"])
                self.assertFalse(missing_ids["search_feedback"])
                self.assertIn("先问一句", missing_ids["memory_text"])
                self.assertIn("不代表记录不存在", missing_ids["memory_text"])
                self.assertLessEqual(missing_ids["payload_tokens"]["count"], missing_ids["payload_budget"])

if __name__ == "__main__":
    unittest.main()
