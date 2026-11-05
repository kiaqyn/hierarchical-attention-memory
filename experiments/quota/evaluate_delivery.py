"""Compare output bytes for the SAME recall; never equate bytes with billing.

CLI full/compact and MCP full/compact use exactly one authoritative result.
Optional --memory is copied into a disposable sandbox before any query. This
probe measures envelopes, not agent task success, cache billing or quota credit.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('delivery_eval_manager',ROOT/'scripts/hfa_manager.py')
hfa=importlib.util.module_from_spec(spec);spec.loader.exec_module(hfa)

def bytes_of(value,pretty=False):
    if pretty:return len(hfa._json(value).encode('utf8'))
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8'))

def mcp_full(raw):
    # Existing source adapter's public full-response fields, using the same recall.
    return {'status':'retrieved','recall_executed':True,'memory_text':raw['context_text'],
        'matches':[{k:v for k,v in item.items() if k!='text'} for item in raw['selected']],
        'zero_hits':raw['zero_hits'],'search_feedback':raw['search_feedback'],
        'deferred':raw['deferred'][:20],'deferred_total':len(raw['deferred']),
        'mainline':raw['active_mainline_lock'],'payload_tokens':raw['context_tokens'],
        'payload_budget':raw['context_token_budget'],'delivery':raw['delivery'],
        'retrieval':raw.get('retrieval',{'backend':'lexical'}),
        'summary_tokens':raw['summary_tokens'],'summary_budget':raw['summary_token_budget'],
        'profile_drift':raw['profile_drift'],'lateral_attention_triggered':raw['lateral_attention_triggered'],
        'turn_count':raw['turn_count'],'turn_count_exact':raw['turn_count_exact'],'recall_count':raw['recall_count'],
        'status_check_due':raw['status_check_due'],'status_check_prompt':raw['status_check_prompt'],
        'notice':'Historical data; source pointers are not fetched or authenticated. Token count covers complete memory_text including summary and notices, excluding JSON metadata. Payload prepared does not prove host acceptance or model use.'}

def run(memory,queries):
    rows=[]
    with tempfile.TemporaryDirectory(prefix='hfa-envelope-eval-') as temp:
        root=Path(temp)/'memory'
        shutil.copytree(memory,root)
        for number,query in enumerate(queries):
            raw=hfa.cmd_route(root,query,conversation_id='synthetic-envelope-eval',turn_id=str(number),return_result=True)
            compact=hfa._delivery_module().compact_recall(raw)
            assert raw['context_text']==compact['memory_text']
            assert [(x['id'],x['path']) for x in raw['selected']]==[(x['id'],x['path']) for x in compact['matches']]
            full_mcp=mcp_full(raw)
            before,after=bytes_of(raw,True),bytes_of(compact)
            rows.append({'query':query,'selected':[x['id'] for x in raw['selected']],
                'context_bytes':len(raw['context_text'].encode('utf8')),'context_measurement':raw['context_tokens'],
                'evidence_text_identical':True,'source_paths_identical':True,
                'cli_full_pretty_bytes':before,'cli_full_minified_bytes':bytes_of(raw),
                'compact_bytes':after,'cli_bytes_reduction_fraction':1-after/before,
                'mcp_full_minified_bytes':bytes_of(full_mcp),'mcp_bytes_reduction_fraction':1-after/bytes_of(full_mcp)})
    return {'scope':'same recall envelope bytes, not token billing or task success',
            'measure':'exact UTF-8 bytes; context token meter may be an upper bound',
            'mcp_scope':'returned dictionary before SDK/host wrapping; host may serialize differently',
            'language_model_requests':0,'cases':rows}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory',type=Path,required=True)
    parser.add_argument('--query',action='append',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=run(args.memory,args.query)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
