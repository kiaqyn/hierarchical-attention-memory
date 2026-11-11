"""Pinned local embeddings must not depend on unrelated batch companions."""
import os
from pathlib import Path
import unittest

from validate_hfa import hfa

sem = hfa._semantic_module()
MODEL = os.environ.get('HFA_TEST_SEMANTIC_MODEL')
QUERY = 'export retry budget'
PASSAGE = '导出重试策略\nExport retry budget is three attempts, with eight seconds between failures.'
LONG = 'A completely different synthetic question about orchestral instruments and concert programs. ' * 40


@unittest.skipUnless(MODEL, 'explicit HFA_TEST_SEMANTIC_MODEL required')
class BatchInvarianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = sem.model_identity(MODEL)
        cls.encoder = sem.LocalEncoder(MODEL)

    def test_query_independent_of_neighbors_and_position(self):
        expected = self.encoder.encode([QUERY], 'query')[0]
        for texts, index in (([QUERY, 'hi'], 0), ([QUERY, LONG], 0), ([LONG, QUERY], 1)):
            with self.subTest(index=index, length=len(texts[1])):
                self.assertTrue(expected == self.encoder.encode(texts, 'query')[index], 'Query vector changed with batch context')

    def test_passage_independent_of_neighbors_and_position(self):
        expected = self.encoder.encode([PASSAGE], 'passage')[0]
        for texts, index in (([PASSAGE, 'hi'], 0), ([PASSAGE, LONG], 0), ([LONG, PASSAGE], 1)):
            with self.subTest(index=index, length=len(texts[1])):
                self.assertTrue(expected == self.encoder.encode(texts, 'passage')[index], 'Passage vector changed with batch context')

    def test_cold_and_incremental_cache_have_identical_vectors_and_rankings(self):
        def state(texts):
            docs = [{'id': str(i), 'title': '', 'summary': [text], 'project': 'M1',
                     'status': 'ACTIVE', 'supersedes': []} for i, text in enumerate(texts)]
            return sem.snapshot(Path('synthetic-batch-invariance'), docs, 'M1')
        texts = ['hi', PASSAGE, LONG]
        cold, _ = sem.prepare_cache(state(texts), None, self.identity, self.encoder.encode)
        incremental = None
        for size in range(1, len(texts)+1):
            incremental, _ = sem.prepare_cache(state(texts[:size]), incremental, self.identity, self.encoder.encode)
        self.assertTrue(cold == incremental, 'Full and incremental caches differ for identical source content')
        q = self.encoder.encode([QUERY], 'query')[0]
        self.assertEqual(sem.rank(cold, q, .875, 0), sem.rank(incremental, q, .875, 0))
        def fail(_):
            self.fail('A warm unchanged cache must not re-encode')
        warm, info = sem.prepare_cache(state(texts), cold, self.identity, fail)
        self.assertEqual(warm, cold)
        self.assertEqual(info['encoded_documents'], 0)

    def test_order_duplicates_and_former_batch_boundary(self):
        texts = [QUERY, 'hi', PASSAGE, QUERY, 'one', 'two', 'three', 'four', LONG, QUERY]
        expected = [self.encoder.encode([text], 'query')[0] for text in texts]
        self.assertTrue(self.encoder.encode(texts, 'query') == expected, 'Input ordering or vectors changed across batch boundaries')

    def test_legacy_batch_pipeline_cache_is_invalidated(self):
        legacy = {**self.identity, 'pipeline': 'e5-query-passage-mean-l2-512-v1'}
        self.assertNotEqual(legacy, self.identity)
        doc = {'id': 'a', 'title': 'synthetic', 'summary': [PASSAGE], 'project': 'M1',
               'status': 'ACTIVE', 'supersedes': []}
        state = sem.snapshot(Path('synthetic-pipeline-version'), [doc], 'M1')
        old, _ = sem.prepare_cache(state, None, legacy, self.encoder.encode)
        self.assertFalse(sem.cache_consistent(old, state, self.identity))
        new, info = sem.prepare_cache(state, old, self.identity, self.encoder.encode)
        self.assertEqual(info['cache_status'], 'rebuilt_identity_or_scope')
        self.assertEqual(info['encoded_documents'], 1)
        self.assertTrue(sem.cache_consistent(new, state, self.identity))

    def test_empty_and_invalid_prefix(self):
        self.assertEqual(self.encoder.encode([], 'query'), [])
        with self.assertRaises(ValueError):
            self.encoder.encode([QUERY], 'unsupported')


if __name__ == '__main__':
    unittest.main()
