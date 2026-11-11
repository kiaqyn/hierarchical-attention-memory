"""Bounded opt-in local pair-relevance fallback and missing-field veto checks."""
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
rerank = hfa._rerank_module()
SEM_IDENTITY = {"model": sem.MODEL, "revision": sem.REVISION, "files": sem.PINNED.copy(),
                "pipeline": "e5-query-passage-mean-l2-512-singleton-v2", "dimensions": sem.DIMENSIONS}
PAIR_IDENTITY = {"model": rerank.MODEL, "revision": rerank.REVISION, "files": rerank.PINNED.copy(),
                 "pipeline": "pair-longest-first-512-singleton-v1"}


def unit_vector(axis, secondary=None, secondary_weight=0.0):
    vec = [0.0] * sem.DIMENSIONS
    vec[axis] = 1.0
    if secondary is not None:
        vec[secondary] = secondary_weight
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


class SemanticRerankChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-semantic-rerank-")
        self.root = Path(self.temp.name).resolve() / "memory"
        invoke(hfa.cmd_init, self.root)
        self.charge = self.add(
            "扣款去重",
            "同一个付款事件重复到达时，按幂等键返回最初的处理结果；幂等记录保留36小时。DEDUP_PROOF_101。",
        )
        self.noise = self.add(
            "付款审计",
            "付款事件日志每天汇总到审计文件，保留30天。AUDIT_NOISE_202。",
        )
        self.build = self.add(
            "不稳定用例隔离",
            "偶发失败的测试最多自动再跑1次；仍不稳定则进入隔离队列，阻止发布。QUARANTINE_PROOF_303。",
        )
        self.pair_calls = []
        owner = self

        class FakeEncoder:
            def __init__(self, *_):
                pass

            def encode(self, texts, kind="passage"):
                out = []
                for t in texts:
                    if kind == "passage":
                        if "DEDUP_PROOF_101" in t:
                            out.append(unit_vector(0, 3, 1.3))
                        elif "AUDIT_NOISE_202" in t:
                            out.append(unit_vector(1, 3, 1.25))
                        elif "QUARANTINE_PROOF_303" in t:
                            out.append(unit_vector(2, 3, 1.28))
                        else:
                            out.append(unit_vector(9))
                    else:
                        if "STRONG_DENSE_ZH" in t:
                            out.append(unit_vector(0, 3, 1.3))
                        elif "PARAPHRASE_EN_CHARGE" in t or "who approved" in t.lower() or "encryption" in t.lower():
                            # Cosine similarity with axis 3 is ~0.78 (> 0.75 floor, < 0.875 primary gate)
                            out.append(unit_vector(3))
                        elif "PARAPHRASE_EN_BUILD" in t:
                            out.append(unit_vector(3))
                        elif "LOW_DENSE_NOISE" in t:
                            out.append(unit_vector(8))
                        else:
                            out.append(unit_vector(7))
                return out

        class FakePairModel:
            def __init__(self, *_):
                pass

            def score_pairs(self, pairs):
                owner.pair_calls.append(list(pairs))
                scores = []
                for q, text in pairs:
                    if ("PARAPHRASE_EN_CHARGE" in q or "who approved" in q.lower()) and "DEDUP_PROOF_101" in text:
                        scores.append(0.86)
                    elif "who approved" in q.lower() and "AUDIT_NOISE_202" in text:
                        scores.append(0.75)
                    elif "PARAPHRASE_EN_BUILD" in q and "QUARANTINE_PROOF_303" in text:
                        scores.append(0.82)
                    elif "AMBIGUOUS_MARGIN" in q and ("DEDUP_PROOF_101" in text or "AUDIT_NOISE_202" in text):
                        scores.append(0.74)
                    else:
                        scores.append(0.04)
                return scores

        self.patches = [
            patch.object(sem, "model_identity", return_value=SEM_IDENTITY),
            patch.object(sem, "LocalEncoder", FakeEncoder),
            patch.object(rerank, "model_identity", return_value=PAIR_IDENTITY),
            patch.object(rerank, "LocalPairModel", FakePairModel),
        ]
        for p in self.patches:
            p.start()
        self.sem_dir = Path(self.temp.name).resolve() / "sem-model"
        self.pair_dir = Path(self.temp.name).resolve() / "pair-model"
        self.sem_dir.mkdir()
        self.pair_dir.mkdir()
        self.runtime = sem.EncoderSession(self.sem_dir)
        self.pair_runtime = rerank.PairSession(self.pair_dir)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def add(self, title, text, project=None, supersedes=None):
        rc, out = invoke(hfa.cmd_add_topic, self.root, title, "opaque-hitword", text,
                         None, project, "synthetic:rerank-test", supersedes, None, True)
        self.assertEqual(rc, 0)
        return json.loads(out)["id"]

    def recall(self, query, **kwargs):
        return hfa.cmd_route(
            self.root, query, mode="semantic", semantic_model=self.sem_dir,
            semantic_runtime=self.runtime, semantic_reranker=self.pair_dir,
            reranker_runtime=self.pair_runtime, return_result=True, **kwargs
        )

    def test_reranker_requires_explicit_semantic_mode_and_model(self):
        with self.assertRaises(hfa.MemoryError):
            hfa.cmd_route(self.root, "PARAPHRASE_EN_CHARGE", mode="hybrid",
                          semantic_reranker=self.pair_dir, return_result=True)
        with self.assertRaises(hfa.MemoryError):
            hfa.cmd_route(self.root, "PARAPHRASE_EN_CHARGE", mode="semantic",
                          semantic_reranker=self.pair_dir, return_result=True)
        self.assertEqual(self.pair_calls, [])

    def test_single_query_fallback_recovers_paraphrase_and_marks_method(self):
        without = hfa.cmd_route(self.root, "PARAPHRASE_EN_CHARGE", mode="semantic",
                                semantic_model=self.sem_dir, semantic_runtime=self.runtime, return_result=True)
        self.assertEqual(without["selected"], [])
        with_rerank = self.recall("PARAPHRASE_EN_CHARGE")
        self.assertEqual([r["id"] for r in with_rerank["selected"]], [self.charge])
        match = with_rerank["selected"][0]["match"]
        self.assertEqual(match["method"], "semantic_rerank")
        self.assertEqual(match["rerank_support"][0]["query_index"], 0)
        self.assertAlmostEqual(match["rerank_support"][0]["pair_score"], 0.86)
        info = with_rerank["retrieval"]["reranker"]
        self.assertEqual(info["accepted_query_indices"], [0])
        self.assertEqual(info["meaning"], "candidate_relevance_not_answer_verification")

    def test_dense_hit_is_preserved_without_invoking_reranker_for_that_direction(self):
        result = self.recall("STRONG_DENSE_ZH")
        self.assertEqual([r["id"] for r in result["selected"]], [self.charge])
        self.assertEqual(result["selected"][0]["match"]["method"], "semantic")
        self.assertNotIn("rerank_support", result["selected"][0]["match"])
        self.assertEqual(result["retrieval"]["reranker"]["pairs_scored"], 0)
        self.assertEqual(self.pair_calls, [])

    def test_multi_direction_only_reranks_uncovered_refinements_and_keeps_dense_method(self):
        result = self.recall(
            "Broad framing question",
            refinement_queries=["STRONG_DENSE_ZH", "PARAPHRASE_EN_BUILD"],
        )
        self.assertEqual({r["id"] for r in result["selected"]}, {self.charge, self.build})
        by_id = {r["id"]: r for r in result["selected"]}
        self.assertEqual(by_id[self.charge]["match"]["method"], "semantic")
        self.assertEqual(by_id[self.build]["match"]["method"], "semantic_rerank")
        self.assertEqual(result["retrieval"]["reranker"]["accepted_query_indices"], [2])
        # Base query (index 0) and already-covered direction 1 must not be pair-scored.
        scored_queries = {q for batch in self.pair_calls for q, _ in batch}
        self.assertEqual(scored_queries, {"PARAPHRASE_EN_BUILD"})

    def test_missing_field_veto_blocks_top_hit_without_promoting_runner_up(self):
        result = self.recall("Who approved PARAPHRASE_EN_CHARGE?")
        self.assertEqual(result["selected"], [])
        self.assertEqual(result["retrieval"]["reranker"]["accepted_query_indices"], [])

    def test_dense_floor_and_margin_guard_block_weak_or_ambiguous_candidates(self):
        low_dense = self.recall("LOW_DENSE_NOISE")
        self.assertEqual(low_dense["selected"], [])
        self.assertEqual(low_dense["retrieval"]["reranker"]["pairs_scored"], 0)
        ambiguous = self.recall("AMBIGUOUS_MARGIN PARAPHRASE_EN_CHARGE")
        self.assertEqual(ambiguous["selected"], [])

    def test_inactive_branch_and_superseded_records_never_reach_reranker(self):
        replacement = self.add("扣款去重更新", "幂等记录改为保留48小时。DEDUP_PROOF_101。", supersedes=[self.charge])
        private = self.add("支线机密", "PRIVATE_BRANCH_SECRET DEDUP_PROOF_101。", project="S1")
        result = self.recall("PARAPHRASE_EN_CHARGE")
        self.assertEqual([r["id"] for r in result["selected"]], [replacement])
        scored_texts = [text for batch in self.pair_calls for _, text in batch]
        self.assertTrue(all("PRIVATE_BRANCH_SECRET" not in t for t in scored_texts))
        self.assertTrue(all("保留36小时" not in t for t in scored_texts))
        self.assertNotIn(private, [r["id"] for r in result["selected"]])


