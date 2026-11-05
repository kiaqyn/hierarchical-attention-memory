"""Worker boundary, recovery and warm-session tests; all memory is synthetic."""
import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from validate_hfa import ROOT, hfa, invoke
from test_semantic_service import vector, IDENTITY

spec = importlib.util.spec_from_file_location('worker_test_module', ROOT/'scripts/hfa_worker.py')
worker_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker_module)
sem = hfa._semantic_module()
MODEL = os.environ.get('HFA_TEST_SEMANTIC_MODEL')

class SessionChecks(unittest.TestCase):
    def test_single_encoder_reused_and_failure_evicts_it(self):
        class Encoder:
            def __init__(self, _): pass
            def encode(self, texts, kind):
                if texts == ['fail']: raise ValueError('synthetic failure')
                return [vector(0) for _ in texts]
        with patch.object(sem, 'LocalEncoder', Encoder):
            session = sem.EncoderSession('fake')
            session.encode(['first'], 'passage')
            session.encode(['second'], 'query')
            self.assertEqual(session.loads, 1)
            with self.assertRaisesRegex(ValueError, 'synthetic'): session.encode(['fail'])
            self.assertFalse(session.loaded)
            session.encode(['recover'])
            self.assertEqual(session.loads, 2)

    def test_warm_recall_revalidates_model_bytes_and_produces_no_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'memory'
            invoke(hfa.cmd_init, root)
            session = sem.EncoderSession('fake')
            class Encoder:
                def __init__(self, _): pass
                def encode(self, texts, kind): return [vector(0) for _ in texts]
            with patch.object(sem, 'LocalEncoder', Encoder), patch.object(sem, 'model_identity', return_value=IDENTITY) as identity:
                def call():
                    return hfa.cmd_route(root, 'test', mode='semantic', semantic_model=Path('fake'),
                                         semantic_runtime=session, return_result=True)
                output=io.StringIO()
                with contextlib.redirect_stdout(output):
                    first, second=call(), call()
                self.assertEqual(output.getvalue(), '')
                self.assertFalse(first['retrieval']['encoder_reused'])
                self.assertTrue(second['retrieval']['encoder_reused'])
                self.assertEqual(second['retrieval']['encoder_loads'], 1)
                self.assertEqual(identity.call_count, 2)
                before=(root/'history/session-state.json').read_bytes()
                identity.side_effect=ValueError('Semantic model checksum mismatch')
                with self.assertRaisesRegex(ValueError, 'checksum'): call()
                self.assertEqual(before,(root/'history/session-state.json').read_bytes())
                with self.assertRaisesRegex(hfa.MemoryError, 'different model'):
                    identity.side_effect=None
                    hfa.cmd_route(root,'test',mode='semantic',semantic_model=Path('other'),semantic_runtime=session)


# Real subprocesses with deterministic fault injection, never production test flags.
ECHO = '''import json,sys,time,os
for line in sys.stdin:
    r=json.loads(line); q=r['arguments']['query']
    if q=='hang': time.sleep(10)
    if q=='crash': os._exit(7)
    if q=='bad': print('{not-json',flush=True); continue
    if q=='wrong-id': r['id']+=1
    if q=='error': print(json.dumps({'id':r['id'],'ok':False,'error':'synthetic error'}),flush=True); continue
    print(json.dumps({'id':r['id'],'ok':True,'result':{'query':q}}),flush=True)
'''

class EchoWorker(worker_module.RecallWorker):
    def _start(self):
        self._process=subprocess.Popen([sys.executable,'-u','-c',ECHO],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)

