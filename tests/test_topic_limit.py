"""Per-request record limits narrow output without changing scope or directory policy."""
import importlib.util
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
spec=importlib.util.spec_from_file_location('topic_limit_worker',ROOT/'scripts/hfa_worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

class TopicLimitChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hfa-topic-limit-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'memory';hfa.cmd_init(self.root,quiet=True,profile="example")
        invoke(hfa.cmd_add_topic,self.root,'first','topic-fixture','FACT_A')
        invoke(hfa.cmd_add_topic,self.root,'second','topic-fixture','FACT_B')

    def recall(self,**kw):
        return hfa.cmd_route(self.root,'topic-fixture',conversation_id='fixture',turn_id='same',return_result=True,**kw)

    def test_default_and_explicit_limits_preserve_ranking_summary_and_budgets(self):
        default=self.recall();narrow=self.recall(max_topics=1)
        self.assertEqual(len(default['selected']),2)
        self.assertEqual([x['id'] for x in narrow['selected']],[default['selected'][0]['id']])
        self.assertEqual(default['summary_text'],narrow['summary_text'])
        self.assertIn('FACT_B',narrow['context_text']);self.assertNotIn('FACT_A',narrow['recall_text'])
        self.assertEqual(default['context_token_budget'],narrow['context_token_budget'])
        self.assertEqual(default['recall_token_budget'],narrow['recall_token_budget'])
        self.assertLessEqual(narrow['context_tokens']['count'],narrow['context_token_budget'])
        self.assertTrue(any(x['reason']=='recall budget or item limit' for x in narrow['deferred']))

    def test_invalid_or_wider_limit_fails_before_semantic_work_or_ledger_write(self):
        for limit in (0,-1,True,False,1.5,'1',3):
            with self.subTest(limit=limit),patch.object(hfa,'_prepare_semantic',side_effect=AssertionError('unexpected model work')):
                with self.assertRaises(hfa.MemoryError):self.recall(max_topics=limit,mode='semantic',semantic_model=Path('fake'))
        self.assertFalse((self.root/'history/session-state.json').exists())

    def test_narrowing_does_not_unlock_branch_or_mutate_directory_configuration(self):
        invoke(hfa.cmd_add_topic,self.root,'branch','topic-fixture','PRIVATE_BRANCH_FIXTURE',None,'S1')
        before=(self.root/'config.json').read_bytes()
        result=self.recall(max_topics=1)
        self.assertEqual((self.root/'config.json').read_bytes(),before)
        self.assertNotIn('PRIVATE_BRANCH_FIXTURE',result['context_text'])
        self.assertTrue(any(x['reason']=='explicit --unlock reason required' for x in result['deferred']))

    def test_worker_transport_validates_numeric_limit_and_rejects_unknown_scope(self):
        worker.validate_request({'query':'fixture','max_topics':1})
        for limit in (0,-1,True,False,1.5,'1'):
            with self.subTest(limit=limit),self.assertRaises(ValueError):worker.validate_request({'query':'fixture','max_topics':limit})
        with self.assertRaises(ValueError):worker.validate_request({'query':'fixture','memory_dir':'outside'})

    def test_actual_cli_narrows_without_adding_recall_or_changing_real_turn(self):
        args=[sys.executable,'-X','utf8','-B',str(ROOT/'scripts/hfa_manager.py'),'recall',str(self.root),
              '--query','topic-fixture','--conversation-id','fixture','--turn-id','same','--output','compact','--max-topics','1']
        result=subprocess.run(args,capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(len(data['matches']),1)
        self.assertEqual(data['turn_count'],1);self.assertEqual(data['recall_count'],1)
        event=json.loads((self.root/'history/access.jsonl').read_text(encoding='utf8').splitlines()[0])
        self.assertEqual(event['max_topics'],1)

    def test_stdlib_only_cli_and_mcp_config_do_not_require_optional_dependencies(self):
        env={**os.environ,'PYTHONPATH':''}
        result=subprocess.run([sys.executable,'-S','-B',str(ROOT/'scripts/hfa_manager.py'),'recall',str(self.root),
            '--query','topic-fixture','--output','compact','--max-topics','1'],env=env,capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(len(data['matches']),1)
        self.assertFalse(data['payload_tokens']['exact'])
        cfg=subprocess.run([sys.executable,'-S','-B',str(ROOT/'scripts/hfa_mcp.py'),'--memory-dir',str(self.root),'--print-config'],
            env=env,capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(cfg.returncode,0,cfg.stdout+cfg.stderr)


@unittest.skipIf(Client is None,'optional MCP runtime unavailable')
class TopicLimitMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_lexical_and_optional_semantic_validate_and_preserve_limits(self):
        for model in [None]+([MODEL] if MODEL else []):
            with self.subTest(semantic=bool(model)),tempfile.TemporaryDirectory(prefix='hfa-topic-sdk-') as temp:
                root=Path(temp)/'memory';argv=['-B',str(ROOT/'scripts/hfa_mcp.py'),'--memory-dir',str(root),'--compact']
                if model:argv+=['--semantic-model',model]
                env={'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'}
                for key in ('PYTHONPATH','TIKTOKEN_CACHE_DIR'):
                    if os.environ.get(key):env[key]=os.environ[key]
                params=StdioServerParameters(command=sys.executable,args=argv,env=env)
                async with _open_client(params) as client:
                    for i in (1,2):
                        saved=await client.call_tool('remember_decision',{'title':f'fact {i}','hit_words':['topic-fixture'],
                            'conclusion':f'FACT_{i}','source':'synthetic:topic-limit','request_id':str(i)})
                        self.assertFalse(saved.is_error,saved)
                    for invalid in (0,True,1.5,3):
                        rejected=await client.call_tool('recall_memory',{'query':'topic-fixture','max_topics':invalid})
                        self.assertTrue(rejected.is_error,rejected)
                    self.assertFalse((root/'history/session-state.json').exists())
                    args={'query':'topic-fixture','conversation_id':'fixture','turn_id':'same'}
                    normal=(await client.call_tool('recall_memory',args)).structured_content
                    narrow=(await client.call_tool('recall_memory',{**args,'max_topics':1})).structured_content
                    self.assertEqual(len(normal['matches']),2)
                    self.assertEqual(len(narrow['matches']),1)
                    self.assertEqual(normal['matches'][0]['id'],narrow['matches'][0]['id'])
                    self.assertEqual(normal['turn_count'],narrow['turn_count'])
                    self.assertEqual(narrow['recall_count'],2)
                    self.assertIsNone(narrow['delivery']['model_used'])
                    if os.environ.get('TIKTOKEN_CACHE_DIR'):
                        self.assertEqual(narrow['payload_tokens']['method'],'o200k_base')


if __name__=='__main__':unittest.main()
