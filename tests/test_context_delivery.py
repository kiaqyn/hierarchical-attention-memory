"""Real delivery boundaries, corruption, turn identity and rollback using fake data."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from validate_hfa import hfa, invoke, snapshot

ROOT=Path(__file__).resolve().parent.parent

class ContextDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hfa-context-')
        self.mem=Path(self.temp.name).resolve()/'memory'
        invoke(hfa.cmd_init,self.mem)

    def tearDown(self):
        self.temp.cleanup()

    def route(self,turn=None,conv='test-conversation',query='查找之前的稀有概念'):
        rc,out=invoke(hfa.cmd_route,self.mem,query,None,None,'hybrid',conv,turn)
        self.assertEqual(rc,0)
        return json.loads(out)

    def hook(self,query='回忆之前的偏好',turn='one-turn'):
        proc=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/hfa_hook.py'),'--phase','pre',
                             '--memory-dir',str(self.mem)],input=json.dumps({
                                 'conversationId':'hook-session','turnId':turn,'lastUserInput':query}),
                             capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return json.loads(proc.stdout)

    def test_summary_only_rule_is_delivered_once_in_complete_measured_hook_text(self):
        marker='UNIQUE_EXPORT_PREFIX_ZEBRA_427'
        path=self.mem/'summary.md'
        path.write_text(path.read_text(encoding='utf8')+'\n- '+marker+'\n',encoding='utf8')
        out=self.hook()
        text=out['injectSteps'][0]['ephemeralMessage']
        self.assertEqual(text.count(marker),1)
        cli=self.route('second')
        self.assertEqual(cli['context_text'].count(marker),1)
        self.assertEqual(hfa.measure_tokens(cli['context_text']),cli['context_tokens'])
        self.assertLessEqual(cli['context_tokens']['count'],cli['context_token_budget'])
        self.assertIsNone(cli['delivery']['host_accepted'])
        self.assertIsNone(cli['delivery']['model_used'])

    def test_summary_changes_reach_next_turn_without_stale_session_cache(self):
        path=self.mem/'summary.md'
        original=path.read_text(encoding='utf8')
        for rule in ('CURRENT_RULE_ALPHA','CURRENT_RULE_BETA'):
            path.write_text(original+'\n'+rule,encoding='utf8')
            out=self.route(rule)
            self.assertIn(rule,out['context_text'])
            if rule.endswith('BETA'): self.assertNotIn('CURRENT_RULE_ALPHA',out['context_text'])

    def test_twenty_retries_of_one_turn_do_not_trigger_twenty_turn_reminder(self):
        results=[self.route('same-turn') for _ in range(20)]
        self.assertTrue(all(x['turn_count']==1 for x in results))
        self.assertFalse(any(x['status_check_due'] for x in results))
        self.assertTrue(results[0]['search_feedback'])
        self.assertFalse(any(x['search_feedback'] for x in results[1:]))

    def test_twenty_distinct_turns_notify_once_and_sessions_are_separate(self):
        for n in range(1,21):
            out=self.route(str(n))
            self.assertEqual(out['turn_count'],n)
            self.assertEqual(out['status_check_due'],n==20)
        self.assertFalse(self.route('20')['status_check_due'])
        other=self.route('20',conv='other-conversation')
        self.assertEqual(other['turn_count'],1)
        self.assertFalse(other['status_check_due'])

    def test_missing_ids_never_claim_exact_turns_or_offer_unbounded_retries(self):
        out=self.route()
        self.assertIsNone(out['turn_count'])
        self.assertFalse(out['turn_count_exact'])
        self.assertFalse(out['status_check_due'])
        self.assertEqual(out['search_feedback'],'')

    def test_unrelated_hook_prompt_gets_core_summary_without_retry_hint(self):
        out=self.hook('今天晚饭吃什么')
        text=out['injectSteps'][0]['ephemeralMessage']
        self.assertIn('核心记忆摘要',text)
        self.assertNotIn('follow-up recall',text)

    def test_persistent_ledger_deduplicates_concurrent_cli_calls(self):
        cmd=[sys.executable,'-B',str(ROOT/'scripts/hfa_manager.py'),'recall',str(self.mem),
             '--query=unknown-stable','--conversation-id=parallel','--turn-id=one']
        def call(_):
            p=subprocess.run(cmd,capture_output=True,encoding='utf8',timeout=30)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            return json.loads(p.stdout)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs=list(pool.map(call,range(8)))
        self.assertEqual({x['turn_count'] for x in outputs},{1})
        self.assertEqual(sum(bool(x['search_feedback']) for x in outputs),1)

    def test_turn_ledger_and_access_event_roll_back_together(self):
        self.route('one')
        before=snapshot(self.mem)
        original=hfa._atomic_write
        def fail(path,content):
            if path==self.mem/'history/session-state.json': raise OSError('simulated ledger failure')
            return original(path,content)
        with patch.object(hfa,'_atomic_write',side_effect=fail):
            with self.assertRaises(OSError): self.route('two')
        # Complete any recoverable rollback through the normal CLI guard.
        invoke(hfa.cmd_status,self.mem,True)
        self.assertEqual(before,snapshot(self.mem))
        self.assertEqual(self.route('two')['turn_count'],2)

    def test_ids_are_hashed_before_runtime_persistence(self):
        self.route('SYNTHETIC_PRIVATE_TURN',conv='SYNTHETIC_PRIVATE_SESSION')
        for name in ('history/session-state.json','history/access.jsonl'):
            text=(self.mem/name).read_text(encoding='utf8')
            self.assertNotIn('SYNTHETIC_PRIVATE',text)

    def test_same_count_sqlite_corruption_is_rejected_then_rebuilt(self):
        invoke(hfa.cmd_add_topic,self.mem,'Export','export-key','CSV output uses UTF-8.')
        with closing(sqlite3.connect(self.mem/'index/fts.sqlite')) as db:
            db.execute("UPDATE chunks_fts SET heading='',body='' WHERE chunk_id='chunk_0002'")
            db.commit()
        with self.assertRaisesRegex(hfa.MemoryError,'fts.sqlite'):
            self.route('one',query='CSV output')
        invoke(hfa.cmd_reindex,self.mem)
        self.assertEqual(self.route('two',query='CSV output')['selected'][0]['id'],'chunk_0002')

    def test_total_context_cap_defers_snippets_without_silently_dropping_summary(self):
        invoke(hfa.cmd_add_topic,self.mem,'Budget fact','budget-key','VALID_LONG_FACT '+('x'*220))
        out=self.route('probe',query='budget-key')
        summary_and_header=out['context_text'].split('\n\nRetrieved decisions:')[0]
        config=self.mem/'config.json';cfg=json.loads(config.read_text(encoding='utf8'))
        notice='Deferred references: chunk_0002 [budget/item limit]'
        cfg['context_token_budget']=hfa.measure_tokens(summary_and_header)['count']+hfa.measure_tokens(notice)['count']+4
        config.write_text(json.dumps(cfg),encoding='utf8');invoke(hfa.cmd_reindex,self.mem)
        bounded=self.route('bounded',query='budget-key')
        self.assertLessEqual(bounded['context_tokens']['count'],cfg['context_token_budget'])
        self.assertIn('核心记忆摘要',bounded['context_text'])
        self.assertEqual(bounded['selected'],[])
        self.assertTrue(bounded['deferred'])

    def test_impossible_context_budget_fails_without_advancing_turn_state(self):
        config=self.mem/'config.json';cfg=json.loads(config.read_text(encoding='utf8'))
        cfg['context_token_budget']=10
        config.write_text(json.dumps(cfg),encoding='utf8');invoke(hfa.cmd_reindex,self.mem)
        before=snapshot(self.mem)
        with self.assertRaisesRegex(hfa.MemoryError,'context_token_budget'): self.route('one')
        self.assertEqual(before,snapshot(self.mem))

if __name__=='__main__': unittest.main(verbosity=2)
