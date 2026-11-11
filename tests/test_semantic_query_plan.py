"""Bounded semantic directions, provenance, final coverage and failure limits."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from validate_hfa import hfa, invoke, snapshot
from test_mcp import ROOT, Client, StdioServerParameters, _open_client

sem = hfa._semantic_module()
IDENTITY = {"model": "synthetic-query-planner", "revision": "fixture", "dimensions": sem.DIMENSIONS}
MAIN = "Which earlier decisions would help"
FIRST = "indirect description of first concern"
SECOND = "a paraphrased second concern"
UNKNOWN = "unrelated hypothesis"


def vector(i):
    result = [0.] * sem.DIMENSIONS
    result[i] = 1.
    return result


class SemanticQueryPlanChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-semantic-directions-")
        self.root = Path(self.temp.name).resolve() / "memory"
        invoke(hfa.cmd_init, self.root)
        self.first = self.add("ALPHA_PROOF")
        self.second = self.add("BETA_PROOF")
        self.calls = []
        owner = self

        class Encoder:
            def __init__(self, *_):
                pass

            def encode(self, texts, kind="passage"):
                owner.calls.append((kind, list(texts)))
                if kind == "passage":
                    return [vector(0 if "ALPHA_PROOF" in t else 1 if "BETA_PROOF" in t else 8)
                            for t in texts]
                return [vector({FIRST: 0, SECOND: 1}.get(t, 9)) for t in texts]

        self.patches = [patch.object(sem, "model_identity", return_value=IDENTITY),
                        patch.object(sem, "LocalEncoder", Encoder)]
        for item in self.patches:
            item.start()
        self.runtime = sem.EncoderSession(Path("synthetic-model"))

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def add(self, text, project=None, supersedes=None):
        rc, out = invoke(hfa.cmd_add_topic, self.root, "Fixture", "opaque-fixture", text,
                         None, project, "synthetic:test", supersedes, None, True)
        self.assertEqual(rc, 0)
        return json.loads(out)["id"]

    def recall(self, **kwargs):
        return hfa.cmd_route(self.root, MAIN, mode="semantic", semantic_model=Path("synthetic-model"),
                             semantic_runtime=self.runtime, return_result=True, **kwargs)

    def truth(self):
        return {p: p.read_bytes() for p in (self.root / "chunks").glob("*.json")}

    def test_paraphrases_are_encoded_in_one_query_batch_with_provenance(self):
        result = self.recall(refinement_queries=[FIRST, SECOND])
        self.assertEqual({r["id"] for r in result["selected"]}, {self.first, self.second})
        self.assertEqual([t for kind, t in self.calls if kind == "query"], [[MAIN, FIRST, SECOND]])
        self.assertEqual(result["retrieval"]["semantic_queries"], 3)
        self.assertEqual(result["retrieval"]["query_batches"], 1)
        self.assertEqual(result["query_plan"]["semantic_query"], "all_directions")
        self.assertEqual({r["id"]: r["match"]["query_indices"] for r in result["selected"]},
                         {self.first: [1], self.second: [2]})
        self.assertTrue(all(r["match"]["method"] == "semantic" for r in result["selected"]))

    def test_original_single_query_preserves_results_and_empty_plan(self):
        a = hfa.cmd_route(self.root, FIRST, mode="semantic", semantic_model=Path("synthetic-model"),
                          semantic_runtime=self.runtime, return_result=True)
        b = hfa.cmd_route(self.root, FIRST, mode="semantic", semantic_model=Path("synthetic-model"),
                          semantic_runtime=self.runtime, return_result=True, refinement_queries=[])
        self.assertEqual(a["selected"], b["selected"])
        self.assertEqual(a["context_text"], b["context_text"])
        self.assertNotIn("query_plan", b)

    def test_cached_vectors_and_encoder_are_reused_for_all_directions(self):
        self.recall(refinement_queries=[FIRST, SECOND])
        result = self.recall(refinement_queries=[FIRST, UNKNOWN])
        self.assertEqual(sum(kind == "passage" for kind, _ in self.calls), 1)
        self.assertEqual(result["retrieval"]["encoded_documents"], 0)
        self.assertTrue(result["retrieval"]["encoder_reused"])
        self.assertEqual(result["retrieval"]["encoder_loads"], 1)

    def test_unknown_direction_is_visible_without_claiming_evidence_complete(self):
        result = self.recall(refinement_queries=[FIRST, UNKNOWN])
        coverage = result["query_plan"]["coverage"]
        self.assertEqual(coverage[1]["selected_ids"], [self.first])
        self.assertEqual(coverage[2]["selected_ids"], [])
        self.assertIn(2, result["query_plan"]["uncovered_query_indices"])
        self.assertEqual(result["query_plan"]["coverage_means"], "returned_candidates_only")

    def test_one_slot_reports_dropped_direction_and_never_duplicates_records(self):
        result = self.recall(refinement_queries=[FIRST, SECOND], max_topics=1)
        self.assertEqual(len(result["selected"]), 1)
        selected = result["selected"][0]["id"]
        dropped = ({self.first, self.second} - {selected}).pop()
        self.assertTrue(any(dropped in c["deferred_ids"] for c in result["query_plan"]["coverage"]))
        self.assertTrue(all(c["selected_ids"] == [] for c in result["query_plan"]["coverage"]
                            if dropped in c["deferred_ids"]))
        self.assertLessEqual(result["context_tokens"]["count"], result["context_token_budget"])

    def test_recall_budget_cannot_be_multiplied_by_number_of_queries(self):
        path = self.root / "config.json"
        config = json.loads(path.read_text(encoding="utf8"))
        config["recall_token_budget"] = 1
        path.write_text(json.dumps(config), encoding="utf8")
        invoke(hfa.cmd_reindex, self.root)
        result = self.recall(refinement_queries=[FIRST, SECOND])
        self.assertEqual(result["selected"], [])
        self.assertTrue(all(not c["selected_ids"] for c in result["query_plan"]["coverage"]))
        self.assertLessEqual(result["recall_tokens"]["count"], 1)

    def test_coverage_uses_final_full_context_after_trimming(self):
        initial = self.recall(refinement_queries=[FIRST, SECOND])
        path = self.root / "config.json"
        config = json.loads(path.read_text(encoding="utf8"))
        config["context_token_budget"] = initial["context_tokens"]["count"] - 1
        path.write_text(json.dumps(config), encoding="utf8")
        invoke(hfa.cmd_reindex, self.root)
        result = self.recall(refinement_queries=[FIRST, SECOND])
        returned = {r["id"] for r in result["selected"]}
        self.assertLess(len(returned), 2)
        self.assertEqual({key for c in result["query_plan"]["coverage"] for key in c["selected_ids"]}, returned)
        self.assertTrue(any(d["reason"] == "total context budget" for d in result["deferred"]))
        self.assertLessEqual(result["context_tokens"]["count"], result["context_token_budget"])

    def test_lexical_and_semantic_provenance_merge_without_duplicate_evidence(self):
        result = self.recall(refinement_queries=["ALPHA_PROOF", FIRST])
        self.assertEqual([r["id"] for r in result["selected"]], [self.first])
        self.assertEqual(result["selected"][0]["match"]["query_indices"], [1, 2])
        self.assertNotEqual(result["selected"][0]["match"]["method"], "semantic")

    def test_directions_matching_same_record_use_one_slot(self):
        result = self.recall(refinement_queries=[FIRST, FIRST + "?", SECOND])
        self.assertEqual(len(result["selected"]), 2)
        found = next(r for r in result["selected"] if r["id"] == self.first)
        self.assertEqual(found["match"]["query_indices"], [1, 2])

    def test_deferred_diagnostics_have_a_fixed_size_with_an_exact_total(self):
        for index in range(6):
            self.add("ALPHA_PROOF additional synthetic evidence " + str(index))
        result = self.recall(refinement_queries=[FIRST], max_topics=1)
        coverage = result["query_plan"]["coverage"][1]
        self.assertEqual(len(coverage["selected_ids"]), 1)
        self.assertEqual(len(coverage["deferred_ids"]), 5)
        self.assertEqual(coverage["deferred_total"], 6)

    def test_unrelated_queries_abstain(self):
        result = self.recall(refinement_queries=[UNKNOWN, "another unrelated hypothesis"])
        self.assertEqual(result["selected"], [])
        self.assertTrue(result["zero_hits"])

    def test_per_direction_margin_is_not_bypassed_by_fusion(self):
        self.add("ALPHA_PROOF near duplicate")
        result = self.recall(refinement_queries=[FIRST, SECOND], semantic_min_margin=.01)
        self.assertEqual([r["id"] for r in result["selected"]], [self.second])
        self.assertEqual(result["query_plan"]["coverage"][1]["selected_ids"], [])

    def test_scope_and_replacement_filter_before_encoding(self):
        new = self.add("ALPHA_PROOF replacement", supersedes=[self.first])
        private = self.add("ALPHA_PROOF PRIVATE_BRANCH", project="S1")
        result = self.recall(refinement_queries=[FIRST, SECOND])
        self.assertEqual({r["id"] for r in result["selected"]}, {new, self.second})
        passages = [t for kind, texts in self.calls if kind == "passage" for t in texts]
        self.assertTrue(all("PRIVATE_BRANCH" not in t for t in passages))
        self.assertNotIn(private, [r["id"] for r in result["selected"]])

    def test_same_turn_cap_terms_not_saved_and_no_extra_encoder_call_after_cap(self):
        path = self.root / "config.json"
        config = json.loads(path.read_text(encoding="utf8"))
        config["max_recalls_per_turn"] = 2
        path.write_text(json.dumps(config), encoding="utf8")
        invoke(hfa.cmd_reindex, self.root)
        before = self.truth()
        before_config = path.read_bytes()
        args = {"conversation_id": "synthetic", "turn_id": "same-turn"}
        initial = self.recall(**args)
        second = self.recall(refinement_queries=[FIRST, SECOND], **args)
        self.assertEqual((initial["turn_count"], second["turn_count"]), (1, 1))
        self.assertEqual(second["turn_budget"], {"limit": 2, "used": 2, "remaining": 0})
        calls = len(self.calls)
        with self.assertRaises(hfa.RecallLimitError):
            self.recall(refinement_queries=[FIRST], **args)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(before, self.truth())
        self.assertEqual(path.read_bytes(), before_config)
        log = (self.root / "history/access.jsonl").read_text(encoding="utf8")
        self.assertNotIn(FIRST, log)
        self.assertEqual(json.loads(log.splitlines()[-1])["semantic_directions"], 3)

    def test_invalid_plan_fails_before_inference_or_writes(self):
        before = snapshot(self.root)
        for queries in ([FIRST] * 4, [""], [False]):
            with self.assertRaises(hfa.MemoryError):
                self.recall(refinement_queries=queries)
        self.assertEqual(self.calls, [])
        self.assertEqual(before, snapshot(self.root))

    def test_wrong_query_batch_size_cannot_commit_partial_cache_or_turn(self):
        before = snapshot(self.root)
        original = self.runtime.encode

        def broken(texts, kind="passage"):
            return [vector(0)] if kind == "query" else original(texts, kind)

        with patch.object(self.runtime, "encode", broken), self.assertRaisesRegex(hfa.MemoryError, "query vectors"):
            self.recall(refinement_queries=[FIRST, SECOND])
        self.assertEqual(before, snapshot(self.root))


@unittest.skipUnless(os.environ.get("HFA_TEST_SEMANTIC_MODEL") and Client is not None,
                     "explicit local model and optional MCP SDK required")
class ActualSemanticQueryPlanMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_worker_forwards_all_directions_and_final_coverage(self):
        with tempfile.TemporaryDirectory(prefix="hfa-real-semantic-plan-") as temp:
            memory = Path(temp).resolve() / "synthetic-memory"
            invoke(hfa.cmd_init, memory)
            invoke(hfa.cmd_add_topic, memory, "字形恢复", "synthetic-font",
                   "中文字符变成方框，安装缺失的字体包后恢复显示。SYNTHETIC_FONT_RULE_731。")
            invoke(hfa.cmd_add_topic, memory, "报表日期", "synthetic-calendar",
                   "报表以Asia/Taipei时区划分自然日，数据库统一保存UTC时间。")
            config_file = memory / "config.json"
            config = json.loads(config_file.read_text(encoding="utf8"))
            config["max_recalls_per_turn"] = 2
            config_file.write_text(json.dumps(config), encoding="utf8")
            invoke(hfa.cmd_reindex, memory)
            before = {p: p.read_bytes() for p in (memory / "chunks").glob("*.json")}
            parameters = StdioServerParameters(command=sys.executable,
                args=["-B", str(ROOT / "scripts/hfa_mcp.py"), "--memory-dir", str(memory),
                      "--semantic-model", os.environ["HFA_TEST_SEMANTIC_MODEL"], "--compact"],
                env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            async with _open_client(parameters) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({t.name for t in tools}, {"recall_memory", "remember_decision", "memory_health"})
                args = {"query": MAIN, "conversation_id": "synthetic", "turn_id": "one-real-turn"}
                initial_call = await client.call_tool("recall_memory", args)
                self.assertFalse(initial_call.is_error, initial_call)
                initial = initial_call.structured_content
                self.assertEqual(initial["matches"], [])
                follow = await client.call_tool("recall_memory", {**args,
                    "refinement_queries": ["中文字符只剩矩形占位符，之前怎样补齐文字？", "报表日期"]})
                self.assertFalse(follow.is_error, follow)
                data = follow.structured_content
                self.assertIn("SYNTHETIC_FONT_RULE_731", data["memory_text"])
                self.assertIn("Asia/Taipei", data["memory_text"])
                self.assertEqual(data["retrieval"]["semantic_queries"], 3)
                self.assertEqual(data["retrieval"]["query_batches"], 1)
                self.assertTrue(data["retrieval"]["encoder_reused"])
                self.assertEqual(data["retrieval"]["encoder_loads"], 1)
                self.assertEqual(data["retrieval"]["encoded_documents"], 0)
                self.assertEqual(data["turn_count"], initial["turn_count"])
                self.assertEqual(data["turn_budget"]["remaining"], 0)
                coverage = data["query_plan"]["coverage"]
                self.assertTrue(coverage[1]["selected_ids"])
                self.assertTrue(coverage[2]["selected_ids"])
                self.assertLessEqual(data["payload_tokens"]["count"], data["payload_budget"])
                exhausted = await client.call_tool("recall_memory", args)
                self.assertTrue(exhausted.is_error)
            self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
