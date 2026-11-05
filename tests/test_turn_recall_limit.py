"""Optional persistent per-turn payload limits; all test memory is disposable."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from validate_hfa import ROOT, hfa, invoke
from test_mcp import Client, StdioServerParameters, _open_client

MODEL=os.environ.get('HFA_TEST_SEMANTIC_MODEL')

class TurnLimitChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hfa-turn-cap-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'memory'
        invoke(hfa.cmd_init,self.root)
        invoke(hfa.cmd_add_topic,self.root,'fixture','budget-fixture','UNIQUE_BUDGET_EVIDENCE_731')

    def configure(self,limit):
        path=self.root/'config.json';cfg=json.loads(path.read_text(encoding='utf8'))
        cfg['max_recalls_per_turn']=limit
        path.write_text(json.dumps(cfg),encoding='utf8')
        invoke(hfa.cmd_reindex,self.root)

    def recall(self,turn='one',conv='fixture',**kw):
        return hfa.cmd_route(self.root,'budget-fixture',conversation_id=conv,turn_id=turn,return_result=True,**kw)

    def state(self):
        return {p.name:p.read_bytes() for p in (self.root/'history').glob('*') if p.is_file()}

    def test_default_disabled_needs_no_migration_or_ids_and_adds_no_metadata(self):
        before={p.name:p.read_bytes() for p in (self.root/'index').glob('*')}
        self.assertNotIn('max_recalls_per_turn',hfa._config(self.root))
        for _ in range(3):self.assertNotIn('turn_budget',self.recall(conv=None,turn=None))
        self.assertEqual(before,{p.name:p.read_bytes() for p in (self.root/'index').glob('*')})
        self.configure(0)
        self.assertNotIn('turn_budget',self.recall(conv=None,turn=None,output='compact'))

    def test_two_payloads_then_explicit_limit_error_without_ledger_changes(self):
        self.configure(2)
        first=self.recall();second=self.recall(output='compact')
        self.assertEqual(first['turn_budget'],{'limit':2,'used':1,'remaining':1})
        self.assertEqual(second['turn_budget'],{'limit':2,'used':2,'remaining':0})
        self.assertEqual(first['context_text'],second['memory_text'])
        before=self.state()
        with self.assertRaisesRegex(hfa.RecallLimitError,'do not retry'):self.recall()
        self.assertEqual(before,self.state())
        self.assertEqual(self.recall(turn='two')['turn_budget']['used'],1)
        self.assertEqual(self.recall(conv='another')['turn_budget']['used'],1)

    def test_limit_one_does_not_invite_an_impossible_followup(self):
        self.configure(1)
        result=hfa.cmd_route(self.root,'unknown-zyx',conversation_id='fixture',turn_id='one',return_result=True)
        self.assertTrue(result['zero_hits'])
        self.assertEqual(result['search_feedback'],'')
        self.assertNotIn('3-5 task-related terms',result['context_text'])
        self.assertEqual(result['turn_budget']['remaining'],0)

    def test_enabled_requires_both_real_ids_and_rejects_invalid_limit_types(self):
        self.configure(2)
        for conv,turn in ((None,None),('fixture',None),(None,'one')):
            with self.subTest(conv=conv,turn=turn),self.assertRaisesRegex(hfa.MemoryError,'host-issued'):self.recall(conv=conv,turn=turn)
        self.assertFalse((self.root/'history/session-state.json').exists())
        path=self.root/'config.json';cfg=json.loads(path.read_text(encoding='utf8'))
        for value in (True,False,-1,1.5,'2',None):
            cfg['max_recalls_per_turn']=value;path.write_text(json.dumps(cfg),encoding='utf8')
            with self.subTest(value=value),self.assertRaisesRegex(hfa.MemoryError,'nonnegative integer'):hfa._config(self.root)

    def test_enabling_counts_previous_successes_and_rejects_corrupt_ledger(self):
        for _ in range(3):self.recall()
        self.configure(2)
        with self.assertRaises(hfa.RecallLimitError) as result:self.recall()
        self.assertEqual(result.exception.used,3)
        path=self.root/'history/session-state.json';ledger=json.loads(path.read_text(encoding='utf8'))
        conv=next(iter(ledger['conversations'].values()));turn=next(iter(conv['turns'].values()))
        turn['searches']='0';path.write_text(json.dumps(ledger),encoding='utf8')
        with self.assertRaisesRegex(hfa.MemoryError,'Invalid turn ledger'):self.recall()

    def test_failed_commit_does_not_consume_a_slot(self):
        self.configure(2)
        original=hfa._commit
        def fail(root,updates):
            if 'history/session-state.json' in updates:raise OSError('synthetic ledger commit failure')
            return original(root,updates)
        with patch.object(hfa,'_commit',fail),self.assertRaisesRegex(OSError,'synthetic'):self.recall()
        self.assertFalse((self.root/'history/session-state.json').exists())
        self.assertEqual(self.recall()['turn_budget']['used'],1)

    def test_exhausted_semantic_turn_does_not_prepare_or_hash_model(self):
        self.configure(2);self.recall();self.recall()
        with patch.object(hfa,'_prepare_semantic',side_effect=AssertionError('inference started')) as prepare:
            with self.assertRaises(hfa.RecallLimitError):self.recall(mode='semantic',semantic_model=Path('not-a-model'))
            prepare.assert_not_called()

    def test_cli_processes_share_one_atomic_limit_and_stable_error_code(self):
        self.configure(2)
        command=[sys.executable,'-X','utf8','-B',str(ROOT/'scripts/hfa_manager.py'),'recall',str(self.root),
                 '--query','budget-fixture','--conversation-id','concurrent-fixture','--turn-id','one','--output','compact']
        def run(_):return subprocess.run(command,capture_output=True,encoding='utf8',timeout=30)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(run,range(8)))
        allowed=[r for r in results if r.returncode==0];blocked=[r for r in results if r.returncode!=0]
        self.assertEqual(len(allowed),2,[r.stdout+r.stderr for r in results])
        self.assertEqual(len(blocked),6)
        self.assertEqual(sorted(json.loads(r.stdout)['turn_budget']['used'] for r in allowed),[1,2])
        for response in blocked:
            data=json.loads(response.stdout)
            self.assertEqual(data['code'],'turn_recall_limit')
            self.assertNotIn('UNIQUE_BUDGET_EVIDENCE_731',response.stdout)
        self.assertEqual(len((self.root/'history/access.jsonl').read_text(encoding='utf8').splitlines()),2)


@unittest.skipIf(Client is None,'optional SDK unavailable')
class TurnLimitMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_modes_restart_missing_ids_and_new_real_turn(self):
        modes=[(False,None),(True,None)]+([(True,MODEL)] if MODEL else [])
        for compact,model in modes:
            with self.subTest(compact=compact,semantic=bool(model)),tempfile.TemporaryDirectory(prefix='hfa-limit-stdio-') as temp:
                root=Path(temp)/'memory';invoke(hfa.cmd_init,root)
                path=root/'config.json';cfg=json.loads(path.read_text(encoding='utf8'));cfg['max_recalls_per_turn']=2
                path.write_text(json.dumps(cfg),encoding='utf8');invoke(hfa.cmd_reindex,root)
                argv=['-B',str(ROOT/'scripts/hfa_mcp.py'),'--memory-dir',str(root)]
                if compact:argv+=['--compact']
                if model:argv+=['--semantic-model',model]
                params=StdioServerParameters(command=sys.executable,args=argv,env={'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
                args={'query':'budget-fixture','conversation_id':'case','turn_id':'one'}
                async with _open_client(params) as client:
                    saved=await client.call_tool('remember_decision',{'title':'fixture','hit_words':['budget-fixture'],
                        'conclusion':'UNIQUE_LIMIT_MCP_731','source':'synthetic:limit','request_id':'one'})
                    self.assertFalse(saved.is_error,saved)
                    missing=await client.call_tool('recall_memory',{'query':'budget-fixture'})
                    self.assertTrue(missing.is_error)
                    self.assertFalse((root/'history/session-state.json').exists())
                    for count in (1,2):
                        response=await client.call_tool('recall_memory',args)
                        self.assertFalse(response.is_error,response)
                        self.assertEqual(response.structured_content['turn_budget']['used'],count)
                    blocked=await client.call_tool('recall_memory',args)
                    self.assertTrue(blocked.is_error)
                    self.assertIn('Recall limit reached',blocked.content[0].text)
                    self.assertNotIn('UNIQUE_LIMIT_MCP_731',blocked.content[0].text)
                    next_turn=await client.call_tool('recall_memory',{**args,'turn_id':'two'})
                    self.assertEqual(next_turn.structured_content['turn_budget']['remaining'],1)
                    if model:self.assertTrue(next_turn.structured_content['retrieval']['encoder_reused'])
                    self.assertTrue((await client.call_tool('memory_health',{})).structured_content['healthy'])
                async with _open_client(params) as client:
                    repeated=await client.call_tool('recall_memory',args)
                    self.assertTrue(repeated.is_error)
                    self.assertIn('Recall limit reached',repeated.content[0].text)


if __name__=='__main__':unittest.main()