@unittest.skipUnless(
    os.environ.get("HFA_TEST_SEMANTIC_MODEL")
    and os.environ.get("HFA_TEST_RERANKER_MODEL")
    and Client is not None,
    "explicit local semantic + reranker models and MCP SDK required",
)
class ActualLocalRerankerChecks(unittest.IsolatedAsyncioTestCase):
    def test_pinned_reranker_singleton_invariance_and_checksum_guard(self):
        model_dir = Path(os.environ["HFA_TEST_RERANKER_MODEL"]).resolve(strict=True)
        identity = rerank.model_identity(model_dir)
        self.assertEqual(identity["model"], rerank.MODEL)
        session = rerank.PairSession(model_dir)
        target = (
            "How do we avoid charging twice when a payment event is delivered again?",
            "扣款去重\n同一个付款事件重复到达时，按幂等键返回最初的处理结果；幂等记录保留36小时。",
        )
        companion_short = ("unrelated short query", "简短无关记录")
        companion_long = ("What is the retention window? " * 12, "长文本干扰段落。" * 30)
        alone = session.score_pairs([target])[0]
        with_short = session.score_pairs([companion_short, target])[1]
        with_long = session.score_pairs([target, companion_long])[0]
        self.assertEqual(alone, with_short)
        self.assertEqual(alone, with_long)
        self.assertEqual(session.loads, 1)

    async def test_stdio_mcp_worker_with_real_reranker_and_turn_guard(self):
        with tempfile.TemporaryDirectory(prefix="hfa-real-reranker-mcp-") as temp:
            memory = Path(temp).resolve() / "memory"
            invoke(hfa.cmd_init, memory)
            invoke(
                hfa.cmd_add_topic, memory, "扣款去重", "synthetic-charge",
                "同一个付款事件重复到达时，按幂等键返回最初的处理结果；幂等记录保留36小时。REAL_RERANK_771。",
            )
            invoke(
                hfa.cmd_add_topic, memory, "付款审计", "synthetic-audit",
                "付款事件日志每天汇总到审计文件，保留30天。",
            )
            config_path = memory / "config.json"
            config = json.loads(config_path.read_text(encoding="utf8"))
            config["max_recalls_per_turn"] = 2
            config_path.write_text(json.dumps(config), encoding="utf8")
            invoke(hfa.cmd_reindex, memory)
            before = {p: p.read_bytes() for p in (memory / "chunks").glob("*.json")}
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-B", str(ROOT / "scripts/hfa_mcp.py"), "--memory-dir", str(memory),
                    "--semantic-model", os.environ["HFA_TEST_SEMANTIC_MODEL"],
                    "--semantic-reranker", os.environ["HFA_TEST_RERANKER_MODEL"], "--compact",
                ],
                env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            async with _open_client(parameters) as client:
                hit = await client.call_tool(
                    "recall_memory",
                    {
                        "query": "How do we avoid charging twice when a payment event is delivered again?",
                        "conversation_id": "synthetic",
                        "turn_id": "rerank-turn-1",
                    },
                )
                self.assertFalse(hit.is_error, hit)
                data = hit.structured_content
                self.assertIn("REAL_RERANK_771", data["memory_text"])
                self.assertEqual(data["retrieval"]["reranker"]["accepted_query_indices"], [0])
                self.assertEqual(data["retrieval"]["reranker"]["model_loads"], 1)
                veto = await client.call_tool(
                    "recall_memory",
                    {
                        "query": "Who approved the payment deduplication design?",
                        "conversation_id": "synthetic",
                        "turn_id": "rerank-turn-1",
                    },
                )
                self.assertFalse(veto.is_error, veto)
                veto_data = veto.structured_content
                self.assertEqual(veto_data["matches"], [])
                self.assertTrue(veto_data["retrieval"]["reranker"]["model_reused"])
                self.assertEqual(veto_data["turn_budget"]["remaining"], 0)
            self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
