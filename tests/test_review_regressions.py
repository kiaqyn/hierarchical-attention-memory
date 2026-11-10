"""Public regressions for the 2026-10-31 review; only isolated fake data."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hook import HookIntegrationTests
from validate_hfa import hfa, invoke, snapshot


class ReviewRegressions(HookIntegrationTests):
    def payload(self, **extra):
        return {"conversationId":"regression", "turnId":"turn-1", "workspacePath":str(self.workspace),
                "decision":{"title":"Export decision", "hit_words":["export-safe"],
                            "conclusion":"Use UTF-8 for CSV output."}, **extra}

    def fire(self, payload, *args, env=None):
        proc = self.run_hook(json.dumps(payload, ensure_ascii=False), list(args), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc, json.loads(proc.stdout)

    def assert_secret_absent(self, secret, output):
        self.assertNotIn(secret.lower(), output.lower())
        for path in self.mem.rglob("*"):
            if path.is_file():
                # Covers L1, derived JSON/SQLite, summaries, stream and journals.
                self.assertNotIn(secret.lower().encode(), path.read_bytes().lower(), str(path))

    def test_plaintext_credential_not_reintroduced_through_keywords(self):
        fake = 'sk-FAKEFORTEST0123456789012345'
        proc, result = self.fire(self.payload(decision=None, memoryWriteAllowed=True,
            lastUserInput='修复 app.py', lastAssistantResponse=f'结论：已在 app.py 修复编码问题。\n验证结果：凭据 {fake} 已脱敏。'), '--phase','post')
        self.assertIn('saved', result)
        self.assert_secret_absent(fake, proc.stdout + proc.stderr)

    def test_structured_all_fields_and_multiline_keys_are_redacted(self):
        fake = 'sk-FAKEFORTEST0123456789012345'
        key_body = 'PRIVATEKEYBODYFAKE1234567890'
        pem = '-----BEGIN PRIVATE KEY-----\n' + key_body + '\n-----END PRIVATE KEY-----'
        proc, result = self.fire(self.payload(conversationId=fake, turnId=fake, decision={
            'title':f'Export {fake}', 'hit_words':['export-safe',fake],
            'conclusion':['Use UTF-8.',pem,'password="FAKE PASSWORD123"'],
            'source':fake, 'request_id':fake}), '--phase','post')
        self.assertIn('saved', result)
        for secret in (fake,key_body,'PASSWORD123'):
            self.assert_secret_absent(secret, proc.stdout+proc.stderr)
        proc, result = self.fire(self.payload(decision={
            'title':'Access rule', 'hit_words':'access-rule, Bearer FAKE1234/BEARER5678',
            'conclusion':'Use the project credential store.'}), '--phase','post')
        self.assertIn('saved', result)
        self.assert_secret_absent('BEARER5678', proc.stdout+proc.stderr)

    def test_explicit_root_never_falls_back_to_existing_workspace(self):
        chosen = self.workspace/'project-a'/'memory'
        before = snapshot(self.mem)
        proc,result = self.fire(self.payload(), '--phase','post','--memory-dir',str(chosen),
                                env={'HFA_MEMORY_DIR':str(self.mem)})
        self.assertIn('saved',result)
        self.assertTrue((chosen/'chunks/chunk_0001.json').is_file())
        self.assertEqual(snapshot(self.mem),before)

    def test_broken_explicit_root_refuses_write_instead_of_switching_projects(self):
        chosen = self.workspace/'broken';chosen.mkdir();(chosen/'notes.txt').write_text('user files')
        before = snapshot(self.mem)
        proc,result = self.fire(self.payload(), '--phase','post','--memory-dir',str(chosen))
        self.assertEqual(result,{})
        self.assertIn('no fallback',proc.stderr)
        self.assertEqual(snapshot(self.mem),before)

    def test_environment_root_is_binding_and_multiworkspace_is_ambiguous(self):
        chosen = self.workspace/'env-memory'
        before = snapshot(self.mem)
        _,result = self.fire(self.payload(), '--phase','post',env={'HFA_MEMORY_DIR':str(chosen)})
        self.assertIn('saved',result)
        self.assertTrue((chosen/'chunks/chunk_0001.json').is_file())
        _,result = self.fire(self.payload(workspacePaths=[str(self.workspace),str(chosen)]),
                            '--phase','post',env={'HFA_MEMORY_DIR':''})
        self.assertEqual(result,{})
        self.assertEqual(snapshot(self.mem),before)

    def transcript(self, records):
        path=self.workspace/'chat.jsonl'
        path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf8')
        return path

    def test_transcript_only_stop_saves_and_retries_without_duplication(self):
        text='```hfa-memory\n'+json.dumps(self.payload()['decision'])+'\n```'
        path=self.transcript([{'type':'USER_INPUT','content':'保存这次明确决定。'},
                             {'type':'PLANNER_RESPONSE','step_index':12,'content':text}])
        payload={'conversationId':'transcript', 'transcriptPath':str(path), 'terminationReason':'completed'}
        for _ in range(2):
            _,result=self.fire(payload,'--phase','stop','--memory-dir',str(self.mem))
            self.assertEqual(result,{'decision':'allow'})
        self.assertTrue((self.mem/'chunks/chunk_0002.json').is_file())
        self.assertEqual(len(list((self.mem/'chunks').glob('*.json'))),2)

    def test_transcript_no_save_and_stale_assistant_response_are_blocked(self):
        response={'type':'PLANNER_RESPONSE','step_index':12,
                  'content':'```hfa-memory\n'+json.dumps(self.payload()['decision'])+'\n```'}
        for records in ([{'type':'USER_INPUT','content':'这次内容不要保存到长期记忆。'},response],
                        [response,{'type':'USER_INPUT','content':'现在开始新的一轮。'}]):
            path=self.transcript(records)
            _,result=self.fire({'transcriptPath':str(path),'terminationReason':'completed'},
                               '--phase','stop','--memory-dir',str(self.mem))
            self.assertEqual(result,{'decision':'allow'})
            self.assertFalse((self.mem/'chunks/chunk_0002.json').exists())

    def test_no_save_overrides_structured_and_opted_in_prose_without_auto_init(self):
        fresh=self.workspace/'fresh'
        for prompt in ('这次内容不要保存到长期记忆。',"Don't save this to memory.",'请勿记录这段内容。'):
            for structured in (self.payload()['decision'],None):
                _,result=self.fire(self.payload(workspacePath=str(fresh),lastUserInput=prompt,
                    memoryWriteAllowed=True,decision=structured,lastAssistantResponse='结论：已修复 app.py 的超时问题。'),
                    '--phase','post','--memory-dir',str(fresh/'memory'))
                self.assertEqual(result,{})
        self.assertFalse(fresh.exists())

    def test_host_deny_and_invalid_permission_values_override_explicit_decisions(self):
        for value in (False,'true','false',0,1,None):
            _,result=self.fire(self.payload(memoryWriteAllowed=value),'--phase','post')
            self.assertEqual(result,{})
        self.assertFalse((self.mem/'chunks/chunk_0002.json').exists())

    def test_prose_requires_opt_in_and_examples_are_never_saved(self):
        for text in ('结论：已修复 app.py 的超时问题。',):
            _,result=self.fire(self.payload(decision=None,lastAssistantResponse=text),'--phase','post')
            self.assertEqual(result,{})
        for text in ('假设：已修复 app.py 的超时问题，测试通过。仅用于举例，未执行。',
                     'The bug in app.py has never been fixed; this is only a proposed solution.'):
            _,result=self.fire(self.payload(decision=None,memoryWriteAllowed=True,lastAssistantResponse=text),'--phase','post')
            self.assertEqual(result,{})
        self.assertFalse((self.mem/'chunks/chunk_0002.json').exists())
        # Words such as 示例 outside the fence do not reject the fenced fields.
        example='示例：\n```hfa-memory\n'+json.dumps(self.payload()['decision'])+'\n```'
        _,result=self.fire(self.payload(decision=None,lastAssistantResponse=example),'--phase','post')
        self.assertEqual(result['saved']['id'],'chunk_0002')

    def test_missing_transcript_and_failed_write_emit_sanitized_diagnostics(self):
        proc,result=self.fire({'transcriptPath':str(self.workspace/'missing'),'terminationReason':'completed'},
                              '--phase','stop','--memory-dir',str(self.mem))
        self.assertEqual(result,{'decision':'allow'})
        self.assertIn('Transcript unavailable',proc.stderr)
        (self.mem/'config.json').write_text('[]',encoding='utf8')
        proc,result=self.fire(self.payload(),'--phase','post')
        self.assertEqual(result,{})
        self.assertIn('Memory write failed',proc.stderr)
        self.assertNotIn('Traceback',proc.stderr)

    def test_explicit_conditional_rule_is_not_mistaken_for_hypothetical_work(self):
        _,result=self.fire(self.payload(decision={
            'title':'Recovery rule', 'hit_words':['recovery-rule'],
            'conclusion':'如果发布失败，先回滚，再定位根因。'}),'--phase','post')
        self.assertIn('saved',result)

    def test_byte_bound_blocks_underestimated_rare_char_payloads(self):
        before=snapshot(self.mem)
        with patch.object(hfa,'_encoder',return_value=None):
            with self.assertRaises(hfa.MemoryError):
                invoke(hfa.cmd_add_topic,self.mem,'Rare text','rare-text','龘'*300)
        self.assertEqual(snapshot(self.mem),before)
        # Already stored by a different tokenizer/configuration: recall must defer.
        config=self.mem/'config.json';value=json.loads(config.read_text(encoding='utf8'))
        value['recall_token_budget']=2000;config.write_text(json.dumps(value),encoding='utf8')
        invoke(hfa.cmd_add_topic,self.mem,'Rare text','rare-text','龘'*300)
        value['recall_token_budget']=600;config.write_text(json.dumps(value),encoding='utf8')
        invoke(hfa.cmd_reindex,self.mem)
        with patch.object(hfa,'_encoder',return_value=None):
            _,output=invoke(hfa.cmd_route,self.mem,'rare-text')
        result=json.loads(output)
        self.assertEqual(result['selected'],[])
        self.assertTrue(result['deferred'])

    def test_route_queries_ondisk_fts_without_rebuilding_sqlite(self):
        invoke(hfa.cmd_add_topic,self.mem,'On-disk FTS','ondisk-fts','中文乱码已通过 UTF-8 修复，CSV 导出必须指定编码。')
        hfa._DERIVED_CACHE.clear()
        hfa._build_fts_cached.cache_clear()
        with patch.object(hfa,'_build_fts_cached',side_effect=AssertionError('cmd_route must not rebuild SQLite FTS5 index')):
            _,output=invoke(hfa.cmd_route,self.mem,'输出文件全是看不懂的符号')
        result=json.loads(output)
        self.assertEqual(result['selected'][0]['id'],'chunk_0002')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ReviewRegressions(name) for name in ReviewRegressions.__dict__ if name.startswith('test_'))

if __name__=='__main__':
    unittest.main(verbosity=2)

