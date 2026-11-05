"""Measure real MCP SDK results against disposable synthetic memory only.

No host/model/account claim. In particular a host that already consumes only
content does not gain from removing structuredContent. Oracle fixture, not an
autonomous agent or a no-HFA task comparison.
"""
import argparse
import asyncio
import contextlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
MARKER='MCP_SINGLE_COPY_731'
MODES=('full','compact','full_text','compact_text')


async def run():
    import tiktoken
    from mcp import Client,StdioServerParameters
    try:
        from mcp.client.stdio import stdio_client
    except ImportError:
        stdio_client=None
    with patch.object(socket,'getaddrinfo',side_effect=OSError('prepare tokenizer cache first')),patch.object(socket.socket,'connect',side_effect=OSError('offline')):
        encoder=tiktoken.get_encoding('o200k_base')
    count=lambda text:len(encoder.encode(text,disallowed_special=()))
    spec=importlib.util.spec_from_file_location('transport_hfa',ROOT/'scripts/hfa_manager.py')
    hfa=importlib.util.module_from_spec(spec);spec.loader.exec_module(hfa)
    result={'scope':__doc__,'sdk_version':importlib.metadata.version('mcp'),
            'encoding':'o200k_base','tokenizer_version':importlib.metadata.version('tiktoken'),
            'language_model_requests':0,'modes':{}}
    payloads={}
    with tempfile.TemporaryDirectory(prefix='hfa-sdk-transport-') as temporary:
        base=Path(temporary);seed=base/'seed'
        with contextlib.redirect_stdout(io.StringIO()):
            hfa.cmd_init(seed)
            hfa.cmd_add_topic(seed,'Transport fixture','transport-proof',
                MARKER+'：这是一条合成证据，完整理由和来源必须保留。',
                source='synthetic:transport',request_id='transport-proof',as_json=True)
        for mode in MODES:
            memory=base/mode;shutil.copytree(seed,memory)
            argv=['-B',str(ROOT/'scripts/hfa_mcp.py'),'--memory-dir',str(memory)]
            if mode.startswith('compact'):argv.append('--compact')
            if mode.endswith('_text'):argv.append('--recall-text-only')
            env={k:os.environ[k] for k in ('PYTHONPATH','TIKTOKEN_CACHE_DIR') if k in os.environ}
            params=StdioServerParameters(command=sys.executable,args=argv,
                env={**env,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
            client_context=Client(params) if hasattr(params,'__aenter__') or stdio_client is None else Client(stdio_client(params))
            rows={};payloads[mode]={}
            async with client_context as client:
                for case,query in (('found','transport-proof'),('miss','absent-xyz-992'),('clarify','画面打转')):
                    call=await client.call_tool('recall_memory',{'query':query,'conversation_id':'synthetic','turn_id':case})
                    if call.is_error:raise ValueError('Unexpected tool error in '+mode+'/'+case)
                    if len(call.content)!=1 or call.content[0].type!='text':raise ValueError('Expected one JSON text block')
                    text=call.content[0].text;payload=json.loads(text)
                    if mode.endswith('_text'):
                        if call.structured_content is not None:raise ValueError('Structured duplicate remains')
                    elif payload!=call.structured_content:raise ValueError('SDK copies disagree')
                    dumped=call.model_dump(mode='json',by_alias=True,exclude_none=True)
                    wire=json.dumps(dumped,ensure_ascii=False,separators=(',',':'))
                    payloads[mode][case]=payload
                    rows[case]={'sdk_result':dumped,'sdk_result_o200k_tokens':count(wire),
                                'text_content_o200k_tokens':count(text),'marker_copies':wire.count(MARKER)}
            result['modes'][mode]=rows
    for mode in ('full','compact'):
        if payloads[mode]!=payloads[mode+'_text']:raise ValueError('Payload differs in text-only transport')
        for case in ('found','miss','clarify'):
            if result['modes'][mode][case]['text_content_o200k_tokens']!=result['modes'][mode+'_text'][case]['text_content_o200k_tokens']:
                raise ValueError('Text-only host comparison unexpectedly changed')
    result['payloads_identical']=True
    result['text_consuming_host_saving_tokens']=0
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    try:
        result=asyncio.run(run());result['passed']=True
    except Exception as exc:
        result={'passed':False,'error':type(exc).__name__+': '+str(exc)}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k!='modes'}|{
        'tokens':{mode:sum(row['sdk_result_o200k_tokens'] for row in rows.values())
                  for mode,rows in result.get('modes',{}).items()}},ensure_ascii=False))
    return 0 if result['passed'] else 2


if __name__=='__main__':sys.exit(main())
