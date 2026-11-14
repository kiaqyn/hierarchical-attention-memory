"""Real local model/stdio/wheel checks, explicitly enabled by a synthetic-test model path."""
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
BODY='中文字符变成方框，安装缺失的字体包后恢复显示。测试证据标记 SYNTHETIC_FONT_RULE_731。'
QUERY='中文字符只剩矩形占位符，之前怎样补齐文字？'


@unittest.skipUnless(MODEL,'explicit HFA_TEST_SEMANTIC_MODEL required')
class ActualSemanticTests(unittest.TestCase):
    def cli(self,*args,env=None,cwd=None):
        process=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/hfa_manager.py'),*map(str,args)],
                               cwd=cwd,env=env,capture_output=True,encoding='utf8',timeout=90)
        self.assertEqual(process.returncode,0,process.stdout+'\n'+process.stderr)
        return json.loads(process.stdout) if process.stdout.lstrip().startswith('{') else process.stdout

    def test_actual_model_cli_incremental_refresh_and_corruption_recovery(self):
        with tempfile.TemporaryDirectory(prefix='hfa-real-semantic-') as temp:
            root=Path(temp)/'合成 memory'
            self.cli('init',root)
            saved=self.cli('add-topic',root,'--title','字形恢复','--hit-words','fixture-z91','--conclusion',BODY,'--source','synthetic:font','--json')
            arguments=['recall',root,'--mode','semantic','--semantic-model',MODEL,'--query',QUERY]
            first=self.cli(*arguments)
            self.assertIn(saved['id'],[x['id'] for x in first['selected']])
            self.assertEqual(first['retrieval']['backend'],'local_semantic')
            self.assertEqual(first['retrieval']['encoded_documents'],1)
            semantic_only=self.cli('recall',root,'--mode','semantic','--semantic-model',MODEL,
                 '--semantic-min-score','.80','--query',
                 'How did we get the missing Chinese characters to render instead of square placeholders?')
            self.assertEqual(semantic_only['selected'][0]['id'],saved['id'])
            self.assertEqual(semantic_only['selected'][0]['match']['method'],'semantic')
            before={p.name:p.read_bytes() for p in (root/'chunks').glob('*.json')}
            self.assertEqual(self.cli(*arguments)['retrieval']['encoded_documents'],0)
            newer=self.cli('add-topic',root,'--title','新的字形决定','--hit-words','fixture-z92',
                           '--conclusion',BODY.replace('731','732'),'--source','synthetic:updated',
                           '--supersedes',saved['id'],'--json')
            fresh=self.cli(*arguments)
            self.assertEqual(fresh['selected'][0]['id'],newer['id'])
            self.assertEqual(fresh['retrieval']['encoded_documents'],1)
            self.assertEqual(fresh['retrieval']['encoder_loads'],1)
            self.assertEqual((root/'chunks'/f"{saved['id']}.json").read_bytes(),before[f"{saved['id']}.json"])
            (root/'index/semantic-M1.json').write_text('{corrupt',encoding='utf8')
            self.assertEqual(self.cli(*arguments)['retrieval']['cache_status'],'rebuilt_corrupt')
            self.assertTrue(self.cli('semantic-index',root,'--model-dir',MODEL,'--verify')['consistent'])
            self.assertLessEqual(fresh['context_tokens']['count'],fresh['context_token_budget'])
            self.assertLessEqual(fresh['recall_tokens']['count'],fresh['recall_token_budget'])

    def test_wheel_contains_backend_and_runs_core_without_optional_site_packages(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('wheel_semantic_test',ROOT/'scripts/_build_backend.py')
        backend=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        with tempfile.TemporaryDirectory(prefix='hfa-semantic-wheel-') as temp:
            temp=Path(temp)
            wheel=temp/backend.build_wheel(str(temp))
            extracted=temp/'installed'
            with zipfile.ZipFile(wheel) as package:
                self.assertIn('scripts/hfa_semantic.py',package.namelist())
                self.assertIn('scripts/hfa_worker.py',package.namelist())
                self.assertIn('scripts/hfa_download_model.py',package.namelist())
                metadata=next(x for x in package.namelist() if x.endswith('/METADATA'))
                self.assertIn('Provides-Extra: semantic',package.read(metadata).decode())
                package.extractall(extracted)
            env={**os.environ,'PYTHONPATH':str(extracted),'PYTHONDONTWRITEBYTECODE':'1'}
            memory=temp/'memory'
            for args in (['init',str(memory)],['status',str(memory),'--json']):
                result=subprocess.run([sys.executable,'-S','-B','-m','scripts.hfa_manager',*args],
                                      cwd=temp,env=env,capture_output=True,encoding='utf8',timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            # The same packaged backend performs real inference with installed extras.
            self.cli('add-topic',memory,'--title','字形恢复','--hit-words','fixture-z91','--conclusion',BODY,'--json')
            result=subprocess.run([sys.executable,'-B','-m','scripts.hfa_manager','recall',str(memory),
                                   '--mode','semantic','--semantic-model',MODEL,'--query',QUERY],
                                  cwd=temp,env=env,capture_output=True,encoding='utf8',timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            payload=json.loads(result.stdout)
            self.assertIn('SYNTHETIC_FONT_RULE_731',payload['context_text'])

    def test_installed_download_verifier_uses_no_network_or_writes(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('download_verifier_test',ROOT/'scripts/hfa_download_model.py')
        # Invoke the packaged module entrypoint: verifier never downloads.
        before=(Path(MODEL)/'manifest.json').read_bytes()
        process=subprocess.run([sys.executable,'-B','-m','scripts.hfa_download_model','--output',MODEL,'--verify'],
                               cwd=ROOT,capture_output=True,encoding='utf8',timeout=30)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        self.assertTrue(json.loads(process.stdout)['verified'])
        self.assertEqual((Path(MODEL)/'manifest.json').read_bytes(),before)


@unittest.skipUnless(MODEL and Client is not None,'explicit local model and optional SDK required')
class ActualSemanticMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_discovery_recall_supersession_same_turn_and_restart(self):
        with tempfile.TemporaryDirectory(prefix='hfa-semantic-stdio-') as temp:
            root=Path(temp)/'synthetic memory'
            params=StdioServerParameters(command=sys.executable,args=['-B',str(ROOT/'scripts/hfa_mcp.py'),
                 '--memory-dir',str(root),'--semantic-model',MODEL],
                 env={'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
            async with _open_client(params) as client:
                tools=await client.list_tools()
                self.assertEqual({x.name for x in tools.tools},{'recall_memory','remember_decision','memory_health'})
                saved=await client.call_tool('remember_decision',{'title':'字体决定','hit_words':['fixture-z91'],
                         'conclusion':BODY,'source':'synthetic:stdio','request_id':'one'})
                self.assertFalse(saved.is_error,saved)
                query={'query':QUERY,'conversation_id':'synthetic-one','turn_id':'same-real-turn'}
                found=await client.call_tool('recall_memory',query)
                self.assertFalse(found.is_error,found)
                data=found.structured_content
                self.assertIn('SYNTHETIC_FONT_RULE_731',data['memory_text'])
                self.assertEqual(data['retrieval']['backend'],'local_semantic')
                self.assertFalse(data['retrieval']['encoder_reused'])
                again=(await client.call_tool('recall_memory',query)).structured_content
                self.assertEqual(again['retrieval']['encoded_documents'],0)
                self.assertTrue(again['retrieval']['encoder_reused'])
                self.assertEqual(again['retrieval']['encoder_loads'],1)
                self.assertEqual(again['turn_count'],data['turn_count'])
                updated=await client.call_tool('remember_decision',{'title':'新字体决定','hit_words':['fixture-z92'],
                         'conclusion':BODY.replace('731','732'),'source':'synthetic:stdio-updated',
                         'request_id':'two','supersedes':[saved.structured_content['id']]})
                self.assertFalse(updated.is_error,updated)
                fresh=(await client.call_tool('recall_memory',query)).structured_content
                self.assertIn('SYNTHETIC_FONT_RULE_732',fresh['memory_text'])
                self.assertNotIn('SYNTHETIC_FONT_RULE_731',fresh['memory_text'])
                self.assertEqual(fresh['retrieval']['encoded_documents'],1)
                self.assertEqual(fresh['retrieval']['encoder_loads'],1)
            async with _open_client(params) as client:
                restarted=(await client.call_tool('recall_memory',query)).structured_content
                self.assertIn('SYNTHETIC_FONT_RULE_732',restarted['memory_text'])
                self.assertEqual(restarted['retrieval']['encoded_documents'],0)
                self.assertFalse(restarted['retrieval']['encoder_reused'])


if __name__=='__main__':unittest.main()