class WorkerChecks(unittest.TestCase):
    def setUp(self):
        self.worker=EchoWorker('.', '.', timeout=3)
        self.addCleanup(self.worker.close)

    def test_concurrent_requests_are_serial_and_do_not_cross_responses(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda i:self.worker.recall(query='q'+str(i)),range(15)))
        self.assertEqual([r['query'] for r in results],['q'+str(i) for i in range(15)])
        child=self.worker._process
        self.worker.close()
        self.assertIsNotNone(child.poll())
        with self.assertRaisesRegex(worker_module.WorkerError,'closed'):self.worker.recall(query='x')

    def test_timeout_kills_child_then_next_request_starts_cleanly(self):
        self.worker.recall(query='ready')
        child=self.worker._process
        self.worker.timeout=.1
        with self.assertRaisesRegex(worker_module.WorkerError,'SAME'):self.worker.recall(query='hang')
        self.assertIsNotNone(child.poll())
        self.worker.timeout=3
        self.assertEqual(self.worker.recall(query='recovered')['query'],'recovered')
        self.assertIsNot(child,self.worker._process)

    def test_crash_invalid_json_and_mismatched_id_fail_without_auto_replay(self):
        for query in ('crash','bad','wrong-id'):
            with self.subTest(query=query):
                with self.assertRaises(worker_module.WorkerError):self.worker.recall(query=query)
                self.assertIsNone(self.worker._process)
                self.assertEqual(self.worker.recall(query='recovered')['query'],'recovered')

    def test_expected_error_is_not_zero_hit_or_worker_restart(self):
        self.worker.recall(query='ready')
        child=self.worker._process
        with self.assertRaisesRegex(worker_module.WorkerError,'synthetic error'):self.worker.recall(query='error')
        self.assertEqual(self.worker.recall(query='recovered')['query'],'recovered')
        self.assertIs(self.worker._process,child)

    def test_bound_paths_options_and_oversized_requests_cannot_be_overridden(self):
        for extra in ({'root':'outside'},{'mode':'keyword'},{'semantic_min_score':-1},{'command':'add-topic'}, {'query':' '*5}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.worker.recall(**{'query':'q',**extra})
        with self.assertRaises(worker_module.WorkerError):self.worker.recall(query='q',unlock='a'*worker_module.MAX_REQUEST)
        self.assertIsNone(self.worker._process)

    def test_expired_queue_does_not_kill_or_send_to_active_worker(self):
        self.worker.recall(query='ready')
        child=self.worker._process
        self.worker.timeout=.03
        with self.worker._lock:
            with self.assertRaisesRegex(worker_module.WorkerError,'no request'):self.worker.recall(query='not-sent')
        self.assertIsNone(child.poll())


@unittest.skipUnless(MODEL,'explicit local model required')
class ActualWorkerChecks(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','actual Windows sharing semantics required')
    def test_warm_recall_succeeds_while_unchanged_cache_has_a_shared_reader(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory(prefix='hfa-worker-readonly-cache-') as temp:
            root=Path(temp)/'memory';invoke(hfa.cmd_init,root)
            invoke(hfa.cmd_add_topic,root,'字体决定','font-fixture','安装缺失字体包。CACHE_SHARED_731')
            worker=worker_module.RecallWorker(root,MODEL)
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
            kernel.CreateFileW.restype=wintypes.HANDLE
            kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
            handle=None
            try:
                args={'query':'font-fixture','conversation_id':'fixture','turn_id':'same'}
                cold=worker.recall(**args)
                path=root/'index/semantic-M1.json'
                before=path.read_bytes()
                handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0,None)
                self.assertNotIn(handle,(None,ctypes.c_void_p(-1).value),ctypes.get_last_error())
                warm=worker.recall(**args)
                self.assertEqual(warm['context_text'],cold['context_text'])
                self.assertIn('CACHE_SHARED_731',warm['context_text'])
                self.assertEqual(warm['recall_count'],cold['recall_count']+1)
                self.assertEqual(path.read_bytes(),before)
            finally:
                if handle not in (None,ctypes.c_void_p(-1).value):kernel.CloseHandle(handle)
                worker.close()

    def test_real_model_warm_cache_restart_and_external_updates(self):
        with tempfile.TemporaryDirectory(prefix='hfa-worker-real-') as temp:
            root=Path(temp)/'synthetic'
            invoke(hfa.cmd_init,root)
            def add(text, supersedes=None):
                _,result=invoke(hfa.cmd_add_topic,root,'字体决定','fixture-font',text,None,None,'synthetic:worker',supersedes,None,True)
                return json.loads(result)['id']
            old=add('中文显示方框时安装缺失字体。SYNTHETIC_WARM_1')
            worker=worker_module.RecallWorker(root,MODEL)
            try:
                args={'query':'fixture-font','conversation_id':'test','turn_id':'same'}
                first=worker.recall(**args)
                child=worker._process
                second=worker.recall(**args)
                self.assertIs(worker._process,child)
                self.assertEqual(second['retrieval']['encoder_loads'],1)
                self.assertTrue(second['retrieval']['encoder_reused'])
                self.assertEqual(first['turn_count'],second['turn_count'])
                new=add('更新为安装字体包。SYNTHETIC_WARM_2',[old])
                latest=worker.recall(**args)
                self.assertEqual([s['id'] for s in latest['selected']],[new])
                self.assertNotIn('SYNTHETIC_WARM_1',latest['context_text'])
                self.assertEqual(latest['retrieval']['encoded_documents'],1)
                self.assertEqual(latest['retrieval']['encoder_loads'],1)
                # Kill only this test-owned worker, then retry the same real turn.
                child.kill();child.wait(timeout=5)
                restarted=worker.recall(**args)
                self.assertFalse(restarted['retrieval']['encoder_reused'])
                self.assertEqual(restarted['retrieval']['encoded_documents'],0)
                self.assertEqual(restarted['turn_count'],latest['turn_count'])
                self.assertEqual([s['id'] for s in restarted['selected']],[new])
            finally:
                child=worker._process
                worker.close()
                if child is not None:self.assertIsNotNone(child.poll())


if __name__=='__main__':unittest.main()
