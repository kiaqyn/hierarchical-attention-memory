"""Public host-contract fixtures, not captured private chats or model-use proof."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent.parent

class NativePreHookTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hfa-native-pre-')
        self.workspace=Path(self.temp.name).resolve()
        self.memory=self.workspace/'memory'
        self.transcript=self.workspace/'transcript.jsonl'
        proc=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/hfa_manager.py'),'init',str(self.memory)],capture_output=True,encoding='utf8')
        self.assertEqual(proc.returncode,0,proc.stderr)
        p=self.memory/'summary.md'
        p.write_text(p.read_text(encoding='utf8')+'\n- NATIVE_PRE_ONLY_SUMMARY_RULE_813\n',encoding='utf8')

    def tearDown(self): self.temp.cleanup()

    def write(self,records):
        self.transcript.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in records),encoding='utf8')

    def fire(self,**extra):
        payload={'invocationNum':0,'initialNumSteps':1,'conversationId':'synthetic-host-conversation',
                 'workspacePaths':[str(self.workspace)],'transcriptPath':str(self.transcript),
                 'artifactDirectoryPath':str(self.workspace),'modelName':'synthetic-no-model-call',**extra}
        p=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/hfa_hook.py'),'--phase','pre'],
                         input=json.dumps(payload),capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(p.returncode,0,p.stderr)
        return json.loads(p.stdout),p.stderr

    def ledger(self):
        return json.loads((self.memory/'history/session-state.json').read_text(encoding='utf8'))

    def test_documented_metadata_only_payload_delivers_core_summary(self):
        self.write([{'type':'USER_INPUT','step_index':0,'content':'之前的偏好是什么？'}])
        out,_=self.fire()
        self.assertIn('NATIVE_PRE_ONLY_SUMMARY_RULE_813',out['injectSteps'][0]['ephemeralMessage'])

    def test_model_invocations_within_same_user_turn_are_not_new_turns(self):
        self.write([{'type':'USER_INPUT','step_index':0,'content':'之前的稀有术语是什么？'},
                    {'type':'PLANNER_RESPONSE','step_index':1,'content':'checking tools'}])
        first,_=self.fire(invocationNum=0)
        second,_=self.fire(invocationNum=1,initialNumSteps=2)
        session=next(iter(self.ledger()['conversations'].values()))
        self.assertEqual(session['count'],1)
        self.assertTrue('ONE follow-up' in first['injectSteps'][0]['ephemeralMessage'])
        self.assertFalse('ONE follow-up' in second['injectSteps'][0]['ephemeralMessage'])

    def test_repeated_text_in_new_user_step_is_a_new_turn(self):
        user={'type':'USER_INPUT','step_index':0,'content':'之前的偏好是什么？'}
        self.write([user]);self.fire()
        self.write([user,{'type':'USER_INPUT','step_index':8,'content':user['content']}]);self.fire(invocationNum=3)
        self.assertEqual(next(iter(self.ledger()['conversations'].values()))['count'],2)

    def test_explicit_input_and_turn_id_override_stale_transcript(self):
        self.write([{'type':'USER_INPUT','step_index':0,'content':'older unrelated prompt'}])
        self.fire(lastUserInput='today direct input',turnId='explicit-turn')
        self.fire(lastUserInput='today direct input',turnId='explicit-turn',invocationNum=9)
        self.assertEqual(next(iter(self.ledger()['conversations'].values()))['count'],1)

    def test_latest_unsupported_user_does_not_reuse_older_input(self):
        self.write([{'type':'USER_INPUT','step_index':0,'content':'older prompt'},
                    {'type':'USER_INPUT','step_index':3,'content':{'unrecognized':'shape'}}])
        out,stderr=self.fire()
        self.assertEqual(out,{})
        self.assertIn('no older prompt',stderr)
        self.assertFalse((self.memory/'history/session-state.json').exists())

    def test_numeric_host_ids_share_one_turn_including_zero(self):
        self.write([{'type':'USER_INPUT','step_index':4,'content':'older prompt'}])
        first,_=self.fire(conversationId=0, turnId=0, lastUserInput='之前的稀有术语是什么？')
        second,_=self.fire(conversationId=0, turnId=0, lastUserInput='之前的稀有术语是什么？')
        session=next(iter(self.ledger()['conversations'].values()))
        self.assertEqual(session['count'],1)
        self.assertEqual(next(iter(session['turns'].values()))['searches'],2)
        self.assertIn('ONE follow-up', first['injectSteps'][0]['ephemeralMessage'])
        self.assertNotIn('ONE follow-up', second['injectSteps'][0]['ephemeralMessage'])

    def test_boolean_identity_is_not_a_turn_id(self):
        self.write([{'type':'USER_INPUT','step_index':0,'content':'older prompt'}])
        self.fire(conversationId=True, turnId=False, lastUserInput='之前的偏好是什么？')
        ledger=self.ledger()
        self.assertEqual(ledger['conversations'], {})
        self.assertEqual(ledger['recall_count'], 1)

    def test_absent_transcript_does_not_use_invocation_number_as_turn(self):
        out,stderr=self.fire(invocationNum=20)
        self.assertEqual(out,{})
        self.assertIn('Transcript unavailable',stderr)
        self.assertFalse((self.memory/'history/session-state.json').exists())

if __name__=='__main__':unittest.main(verbosity=2)
