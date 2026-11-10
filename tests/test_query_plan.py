"""Caller-planned recall: evidence union, fixed scope/budget/identity, real stdio."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from validate_hfa import hfa, invoke, snapshot
from test_mcp import Client, StdioServerParameters, _open_client

ROOT = Path(__file__).resolve().parent.parent


class QueryPlanChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hfa-plan-')
        self.root = Path(self.tmp.name).resolve()/'memory'
        hfa.cmd_init(self.root, quiet=True, profile="example")
        self.cipher = self.add('Cipher suite', 'suite-reference', 'TLS cipher suite is AES256.')
        self.renewal = self.add('Renewal window', 'renewal-reference', 'Certificate renewal happens every thirty days.')

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, title, word, body, **kwargs):
        return invoke(hfa.cmd_add_topic, self.root, title, word, body, None,
                      kwargs.get('project'), '', kwargs.get('supersedes'), None, True)

    def recall(self, **kwargs):
        return hfa.cmd_route(self.root, 'Which controls govern secure connections?',
                             return_result=True, **kwargs)

    def test_independent_directions_find_both_sources_once(self):
        initial = self.recall(conversation_id='synthetic', turn_id='one')
        result = self.recall(refinement_queries=['cipher suite', 'certificate renewal', 'cipher suite'],
                             conversation_id='synthetic', turn_id='one')
        self.assertEqual(len(result['selected']), 2)
        self.assertIn('AES256', result['context_text'])
        self.assertIn('thirty days', result['context_text'])
        self.assertEqual((initial['turn_count'], result['turn_count']), (1, 1))
        self.assertEqual(result['turn_search_count'], 2)
        self.assertEqual(result['query_plan']['lexical_queries'], 3)
        self.assertEqual(len({r['id'] for r in result['selected']}), 2)
        self.assertTrue(all(r['match']['query_indices'] for r in result['selected']))

    def test_old_single_query_matches_explicit_empty_plan(self):
        a = hfa.cmd_route(self.root, 'cipher suite', return_result=True)
        b = hfa.cmd_route(self.root, 'cipher suite', refinement_queries=[], return_result=True)
        self.assertEqual(a['selected'], b['selected'])
        self.assertEqual(a['context_text'], b['context_text'])
        self.assertNotIn('query_plan', b)

    def test_flat_unrelated_terms_do_not_suppress_independent_facets(self):
        old_style = hfa.cmd_route(self.root, 'cipher renewal audit', return_result=True)
        self.assertEqual(old_style['selected'], [])
        planned = self.recall(refinement_queries=['cipher', 'renewal', 'audit'])
        self.assertEqual(len(planned['selected']), 2)

    def test_single_record_limit_and_complete_budget_still_apply(self):
        result = self.recall(refinement_queries=['cipher suite', 'certificate renewal'], max_topics=1)
        self.assertEqual(len(result['selected']), 1)
        self.assertTrue(result['deferred'])
        self.assertLessEqual(result['context_tokens']['count'], result['context_token_budget'])
        self.assertLessEqual(result['recall_tokens']['count'], result['recall_token_budget'])
        self.assertIn('Core project summary:', result['context_text'])

    def test_invalid_or_forbidden_plans_do_not_write(self):
        before = snapshot(self.root)
        for queries in (['x']*4, 'x', [False], [''], ['x'*257], [None], {}):
            with self.subTest(queries=queries), self.assertRaises(hfa.MemoryError):
                self.recall(refinement_queries=queries)
        with self.assertRaises(hfa.MemoryError):
            self.recall(refinement_queries=['cipher suite'], allow_refinement=False)
        with self.assertRaises(hfa.MemoryError):
            hfa.cmd_route(self.root, '不要检索记忆', refinement_queries=['cipher suite'], return_result=True)
        self.assertEqual(before, snapshot(self.root))

    def test_scope_and_supersession_are_not_expanded(self):
        old = next(json.loads(p.read_text(encoding='utf8'))['id'] for p in (self.root/'chunks').glob('*.json')
                   if 'AES256' in p.read_text(encoding='utf8'))
        self.add('Cipher replacement', 'new-suite-ref', 'TLS cipher suite is CHACHA20.', supersedes=[old])
        self.add('Branch certificate', 'private-renewal-ref', 'Certificate renewal includes PRIVATE_BRANCH.', project='S1')
        result = self.recall(refinement_queries=['cipher suite', 'certificate renewal'])
        self.assertIn('CHACHA20', result['context_text'])
        self.assertNotIn('AES256', result['context_text'])
        self.assertNotIn('PRIVATE_BRANCH', result['context_text'])
        self.assertTrue(any(x['reason'].startswith('superseded;') for x in result['deferred']))

    def test_no_terms_saved_and_one_call_cap_remains_one_call(self):
        p = self.root/'config.json'
        config = json.loads(p.read_text(encoding='utf8'));config['max_recalls_per_turn'] = 2
        p.write_text(json.dumps(config), encoding='utf8');invoke(hfa.cmd_reindex, self.root)
        paths = [p, *sorted((self.root/'chunks').glob('*.json')), self.root/'summary.md']
        originals = {p: p.read_bytes() for p in paths}
        args = dict(conversation_id='synthetic', turn_id='one')
        self.recall(**args)
        second = self.recall(refinement_queries=['cipher suite', 'certificate renewal'], **args)
        self.assertEqual(second['turn_budget'], {'limit':2, 'used':2, 'remaining':0})
        with self.assertRaises(hfa.RecallLimitError):
            self.recall(refinement_queries=['cipher suite'], **args)
        self.assertEqual(originals, {p: p.read_bytes() for p in paths})
        self.assertNotIn('cipher suite', (self.root/'history/access.jsonl').read_text(encoding='utf8'))

    def test_unrelated_directions_and_weak_noise_still_abstain(self):
        result = self.recall(refinement_queries=['polar bear audition', 'garden soil chemistry'])
        self.assertEqual(result['selected'], [])
        self.assertTrue(result['zero_hits'])

    def test_actual_cli_repeated_flags_and_compact_provenance(self):
        process = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/hfa_manager.py'), 'recall', str(self.root),
                     '--query=Which controls govern secure connections?', '--refine-query=cipher suite',
                     '--refine-query=certificate renewal', '--output=compact'], capture_output=True,
                     encoding='utf8', timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(len(result['matches']), 2)
        self.assertEqual(result['query_plan']['lexical_queries'], 3)


@unittest.skipIf(Client is None, 'optional MCP runtime unavailable')
class ActualQueryPlanMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_actual_stdio_plan_discovery_execution_and_validation(self):
        with tempfile.TemporaryDirectory(prefix='hfa-plan-mcp-') as temp:
            memory = Path(temp)/'memory'
            invoke(hfa.cmd_init, memory)
            invoke(hfa.cmd_add_topic, memory, 'Cipher suite', 'suite-reference', 'TLS cipher suite is AES256.')
            invoke(hfa.cmd_add_topic, memory, 'Renewal window', 'renewal-reference', 'Certificate renewal is thirty days.')
            params = StdioServerParameters(command=sys.executable, args=['-B', str(ROOT/'scripts/hfa_mcp.py'),
                    '--memory-dir', str(memory), *(['--semantic-model', os.environ['HFA_TEST_SEMANTIC_MODEL']]
                       if os.environ.get('HFA_TEST_SEMANTIC_MODEL') else [])],
                    env={'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
            async with _open_client(params) as client:
                tools = (await client.list_tools()).tools
                recall = next(t for t in tools if t.name=='recall_memory')
                self.assertIn('refinement_queries', recall.parameters['properties'] if hasattr(recall, 'parameters') else recall.input_schema['properties'])
                result = await client.call_tool('recall_memory', {'query':'Which controls govern secure connections?',
                       'refinement_queries':['cipher suite','certificate renewal'], 'conversation_id':'synthetic', 'turn_id':'one'})
                self.assertFalse(result.is_error, result)
                data = result.structured_content
                self.assertEqual(len(data['matches']), 2)
                self.assertIn('AES256', data['memory_text'])
                self.assertIn('thirty days', data['memory_text'])
                self.assertEqual(data['query_plan']['lexical_queries'], 3)
                invalid = await client.call_tool('recall_memory', {'query':'test', 'refinement_queries':['a']*4})
                self.assertTrue(invalid.is_error)


if __name__ == '__main__':
    unittest.main()
