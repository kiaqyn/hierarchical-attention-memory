"""Preserve evidence and safety while shrinking agent-facing tool envelopes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from validate_hfa import ROOT, hfa, invoke
from test_mcp import Client, StdioServerParameters, _open_client

MODEL=os.environ.get('HFA_TEST_SEMANTIC_MODEL')

class CompactChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hfa-compact-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'memory'
        hfa.cmd_init(self.root,quiet=True,profile="example")
        self.delivery=hfa._delivery_module()

    def add(self,word='quota-fixture',body='UNIQUE_EVIDENCE_731 budget rule',supersedes=None,project=None):
        _,raw=invoke(hfa.cmd_add_topic,self.root,'预算决定',word,body,None,project,'synthetic:evidence:731',supersedes,None,True)
        return json.loads(raw)['id']

    def recall(self,query='quota-fixture',**kw):
        return hfa.cmd_route(self.root,query,conversation_id='case',turn_id='same',return_result=True,**kw)

    def test_complete_evidence_one_copy_pointers_and_accounting_preserved(self):
        key=self.add()
        raw=self.recall();compact=self.delivery.compact_recall(raw)
        self.assertEqual(compact['memory_text'],raw['context_text'])
        self.assertEqual(compact['matches'],[{'id':key,'path':f'chunks/{key}.json'}])
        self.assertEqual(json.dumps(compact,ensure_ascii=False).count('UNIQUE_EVIDENCE_731'),1)
        self.assertIn('synthetic:evidence:731',compact['memory_text'])
        for old,new in [('active_mainline_lock','mainline'),('context_tokens','payload_tokens'),('context_token_budget','payload_budget')]:
            self.assertEqual(raw[old],compact[new])
        for key in ('zero_hits','deferred_total','turn_count','turn_count_exact','recall_count','delivery','status_check_due'):
            if key!='deferred_total':self.assertEqual(raw[key],compact[key])
        self.assertIsNone(compact['delivery']['model_used'])
        self.assertIsNone(compact['delivery']['host_accepted'])
        self.assertNotIn('router_text',compact)
        self.assertNotIn('recall_text',compact)
        self.assertNotIn('summary_text',compact)
        self.assertLess(len(json.dumps(compact,ensure_ascii=False).encode()),len(json.dumps(raw,ensure_ascii=False).encode()))

    def test_miss_then_same_turn_followup_preserves_stop_and_uncertainty(self):
        first=self.recall('unknown-zyx')
        compact=self.delivery.compact_recall(first)
        self.assertTrue(compact['zero_hits'])
        self.assertIn(first['search_feedback'],compact['memory_text'])
        self.assertIn('不代表记录不存在',compact['memory_text'])
        second=self.recall('unknown-zyx',output='compact')
        self.assertEqual(compact['turn_count'],second['turn_count'])
        self.assertEqual(second['recall_count'],2)
        self.assertNotIn('3-5 task-related terms',second['memory_text'])

    def test_periodic_status_prompt_is_kept_inside_complete_text(self):
        config=self.root/'config.json';cfg=json.loads(config.read_text(encoding='utf8'))
        cfg['status_check_interval']=1
        config.write_text(json.dumps(cfg),encoding='utf8')
        invoke(hfa.cmd_reindex,self.root)
        raw=self.recall('unknown-zyx');compact=self.delivery.compact_recall(raw)
        self.assertTrue(compact['status_check_due'])
        self.assertIn(raw['status_check_prompt'],compact['memory_text'])

    def test_budget_deferral_does_not_request_unlock(self):
        self.add('quota-fixture','UNIQUE_A')
        self.add('quota-fixture','UNIQUE_B')
        config=self.root/'config.json';cfg=json.loads(config.read_text(encoding='utf8'))
        cfg['max_recall_topics']=1
        config.write_text(json.dumps(cfg),encoding='utf8')
        invoke(hfa.cmd_reindex,self.root)
        raw=self.recall();compact=self.delivery.compact_recall(raw)
        self.assertIn('budget/item limit',compact['memory_text'])
        self.assertNotIn('--unlock',compact['memory_text'])
        self.assertIn('recall budget',compact['deferred'][0]['reason'])
        self.assertEqual(compact['deferred_total'],len(raw['deferred']))

    def test_branch_and_superseded_evidence_retain_distinct_restrictions(self):
        self.add('side-fixture','SYNTHETIC_SIDE_BODY',project='S1')
        branch=self.recall('side-fixture',output='compact')
        self.assertEqual(branch['matches'],[])
        self.assertNotIn('SYNTHETIC_SIDE_BODY',branch['memory_text'])
        self.assertIn('--unlock required',branch['memory_text'])
        old=self.add('old-rule','OLD_RULE_731')
        self.add('new-rule','NEW_RULE_732',[old])
        historical=self.recall('old-rule',output='compact')
        self.assertIn('historical only',historical['memory_text'])
        self.assertNotIn('OLD_RULE_731',historical['memory_text'])

    def test_bad_format_and_stale_index_do_not_advance_turns(self):
        key=self.add()
        with self.assertRaisesRegex(hfa.MemoryError,'output'):self.recall(output='text')
        self.assertFalse((self.root/'history/session-state.json').exists())
        chunk=self.root/'chunks'/f'{key}.json';data=json.loads(chunk.read_text(encoding='utf8'))
        data['summary']=['Changed truth without reindex'];chunk.write_text(json.dumps(data),encoding='utf8')
        with self.assertRaisesRegex(hfa.MemoryError,'Stale index'):self.recall(output='compact')
        self.assertFalse((self.root/'history/session-state.json').exists())

    def test_actual_cli_full_default_compact_one_write_and_no_sdk_config(self):
        self.add()
        def call(*args):
            p=subprocess.run([sys.executable,'-X','utf8','-B',str(ROOT/'scripts/hfa_manager.py'),'recall',str(self.root),
                '--query=quota-fixture','--conversation-id=case','--turn-id=same',*args],capture_output=True,encoding='utf8',timeout=30)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr);return p.stdout,json.loads(p.stdout)
        full_text,full=call()
        compact_text,compact=call('--output=compact')
        self.assertIn('router_text',full)
        self.assertEqual(full['context_text'],compact['memory_text'])
        self.assertEqual(full['turn_count'],compact['turn_count'])
        self.assertEqual(compact['recall_count'],2)
        self.assertEqual(len((self.root/'history/access.jsonl').read_text(encoding='utf8').splitlines()),2)
        self.assertLess(len(compact_text.encode()),len(full_text.encode()))
        self.assertNotIn('\n',compact_text)
        cfg=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/hfa_mcp.py'),'--memory-dir',str(self.root),
            '--compact','--print-config'],capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(cfg.returncode,0,cfg.stderr)
        self.assertIn('--compact',json.loads(cfg.stdout)['mcpServers']['hfa-project-memory']['args'])

    def test_wheel_compact_core_works_without_optional_site_packages(self):
        spec=importlib.util.spec_from_file_location('compact_build',ROOT/'scripts/_build_backend.py')
        backend=importlib.util.module_from_spec(spec);spec.loader.exec_module(backend)
        wheel=Path(self.temp.name)/backend.build_wheel(self.temp.name)
        installed=Path(self.temp.name)/'installed'
        with zipfile.ZipFile(wheel) as archive:
            self.assertIn('scripts/hfa_delivery.py',archive.namelist());archive.extractall(installed)
        env={**os.environ,'PYTHONPATH':str(installed),'PYTHONDONTWRITEBYTECODE':'1'}
        p=subprocess.run([sys.executable,'-S','-B','-m','scripts.hfa_manager','recall',str(self.root),
            '--query','unknown-zyx','--output','compact'],env=env,cwd=self.temp.name,capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertEqual(json.loads(p.stdout)['status'],'retrieved')


@unittest.skipIf(Client is None,'optional SDK unavailable')
class CompactMCPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_compact_lexical_and_optional_semantic_preserve_behaviors(self):
        modes=[None]+([MODEL] if MODEL else [])
        for model in modes:
            with self.subTest(semantic=bool(model)),tempfile.TemporaryDirectory(prefix='hfa-compact-stdio-') as temp:
                root=Path(temp)/'memory'
                argv=['-B',str(ROOT/'scripts/hfa_mcp.py'),'--memory-dir',str(root),'--compact']
                if model:argv+=['--semantic-model',model]
                params=StdioServerParameters(command=sys.executable,args=argv,env={'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
                async with _open_client(params) as client:
                    self.assertEqual({t.name for t in (await client.list_tools()).tools},{'recall_memory','remember_decision','memory_health'})
                    saved=await client.call_tool('remember_decision',{'title':'quota fixture','hit_words':['quota-fixture'],
                        'conclusion':'UNIQUE_MCP_RULE_731','source':'synthetic:compact','request_id':'one'})
                    self.assertFalse(saved.is_error,saved)
                    args={'query':'quota-fixture','conversation_id':'fixture','turn_id':'same'}
                    first=(await client.call_tool('recall_memory',args)).structured_content
                    second=(await client.call_tool('recall_memory',args)).structured_content
                    self.assertEqual(json.dumps(first,ensure_ascii=False).count('UNIQUE_MCP_RULE_731'),1)
                    self.assertEqual(first['memory_text'],second['memory_text'])
                    self.assertEqual(first['turn_count'],second['turn_count'])
                    self.assertEqual(second['matches'][0]['id'],saved.structured_content['id'])
                    self.assertIsNone(second['delivery']['model_used'])
                    if model:self.assertTrue(second['retrieval']['encoder_reused'])
                    health=(await client.call_tool('memory_health',{})).structured_content
                    self.assertTrue(health['healthy'])
                    before=len((root/'history/access.jsonl').read_text(encoding='utf8').splitlines())
                    blocked=(await client.call_tool('recall_memory',{'query':'画面打转'})).structured_content
                    self.assertEqual(blocked['status'],'clarification_required')
                    self.assertFalse(blocked['recall_executed'])
                    self.assertEqual(before,len((root/'history/access.jsonl').read_text(encoding='utf8').splitlines()))


if __name__=='__main__':unittest.main()
