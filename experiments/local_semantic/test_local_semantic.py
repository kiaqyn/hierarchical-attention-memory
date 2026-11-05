"""Policy and fusion checks; the full model run is a separate experiment."""
import unittest
from local_semantic import accepted_dense, eligible_documents, reciprocal_rank_fusion


class LocalSemanticTests(unittest.TestCase):
    def test_scope_state_and_supersession_filter_before_embedding(self):
        docs = [{"id":"old","project":"M1","status":"ACTIVE","supersedes":[]},
                {"id":"new","project":"M1","status":"ACTIVE","supersedes":["old"]},
                {"id":"branch","project":"S1","status":"ACTIVE","supersedes":[]},
                {"id":"archive","project":"M1","status":"ARCHIVED","supersedes":[]}]
        self.assertEqual([doc["id"] for doc in eligible_documents(docs)], ["new"])
        copied = eligible_documents(docs)
        copied[0]["supersedes"].append("something")
        self.assertEqual(docs[1]["supersedes"], ["old"])

    def test_duplicate_ids_are_rejected(self):
        doc = {"id":"x","project":"M1","status":"ACTIVE"}
        with self.assertRaises(ValueError):
            eligible_documents([doc,doc])

    def test_low_score_and_ambiguous_margin_abstain(self):
        ranking = [("a",.90),("b",.895)]
        self.assertFalse(accepted_dense(ranking,.91,0))
        self.assertFalse(accepted_dense(ranking,.88,.02))
        self.assertEqual(accepted_dense(ranking,.88,0), ["a","b"])

    def test_fusion_keeps_both_sources_and_deduplicates(self):
        self.assertEqual(reciprocal_rank_fusion(["a","b"],["b","c"])[0], "b")
        self.assertEqual(set(reciprocal_rank_fusion(["a","b"],["b","c"])), {"a","b","c"})
        self.assertEqual(reciprocal_rank_fusion(["a","b"],[]), ["a","b"])

    def test_invalid_numeric_gates_fail_instead_of_accepting_candidates(self):
        for threshold,margin,limit in ((float("nan"),0,2),(.8,float("inf"),2),(.8,-1,2),(.8,0,True)):
            with self.assertRaises(ValueError):
                accepted_dense([("a",.9)],threshold,margin,limit)
        with self.assertRaises(ValueError):
            accepted_dense([("a",float("nan"))],.8,0)


if __name__ == "__main__":
    unittest.main()
