"""A popular first direction must not crowd out a distinct requested facet."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from validate_hfa import hfa, invoke
from test_mcp import ROOT, Client, StdioServerParameters, _open_client


class PlannedCoverageChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-planned-coverage-")
        self.root = Path(self.temp.name).resolve() / "synthetic-memory"
        invoke(hfa.cmd_init, self.root)
        self.metrics = self.add("Cache metrics", "cache", "Redis cache reports sampled hit counters.")
        self.pool = self.add("Cache connection pool", "cache", "Redis cache connection pool is bounded at 24.")
        self.retry = self.add("Export retry budget", "retry budget", "Export retry budget is three attempts.")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, title, word, text, project=None, supersedes=None):
        rc, output = invoke(hfa.cmd_add_topic, self.root, title, word, text,
                            None, project, "synthetic:coverage", supersedes, None, True)
        self.assertEqual(rc, 0)
        return json.loads(output)["id"]

    def test_distinct_direction_not_displaced_by_repeated_first_direction(self):
        for mode in ("hybrid", "keyword"):
            with self.subTest(mode=mode):
                result = hfa.cmd_route(self.root,
                    "What cache choices and related failure policy did we settle on?", mode=mode,
                    refinement_queries=["cache connection pool", "export retry budget"], return_result=True)
                first = self.pool if mode == "hybrid" else self.metrics
                self.assertEqual({item["id"] for item in result["selected"]}, {first, self.retry})
                self.assertEqual(result["selected"][0]["id"], first)
                self.assertTrue(result["query_plan"]["coverage"][2]["selected_ids"])
                self.assertLessEqual(result["context_tokens"]["count"], result["context_token_budget"])

    def recall(self, **kwargs):
        return hfa.cmd_route(self.root, "What cache choices and related failure policy did we settle on?",
            refinement_queries=["cache connection pool", "export retry budget"], return_result=True, **kwargs)

    def test_single_slot_keeps_the_best_first_hit(self):
        result = self.recall(max_topics=1)
        self.assertEqual([r["id"] for r in result["selected"]], [self.pool])
        self.assertEqual(result["query_plan"]["coverage"][2]["selected_ids"], [])
        self.assertIn(self.retry, result["query_plan"]["coverage"][2]["deferred_ids"])

    def test_oversized_facet_does_not_hide_a_smaller_record_for_that_facet(self):
        large = self.add("Export retry budget", "retry budget,export", "Export retry budget " + "oversized " * 30)
        short_records = [json.loads((self.root / "chunks" / f"{key}.json").read_text(encoding="utf8"))
                         for key in (self.pool, self.retry)]
        config_path = self.root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf8"))
        config["recall_token_budget"] = hfa.measure_tokens("\n\n".join(hfa._chunk_text(c) for c in short_records))["count"]
        config_path.write_text(json.dumps(config), encoding="utf8")
        invoke(hfa.cmd_reindex, self.root)
        result = self.recall()
        self.assertEqual({r["id"] for r in result["selected"]}, {self.pool, self.retry})
        self.assertTrue(any(d["id"] == large and d["reason"] == "recall budget or item limit" for d in result["deferred"]))
        self.assertEqual(result["query_plan"]["coverage"][2]["selected_ids"], [self.retry])

    def test_missing_direction_does_not_suppress_other_existing_records(self):
        result = hfa.cmd_route(self.root, "cache decisions",
            refinement_queries=["cache connection pool", "heron migration"], return_result=True)
        self.assertEqual({r["id"] for r in result["selected"]}, {self.metrics, self.pool})
        self.assertEqual(result["query_plan"]["coverage"][2]["selected_ids"], [])

    def test_superseded_and_private_records_do_not_claim_coverage(self):
        newer = self.add("Export retry budget", "retry budget", "Export retry budget is now two attempts.",
                         supersedes=[self.retry])
        private = self.add("Export retry budget", "retry budget,export", "PRIVATE_BRANCH export retry budget.", project="S1")
        result = self.recall()
        self.assertEqual({r["id"] for r in result["selected"]}, {self.pool, newer})
        self.assertNotIn("PRIVATE_BRANCH", result["context_text"])
        coverage = result["query_plan"]["coverage"][2]["selected_ids"]
        self.assertNotIn(private, coverage)
        self.assertNotIn(self.retry, coverage)

    def test_original_or_one_refinement_selection_stays_in_relevance_order(self):
        for refinements in (None, ["export retry budget"]):
            actual = hfa.cmd_route(self.root, "cache decisions", refinement_queries=refinements, return_result=True)
            def old_order(candidates, *_):
                yield from candidates
            with patch.object(hfa, "_coverage_candidate_order", old_order):
                prior = hfa.cmd_route(self.root, "cache decisions", refinement_queries=refinements, return_result=True)
            self.assertEqual(actual["selected"], prior["selected"])
            self.assertEqual(actual["context_text"], prior["context_text"])

    def test_one_record_covering_both_directions_does_not_force_an_extra_slot(self):
        combined = self.add("Cache connection pool and export retry budget", "cache,retry budget",
                            "Cache connection pool is 24; export retry budget is three attempts.")
        result = self.recall()
        self.assertEqual(result["selected"][0]["id"], combined)
        self.assertTrue(all(combined in c["selected_ids"] for c in result["query_plan"]["coverage"][1:]))
        self.assertEqual(len({r["id"] for r in result["selected"]}), len(result["selected"]))

    def test_shared_budget_and_real_turn_count_are_not_expanded(self):
        config_path = self.root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf8"))
        config["max_recalls_per_turn"] = 2
        config_path.write_text(json.dumps(config), encoding="utf8")
        invoke(hfa.cmd_reindex, self.root)
        paths = [config_path, self.root / "summary.md", *list((self.root / "chunks").glob("*.json"))]
        before = {p: p.read_bytes() for p in paths}
        args = {"conversation_id": "synthetic", "turn_id": "one"}
        self.recall(**args)
        second = self.recall(**args)
        self.assertEqual(second["turn_count"], 1)
        self.assertEqual(second["turn_budget"]["remaining"], 0)
        with self.assertRaises(hfa.RecallLimitError):
            self.recall(**args)
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        self.assertLessEqual(second["context_tokens"]["count"], second["context_token_budget"])
        self.assertLessEqual(second["recall_tokens"]["count"], second["recall_token_budget"])


@unittest.skipUnless(Client is not None, "optional MCP SDK required")
class ActualPlannedCoverageMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_preserves_a_distinct_facet_under_a_two_record_limit(self):
        with tempfile.TemporaryDirectory(prefix="hfa-coverage-stdio-") as temp:
            root = Path(temp).resolve() / "synthetic-memory"
            invoke(hfa.cmd_init, root)
            ids = []
            for title, word, text in (("Cache metrics", "cache", "Redis cache sampled counters."),
                                      ("Cache connection pool", "cache", "Redis cache connection pool is 24."),
                                      ("Export retry budget", "retry budget", "Export retry budget is three attempts.")):
                rc, output = invoke(hfa.cmd_add_topic, root, title, word, text,
                                    None, None, "synthetic:stdio", None, None, True)
                self.assertEqual(rc, 0)
                ids.append(json.loads(output)["id"])
            arguments = ["-B", str(ROOT / "scripts/hfa_mcp.py"), "--memory-dir", str(root), "--compact"]
            if os.environ.get("HFA_TEST_SEMANTIC_MODEL"):
                arguments += ["--semantic-model", os.environ["HFA_TEST_SEMANTIC_MODEL"]]
            params = StdioServerParameters(command=sys.executable, args=arguments,
                       env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            async with _open_client(params) as client:
                result = await client.call_tool("recall_memory", {
                    "query": "What cache choices and related failure policy did we settle on?",
                    "refinement_queries": ["cache connection pool", "export retry budget"],
                    "conversation_id": "synthetic", "turn_id": "one"})
                self.assertFalse(result.is_error, result)
                data = result.structured_content
                self.assertEqual({r["id"] for r in data["matches"]}, {ids[1], ids[2]})
                self.assertEqual(data["query_plan"]["selection"], "first_relevance_then_uncovered_direction")
                self.assertEqual(data["turn_count"], 1)
                self.assertLessEqual(data["payload_tokens"]["count"], data["payload_budget"])
                if os.environ.get("HFA_TEST_SEMANTIC_MODEL"):
                    self.assertEqual(data["retrieval"]["semantic_queries"], 3)
                    self.assertEqual(data["retrieval"]["query_batches"], 1)


if __name__ == "__main__":
    unittest.main()
