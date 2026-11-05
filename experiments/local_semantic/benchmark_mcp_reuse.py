"""Reproducible, synthetic before/after MCP latency probe; not an accuracy benchmark.

Identical pinned model, 13 active records (12 synthetic + init), default score,
prebuilt disk caches, three alternating server sessions, eight queries each.
Measure tool latency separately from server startup. Never opens real memory.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

FACTS = [
    ('字体修复', 'font-fixture', '中文字符显示方框时安装缺失字体包。FONT_RULE_1'),
    ('启动等待', 'startup-fixture', '启动等待的小圆圈来自重复初始化，改成只初始化一次。BOOT_RULE_2'),
    ('报表导出', 'export-fixture', '报表导出使用 UTF-8-SIG，避免表格软件打开后乱码。CSV_RULE_3'),
    ('离线存储', 'storage-fixture', '离线记录保存在本地 SQLite，事务失败时回滚。STORE_RULE_4'),
    ('构建缓存', 'build-fixture', '缓存键包含依赖锁文件摘要，依赖变化必须重新构建。BUILD_RULE_5'),
    ('支付重试', 'payment-fixture', '支付请求使用稳定幂等键，同一事件重试只执行一次。PAY_RULE_6'),
    ('连接超时', 'network-fixture', '连接超时按三次指数退避，最终失败明确显示错误。NET_RULE_7'),
    ('主题偏好', 'theme-fixture', '项目演示采用浅色主题和大字号。THEME_RULE_8'),
    ('同步校验', 'sync-fixture', '同步前核对内容摘要，冲突必须保留两份原文。SYNC_RULE_9'),
    ('日志保留', 'log-fixture', '合成服务的调试日志保留七天。LOG_RULE_10'),
    ('键盘焦点', 'focus-fixture', '关闭弹窗后焦点返回原触发按钮。FOCUS_RULE_11'),
    ('移动布局', 'layout-fixture', '窄屏隐藏装饰图片，保留正文和导航。LAYOUT_RULE_12'),
]
QUERIES = ['font-fixture', '文字都成小方块了，过去如何修复？',
           'How did we fix missing Chinese glyphs?', '导出的报表为什么乱码？',
           'payment-fixture 重试规则', '同步遇到冲突怎么保留原文？',
           '火星上的鲸鱼吃什么？', 'layout-fixture']
COMPARE = ('memory_text', 'matches', 'zero_hits', 'mainline', 'payload_tokens', 'payload_budget', 'summary_tokens', 'summary_budget')

def cli(source, *args):
    result=subprocess.run([sys.executable,'-B',str(source/'scripts/hfa_manager.py'),*map(str,args)],
                          capture_output=True,encoding='utf8',timeout=120)
    if result.returncode:raise RuntimeError(result.stdout+'\n'+result.stderr)
    return result.stdout

async def sample(source, model, memory):
    params=StdioServerParameters(command=sys.executable,args=['-B',str(source/'scripts/hfa_mcp.py'),
        '--memory-dir',str(memory),'--semantic-model',str(model)],env={'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
    start=time.perf_counter()
    async with Client(stdio_client(params)) as client:
        await client.list_tools()
        startup=time.perf_counter()-start
        calls=[]
        for number,query in enumerate(QUERIES):
            start=time.perf_counter()
            response=await client.call_tool('recall_memory',{'query':query,'conversation_id':'benchmark','turn_id':str(number)})
            elapsed=time.perf_counter()-start
            if response.is_error:raise RuntimeError(str(response))
            data=response.structured_content
            stable={key:data[key] for key in COMPARE}
            digest=hashlib.sha256(json.dumps(stable,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            calls.append({'seconds':elapsed,'payload_sha256':digest,'selected':[x['id'] for x in data['matches']],
                          'zero_hits':data['zero_hits'],'retrieval':data['retrieval']})
        return {'startup_seconds':startup,'calls':calls}

async def run(args):
    result={'protocol':'13 active records, prebuilt passage cache, default thresholds, 3 alternating sessions x 8 identical queries',
            'python':sys.version,'platform':platform.platform(),'queries':QUERIES,
            'accuracy_claim':False,'model':str(args.model),'samples':{'before':[],'after':[]}}
    with tempfile.TemporaryDirectory(prefix='hfa-warm-benchmark-') as temp:
        for round_no in range(3):
            for label in (('before','after') if round_no%2==0 else ('after','before')):
                source=getattr(args,label)
                memory=Path(temp)/f'{round_no}-{label}'
                cli(source,'init',memory)
                for title,word,body in FACTS:
                    cli(source,'add-topic',memory,'--title',title,'--hit-words',word,'--conclusion',body,'--source','synthetic:latency')
                cli(source,'semantic-index',memory,'--model-dir',args.model)
                result['samples'][label].append(await sample(source,args.model,memory))
    result['equivalent_payloads']=all(a['payload_sha256']==b['payload_sha256']
        for x,y in zip(result['samples']['before'],result['samples']['after']) for a,b in zip(x['calls'],y['calls']))
    result['summary']={}
    for label,samples in result['samples'].items():
        warm=[call['seconds'] for sample_ in samples for call in sample_['calls'][1:]]
        result['summary'][label]={'first_query_median_seconds':statistics.median(s['calls'][0]['seconds'] for s in samples),
                                 'warm_query_median_seconds':statistics.median(warm),
                                 'warm_query_max_seconds':max(warm),'warm_samples':len(warm)}
    result['warm_median_speedup']=result['summary']['before']['warm_query_median_seconds']/result['summary']['after']['warm_query_median_seconds']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:result[k] for k in ('equivalent_payloads','summary','warm_median_speedup')},indent=2))
    if not result['equivalent_payloads']:raise SystemExit('Before/after retrieval payloads differ: investigate before accepting latency gain')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('before','after','model','output'):parser.add_argument('--'+flag,required=True,type=Path)
    asyncio.run(run(parser.parse_args()))
