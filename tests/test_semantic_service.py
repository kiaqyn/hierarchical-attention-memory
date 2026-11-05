"""Lifecycle, corruption, scope and race checks without inference dependencies."""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from validate_hfa import hfa, invoke, snapshot

sem=hfa._semantic_module()
IDENTITY={'model':'test-only-encoder','revision':'fixture','dimensions':sem.DIMENSIONS}


def vector(index=0):
    result=[0.0]*sem.DIMENSIONS
    result[index]=1.0
    return result


class CacheChecks(unittest.TestCase):
    def state(self,docs):
        return sem.snapshot(Path('synthetic'),docs,'M1')

    def doc(self,key='a',text='old',project='M1',status='ACTIVE',supersedes=None):
        return {'id':key,'title':text,'summary':['source'],'project':project,'status':status,'supersedes':supersedes or []}

    def test_incremental_add_edit_remove_and_reuse(self):
        encoded=[]
        def encode(texts):
            encoded.extend(texts)
            return [vector() for _ in texts]
        docs=[self.doc()]
        cache,first=sem.prepare_cache(self.state(docs),None,IDENTITY,encode)
        self.assertEqual(first['encoded_documents'],1)
        original=copy.deepcopy(cache)
        warm,info=sem.prepare_cache(self.state(docs),cache,IDENTITY,encode)
        self.assertEqual(info['cache_status'],'reused')
        self.assertEqual(info['encoded_documents'],0)
        self.assertEqual(warm,original)
        docs.append(self.doc('b','new'))
        added,info=sem.prepare_cache(self.state(docs),cache,IDENTITY,encode)
        self.assertEqual(info['encoded_documents'],1)
        docs[0]['summary']=['changed']
        changed,info=sem.prepare_cache(self.state(docs),added,IDENTITY,encode)
        self.assertEqual(info['encoded_documents'],1)
        removed,info=sem.prepare_cache(self.state(docs[1:]),changed,IDENTITY,encode)
        self.assertEqual(info['removed_documents'],1)
        self.assertEqual(set(removed['rows']),{'b'})
        self.assertEqual(cache,original)

    def test_filter_scope_archive_and_supersession_before_encoding(self):
        docs=[self.doc('old'),self.doc('new','new',supersedes=['old']),self.doc('side','PRIVATE','S1'),self.doc('arch','ARCHIVE',status='ARCHIVED')]
        state=self.state(docs)
        self.assertEqual([x['id'] for x in state['documents']],['new'])
        cache,info=sem.prepare_cache(state,None,IDENTITY,lambda texts:[vector() for _ in texts])
        self.assertEqual(set(cache['rows']),{'new'})

    def test_corrupt_checksum_and_nonfinite_or_wrong_shape_rebuild(self):
        state=self.state([self.doc()])
        cache,_=sem.prepare_cache(state,None,IDENTITY,lambda texts:[vector()])
        for bad in ({'broken':True},{**cache,'checksum':'bad'},None):
            new,info=sem.prepare_cache(state,bad,IDENTITY,lambda texts:[vector()])
            self.assertTrue(sem.cache_consistent(new,state,IDENTITY))
            self.assertEqual(info['encoded_documents'],1)
        bad=copy.deepcopy(cache)
        bad['rows']['a']['vector']=[float('nan')]*sem.DIMENSIONS
        self.assertFalse(sem.cache_consistent(bad,state,IDENTITY))
        with self.assertRaises(ValueError):sem.prepare_cache(state,None,IDENTITY,lambda texts:[[0.0]])
        with self.assertRaises(ValueError):sem.prepare_cache(state,None,IDENTITY,lambda texts:[])

    def test_model_and_root_identity_changes_cannot_reuse_vectors(self):
        state=self.state([self.doc()])
        cache,_=sem.prepare_cache(state,None,IDENTITY,lambda texts:[vector()])
        changed,info=sem.prepare_cache(state,cache,{**IDENTITY,'revision':'other'},lambda texts:[vector(1)])
        self.assertEqual(info['encoded_documents'],1)
        state['scope']='other-root'
        _,info=sem.prepare_cache(state,cache,IDENTITY,lambda texts:[vector()])
        self.assertEqual(info['cache_status'],'rebuilt_identity_or_scope')

    def test_score_threshold_margin_and_invalid_numbers(self):
        state=self.state([self.doc(),self.doc('b')])
        cache,_=sem.prepare_cache(state,None,IDENTITY,lambda texts:[vector(),vector()])
        self.assertEqual(sem.rank(cache,vector(),.9,.1),[])
        self.assertEqual(len(sem.rank(cache,vector(),.9,0)),2)
        for value in (float('nan'),float('inf'),True,2):
            with self.assertRaises(ValueError):sem.rank(cache,vector(),value)
        with self.assertRaises(ValueError):sem.rank(cache,vector(),limit=True)

    def test_model_manifest_cannot_redefine_trusted_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'manifest.json').write_text(json.dumps({'model':sem.MODEL,'revision':sem.REVISION}),encoding='utf8')
            (root/'model.onnx').write_bytes(b'untrusted')
            with self.assertRaisesRegex(ValueError,'checksum'):sem.model_identity(root)


class ServiceChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hfa-semantic-service-')
        self.root=Path(self.temp.name)/'memory'
        invoke(hfa.cmd_init,self.root)
        self.passages=[]
        owner=self
        class Encoder:
            def __init__(self,*args):pass
            def encode(self,texts,kind='query'):
                if kind=='passage':owner.passages.extend(texts)
                return [vector(0 if ('MARIGOLD' in text or kind=='query') else 1) for text in texts]
        self.mocks=[patch.object(sem,'model_identity',return_value=IDENTITY),patch.object(sem,'LocalEncoder',Encoder)]
        for mock in self.mocks:mock.start()

    def tearDown(self):
        for mock in reversed(self.mocks):mock.stop()
        self.temp.cleanup()

    def add(self,text='MARIGOLD old decision',supersedes=None):
        rc,out=invoke(hfa.cmd_add_topic,self.root,'fixture','opaque-ticket',text,None,None,'synthetic:test',supersedes,None,True)
        self.assertEqual(rc,0)
        return json.loads(out)['id']

    def recall(self):
        rc,out=invoke(hfa.cmd_route,self.root,'unmatched paraphrase',None,None,'semantic',None,None,True,Path('fake'))
        self.assertEqual(rc,0)
        return json.loads(out)

    def test_recall_refreshes_after_add_supersede_and_edit_without_cache_command(self):
        old=self.add()
        first=self.recall()
        self.assertEqual(first['selected'][0]['id'],old)
        self.assertEqual(first['selected'][0]['match']['method'],'semantic')
        second=self.recall()
        self.assertEqual(second['retrieval']['encoded_documents'],0)
        new=self.add('MARIGOLD new decision',[old])
        third=self.recall()
        self.assertEqual(third['selected'][0]['id'],new)
        self.assertNotIn(old,(self.root/'index/semantic-M1.json').read_text(encoding='utf8'))
        doc=self.root/'chunks'/f'{new}.json'
        data=json.loads(doc.read_text(encoding='utf8'))
        data['summary']=['MARIGOLD edited decision']
        doc.write_text(json.dumps(data),encoding='utf8')
        with self.assertRaisesRegex(hfa.MemoryError,'Stale index'):self.recall()
        invoke(hfa.cmd_reindex,self.root)
        updated=self.recall()
        self.assertIn('edited decision',updated['recall_text'])
        self.assertEqual(updated['retrieval']['encoded_documents'],1)

    def test_archive_and_delete_prune_before_returning_cached_candidates(self):
        key=self.add()
        self.recall()
        path=self.root/'chunks'/f'{key}.json'
        data=json.loads(path.read_text(encoding='utf8'))
        data['status']='ARCHIVED'
        path.write_text(json.dumps(data),encoding='utf8')
        invoke(hfa.cmd_reindex,self.root)
        self.assertFalse(self.recall()['selected'])
        self.assertNotIn(key,json.loads((self.root/'index/semantic-M1.json').read_text(encoding='utf8'))['rows'])
        path.unlink()  # only this test's verified temporary fixture
        invoke(hfa.cmd_reindex,self.root)
        self.assertFalse(self.recall()['selected'])

    def test_corruption_is_reported_as_rebuilt_not_a_silent_miss(self):
        key=self.add()
        self.recall()
        path=self.root/'index/semantic-M1.json'
        path.write_text('{invalid',encoding='utf8')
        result=self.recall()
        self.assertEqual(result['selected'][0]['id'],key)
        self.assertEqual(result['retrieval']['cache_status'],'rebuilt_corrupt')

    def test_changed_truth_during_inference_fails_without_stale_payload_or_turn(self):
        self.add()
        before=snapshot(self.root)
        original=sem.LocalEncoder.encode
        root=self.root
        def concurrent(encoder,texts,kind='query'):
            if kind=='query':
                invoke(hfa.cmd_add_topic,root,'new','new-ticket','MARIGOLD concurrent addition')
            return original(encoder,texts,kind)
        with patch.object(sem.LocalEncoder,'encode',concurrent):
            with self.assertRaisesRegex(hfa.MemoryError,'changed during'):self.recall()
        self.assertFalse((self.root/'index/semantic-M1.json').exists())
        self.assertFalse((self.root/'history/session-state.json').exists())

    def test_no_memory_lock_is_held_during_inference(self):
        self.add()
        original=sem.LocalEncoder.encode
        root=self.root
        def outside(encoder,texts,kind='query'):
            self.assertFalse(hfa._root_lock(root)._is_owned())
            return original(encoder,texts,kind)
        with patch.object(sem.LocalEncoder,'encode',outside):self.recall()

    def test_cache_write_failure_rolls_back_without_damaging_truth(self):
        self.add()
        before=snapshot(self.root)
        original=hfa._atomic_write
        def failure(path,content):
            if path.name=='semantic-M1.json':raise OSError('synthetic disk failure')
            return original(path,content)
        with patch.object(hfa,'_atomic_write',failure):
            with self.assertRaises(OSError):self.recall()
        self.assertEqual(snapshot(self.root),before)
        self.assertFalse((self.root/hfa.JOURNAL).exists())

    def test_verify_does_not_mutate_missing_or_stale_cache(self):
        self.add()
        before=snapshot(self.root)
        rc,out=invoke(hfa.cmd_semantic_index,self.root,Path('fake'),True)
        self.assertEqual(rc,1)
        self.assertEqual(snapshot(self.root),before)
        self.recall()
        before=snapshot(self.root)
        rc,out=invoke(hfa.cmd_semantic_index,self.root,Path('fake'),True)
        self.assertEqual(rc,0)
        self.assertEqual(snapshot(self.root),before)

    def test_unchanged_cache_skips_disk_writes_and_keeps_turn_accounting(self):
        self.add();cold=self.recall()
        writes=[];original=hfa._atomic_write
        def record(path,content):
            writes.append(path.relative_to(self.root).as_posix())
            return original(path,content)
        with patch.object(hfa,'_atomic_write',side_effect=record):warm=self.recall()
        self.assertNotIn('index/semantic-M1.json',writes)
        self.assertIn('history/session-state.json',writes)
        self.assertIn('history/access.jsonl',writes)
        self.assertEqual(warm['context_text'],cold['context_text'])
        self.assertEqual(warm['recall_count'],cold['recall_count']+1)
        self.assertEqual(warm['retrieval']['cache_status'],'reused')
        before=snapshot(self.root)
        with patch.object(hfa,'_atomic_write',side_effect=AssertionError('unnecessary disk write')):
            self.assertEqual(invoke(hfa.cmd_semantic_index,self.root,Path('fake'))[0],0)
        self.assertEqual(snapshot(self.root),before)

    def test_corrupted_or_missing_cache_during_inference_is_repaired_even_if_prepared_reused(self):
        self.add();self.recall()
        path=self.root/'index/semantic-M1.json';original=hfa._publish_semantic_locked
        for corrupt in (b'\xff\xfeinvalid',None):
            def changed(root,prepared,chunks,mainline):
                self.assertEqual(prepared['info']['cache_status'],'reused')
                if corrupt is None:path.unlink()  # This test's private generated cache only.
                else:path.write_bytes(corrupt)
                return original(root,prepared,chunks,mainline)
            with self.subTest(corrupt=corrupt),patch.object(hfa,'_publish_semantic_locked',side_effect=changed):
                result=self.recall()
            self.assertTrue(result['selected'])
            self.assertEqual(invoke(hfa.cmd_semantic_index,self.root,Path('fake'),True)[0],0)

    def test_core_mode_does_not_load_model_or_create_vector_cache(self):
        self.add()
        with patch.object(hfa,'_semantic_module',side_effect=AssertionError('unexpected semantic import')):
            rc,out=invoke(hfa.cmd_route,self.root,'opaque-ticket')
        self.assertEqual(rc,0)
        self.assertFalse(list((self.root/'index').glob('semantic-*')))
        with self.assertRaisesRegex(hfa.MemoryError,'explicit'):invoke(hfa.cmd_route,self.root,'q',None,None,'semantic')

    def test_side_branches_are_not_embedded_even_when_lateral_unlock_is_present(self):
        key=self.add()
        invoke(hfa.cmd_add_topic,self.root,'branch-only','branch-ticket','MARIGOLD PRIVATE_BRANCH',None,'S1')
        result=self.recall()
        self.assertEqual(result['selected'][0]['id'],key)
        self.assertFalse(any('PRIVATE_BRANCH' in text for text in self.passages))
        invoke(hfa.cmd_route,self.root,'unmatched paraphrase',None,'synthetic side inspection','semantic',None,None,True,Path('fake'))
        self.assertFalse(any('PRIVATE_BRANCH' in text for text in self.passages))
        self.passages.clear()
        invoke(hfa.cmd_tombstone,self.root,'S1',None,'synthetic test')
        result=self.recall()
        self.assertFalse(any('PRIVATE_BRANCH' in text for text in self.passages))
        self.assertFalse(result['retrieval']['lateral_semantic_search'])

    def test_runtime_inference_error_is_an_explicit_failure_not_zero_hits(self):
        self.add()
        class Failing:
            def __init__(self,*args):pass
            def encode(self,*args):raise ValueError('Local embedding inference failed')
        with patch.object(sem,'LocalEncoder',Failing):
            with self.assertRaisesRegex(ValueError,'inference failed'):self.recall()
        self.assertFalse((self.root/'history/session-state.json').exists())

    def test_invalid_threshold_is_rejected_even_for_a_negative_only_query(self):
        with self.assertRaises(ValueError):
            invoke(hfa.cmd_route,self.root,'不要查字体',None,None,'semantic',None,None,True,Path('fake'),float('nan'))


if __name__=='__main__':unittest.main()
