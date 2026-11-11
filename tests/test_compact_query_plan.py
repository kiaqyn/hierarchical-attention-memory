"""Reduce echoed query text without losing deduplication/coverage provenance."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from validate_hfa import ROOT, hfa, invoke
from test_mcp import Client, StdioServerParameters, _open_client

QUERY = "Which earlier decisions govern these controls?"


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf8")


class CompactQueryPlanChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-compact-plan-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "synthetic-memory"
        invoke(hfa.cmd_init, self.root)
        invoke(hfa.cmd_add_topic, self.root, "Cache connection pool", "cache", "Cache pool is bounded at 24.")
        invoke(hfa.cmd_add_topic, self.root, "Export retry budget", "retry budget", "Export retry budget is three attempts.")
        self.delivery = hfa._delivery_module()

    def recall(self, directions):
        return hfa.cmd_route(self.root, QUERY, refinement_queries=directions, return_result=True)

    def test_long_hypotheses_are_not_echoed_and_complete_evidence_is_preserved(self):
        directions = [("cache " + "temporary hypothesis wording " * 10)[:256],
                      ("retry budget " + "temporary hypothesis wording " * 10)[:256]]
        raw = self.recall(directions)
        before = copy.deepcopy(raw)
        compact = self.delivery.compact_recall(raw)
        self.assertEqual(raw, before)
        self.assertEqual(compact["memory_text"], raw["context_text"])
        self.assertEqual(compact["matches"], [{"id": r["id"], "path": r["path"]} for r in raw["selected"]])
        self.assertNotIn("refinement_queries", compact["query_plan"])
        self.assertEqual(compact["query_plan"]["refinement_input_indices"], [0, 1])
        self.assertEqual(compact["query_plan"]["coverage"], raw["query_plan"]["coverage"])
        self.assertEqual(raw["query_plan"]["refinement_queries"], directions)
        for text in directions:
            self.assertNotIn(text, serialized(compact).decode("utf8"))

    def test_trimming_duplicates_and_original_query_keep_exact_input_positions(self):
        directions = [" " + QUERY + " ", " cache connection pool ", "cache connection pool"]
        raw = self.recall(directions)
        self.assertEqual(raw["query_plan"]["refinement_queries"], ["cache connection pool"])
        self.assertEqual(raw["query_plan"]["refinement_input_indices"], [1])
        compact = self.delivery.compact_recall(raw)
        self.assertEqual(compact["query_plan"]["refinement_input_indices"], [1])
        self.assertEqual(directions[compact["query_plan"]["refinement_input_indices"][0]].strip(),
                         raw["query_plan"]["refinement_queries"][0])

    def test_duplicate_middle_maps_to_first_and_third_original_inputs(self):
        raw = self.recall(["cache connection pool", " cache connection pool ", "export retry budget"])
        self.assertEqual(raw["query_plan"]["refinement_input_indices"], [0, 2])
        compact = self.delivery.compact_recall(raw)
        self.assertEqual(compact["query_plan"]["refinement_input_indices"], [0, 2])
        self.assertTrue(compact["query_plan"]["coverage"][1]["selected_ids"])
        self.assertTrue(compact["query_plan"]["coverage"][2]["selected_ids"])

    def test_tiny_queries_do_not_grow_the_compact_body(self):
        for directions in (["x"], ["x", "y", "z"]):
            raw = self.recall(directions)
            plan = self.delivery.compact_recall(raw)["query_plan"]
            self.assertEqual(plan["refinement_queries"], directions)
            self.assertNotIn("refinement_input_indices", plan)
            legacy = dict(raw["query_plan"])
            legacy.pop("refinement_input_indices")
            self.assertLessEqual(len(serialized(plan)), len(serialized(legacy)))

    def test_no_plan_retains_the_original_compact_schema(self):
        for directions in (None, [QUERY, " " + QUERY + " "]):
            raw = self.recall(directions)
            self.assertNotIn("query_plan", raw)
            self.assertNotIn("query_plan", self.delivery.compact_recall(raw))

    def test_legacy_or_untrusted_mapping_falls_back_without_losing_query_text(self):
        legacy = {"refinement_queries": ["long explanatory direction"], "coverage": [{"query_index": 1}]}
        for indices in (None, [True], [3], [-1], [0, 1], "0", [float("nan")]):
            plan = copy.deepcopy(legacy)
            if indices is not None:
                plan["refinement_input_indices"] = indices
            before = copy.deepcopy(plan)
            projected = self.delivery._compact_query_plan(plan)
            self.assertEqual(projected["refinement_queries"], before["refinement_queries"])
            self.assertEqual(projected["coverage"], before["coverage"])
        duplicate_map = {"refinement_queries": ["first direction", "second direction"],
                         "refinement_input_indices": [0, 0]}
        self.assertEqual(self.delivery._compact_query_plan(duplicate_map), duplicate_map)

    def test_projection_does_not_search_or_persist_hypotheses(self):
        directions = ["cache connection pool", "export retry budget"]
        paths = [self.root / "config.json", self.root / "summary.md", *list((self.root / "chunks").glob("*.json"))]
        before = {p: p.read_bytes() for p in paths}
        raw = self.recall(directions)
        access = (self.root / "history/access.jsonl").read_bytes()
        self.delivery.compact_recall(raw)
        self.assertEqual(access, (self.root / "history/access.jsonl").read_bytes())
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        for text in directions:
            self.assertNotIn(text, access.decode("utf8"))

    def test_actual_cli_full_keeps_text_and_compact_exposes_the_same_evidence(self):
        def call(output):
            process = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/hfa_manager.py"),
                "recall", str(self.root), "--query", QUERY, "--refine-query", "cache connection pool",
                "--refine-query", "export retry budget", "--output", output,
                "--conversation-id", "synthetic-cli", "--turn-id", "same"],
                capture_output=True, encoding="utf8", timeout=30)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            return json.loads(process.stdout)
        full, compact = call("full"), call("compact")
        self.assertEqual(full["query_plan"]["refinement_queries"], ["cache connection pool", "export retry budget"])
        self.assertEqual(full["context_text"], compact["memory_text"])
        self.assertEqual(compact["query_plan"]["refinement_input_indices"], [0, 1])
        self.assertNotIn("refinement_queries", compact["query_plan"])
        self.assertEqual(compact["turn_count"], full["turn_count"])
        self.assertEqual(compact["recall_count"], full["recall_count"] + 1)


@unittest.skipUnless(Client is not None, "optional MCP SDK required")
class ActualCompactPlanMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_compact_retains_direction_mapping_and_same_turn_budget(self):
        with tempfile.TemporaryDirectory(prefix="hfa-compact-plan-stdio-") as temp:
            root = Path(temp).resolve() / "synthetic-memory"
            invoke(hfa.cmd_init, root)
            invoke(hfa.cmd_add_topic, root, "Cache connection pool", "cache", "Cache pool is bounded at 24.")
            invoke(hfa.cmd_add_topic, root, "Export retry budget", "retry budget", "Export retry budget is three attempts.")
            config_file = root / "config.json"
            config = json.loads(config_file.read_text(encoding="utf8"))
            config["max_recalls_per_turn"] = 2
            config_file.write_text(json.dumps(config), encoding="utf8")
            invoke(hfa.cmd_reindex, root)
            args = ["-B", str(ROOT / "scripts/hfa_mcp.py"), "--memory-dir", str(root), "--compact"]
            if os.environ.get("HFA_TEST_SEMANTIC_MODEL"):
                args += ["--semantic-model", os.environ["HFA_TEST_SEMANTIC_MODEL"]]
            params = StdioServerParameters(command=sys.executable, args=args,
                       env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            async with _open_client(params) as client:
                arguments = {"query": QUERY, "refinement_queries": ["cache connection pool", " cache connection pool ", "export retry budget"],
                             "conversation_id": "synthetic", "turn_id": "one"}
                first = await client.call_tool("recall_memory", arguments)
                self.assertFalse(first.is_error, first)
                data = first.structured_content
                self.assertEqual(data["query_plan"]["refinement_input_indices"], [0, 2])
                self.assertNotIn("refinement_queries", data["query_plan"])
                self.assertEqual(len(data["matches"]), 2)
                again = await client.call_tool("recall_memory", arguments)
                self.assertFalse(again.is_error, again)
                self.assertEqual(again.structured_content["turn_count"], data["turn_count"])
                self.assertEqual(again.structured_content["turn_budget"]["remaining"], 0)
                self.assertTrue((await client.call_tool("recall_memory", arguments)).is_error)


if __name__ == "__main__":
    unittest.main()
