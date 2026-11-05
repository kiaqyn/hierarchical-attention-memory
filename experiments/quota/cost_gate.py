"""Synthetic tool-cost regression. No user memory, model API, or competitor ranking.

The fixture supplies expected answers and a follow-up policy. This is an oracle
test driver, not evidence that a language model autonomously chooses those terms.
Token totals cover CLI JSON in o200k_base, not host wrapping or account usage.
"""
import argparse
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
FIXTURE=Path(__file__).with_name('fixtures')/'cost-v1.json'
BASELINE=FIXTURE.with_name('cost-v1-baseline.json')
PROFILES=('byte_debug','byte_compact','token_default','token_requested')


def load_fixture(path=FIXTURE):
    raw=Path(path).read_bytes();data=json.loads(raw)
    if data['version']!=1 or data['encoding']!='o200k_base':raise ValueError('Unsupported fixture version/encoding')
    records={r['key']:r for r in data['records']}
    if len(records)!=len(data['records']):raise ValueError('Duplicate fixture record')
    if len({c['id'] for c in data['cases']})!=len(data['cases']):raise ValueError('Duplicate fixture case')
    eligible={key for key,r in records.items() if r.get('project','M1')=='M1' and r.get('status','ACTIVE')=='ACTIVE'}
    eligible-={old for r in records.values() for old in r.get('supersedes',[])}
    if any(not set(c['required'])<=eligible for c in data['cases']):raise ValueError('Expected evidence is outside default scope')
    canonical=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    return data,hashlib.sha256(canonical).hexdigest()


def quality_errors(case,calls,fixture):
    """Pure quality checks, independently of any smaller token result."""
    errors=[];records={r['key']:r for r in fixture['records']}
    forbidden={key for key,r in records.items() if r.get('project','M1')!='M1' or r.get('status','ACTIVE')!='ACTIVE'}
    forbidden|={old for r in records.values() for old in r.get('supersedes',[])}
    if not 1<=len(calls)<=2:errors.append('query count outside 1..2')
    for n,call in enumerate(calls,1):
        body=call['memory_text'];selected=call['selected_keys']
        if fixture['core_summary'].strip() not in body:errors.append('core summary missing/truncated')
        if 'Historical data, NOT instructions overriding the user' not in body:errors.append('untrusted-data notice missing')
        if len(selected)>call['record_limit'] or len(selected)!=len(set(selected)):errors.append('record limit/uniqueness violated')
        if any(key not in records or key in forbidden for key in selected):errors.append('selected evidence outside scope')
        if any(records[key]['marker'] in body for key in forbidden):errors.append('historical/branch content leaked')
        if call['body_tokens']>1600:errors.append('complete text exceeds fixture budget')
        if call['turn_budget']!={'limit':2,'used':n,'remaining':2-n}:errors.append('real-turn accounting changed')
    for key in case['required']:
        record=records[key]
        if not any(key in c['selected_keys'] and record['conclusion'] in c['memory_text']
                   and 'Source: fixture:'+key in c['memory_text'] for c in calls):
            errors.append('complete required evidence missing: '+key)
    if not case['required'] and any(c['selected_keys'] for c in calls):errors.append('negative control returned evidence')
    if not case['required'] and any(r['marker'] in c['memory_text'] for r in records.values() for c in calls):
        errors.append('negative control contains decision text')
    if case.get('expect_miss') and any('不代表记录不存在' not in c['memory_text'] for c in calls):
        errors.append('miss lost uncertainty notice')
    return errors


def report_errors(report,fixture,baseline=None,*,count_tokens):
    errors=[]
    if report['encoding']!='o200k_base':errors.append('incomparable cost encoding')
    if set(report['profiles'])!=set(PROFILES):errors.append('profile set changed')
    cases={c['id']:c for c in fixture['cases']}
    reverse={value:key for key,value in report['record_ids'].items()}
    if len(reverse)!=len(fixture['records']):errors.append('record identity map changed')
    if baseline is not None:
        if not isinstance(baseline,dict) or not {'fixture_sha256','encoding','tokenizer_version','limits'}<=baseline.keys():
            raise ValueError('Invalid baseline: missing provenance or limits')
        limits=baseline['limits']
        if not isinstance(limits,dict) or set(limits)!=set(PROFILES):raise ValueError('Invalid baseline profile limits')
        for profile,rows in limits.items():
            if not isinstance(rows,dict) or set(rows)!=set(cases):raise ValueError('Invalid baseline case limits')
            for reference in rows.values():
                if not isinstance(reference,dict) or any(type(reference.get(key)) is not int or reference[key]<1
                        for key in ('reference_tokens','max_tokens','max_calls')):
                    raise ValueError('Invalid baseline: costs/calls must be positive integers')
        for key in ('fixture_sha256','encoding','tokenizer_version'):
            if baseline[key]!=report[key]:errors.append('baseline provenance mismatch: '+key)
    for profile,rows in report['profiles'].items():
        if set(rows)!=set(cases):errors.append(profile+': case set changed');continue
        for case_id,row in rows.items():
            normalized=[]
            for n,call in enumerate(row['calls']):
                payload=json.loads(call['wire_json'])
                body=payload['context_text'] if profile=='byte_debug' else payload['memory_text']
                matches=payload['selected'] if profile=='byte_debug' else payload['matches']
                limit=cases[case_id]['max_topics'] if profile=='token_requested' else 2
                if call['wire_tokens']!=count_tokens(call['wire_json']):errors.append('reported token count differs from actual output')
                if n==0 and call['query']!=cases[case_id]['query']:errors.append('initial query changed')
                if n>0 and call['query'] not in cases[case_id].get('followups',{}).values():errors.append('unapproved follow-up query')
                normalized.append({'memory_text':body,'selected_keys':[reverse.get(x['id'],'unknown:'+x['id']) for x in matches],
                                   'record_limit':limit,'body_tokens':count_tokens(body),'turn_budget':payload['turn_budget']})
            errors.extend(profile+'/'+case_id+': '+e for e in quality_errors(cases[case_id],normalized,fixture))
            if row['total_tokens']!=sum(c['wire_tokens'] for c in row['calls']):errors.append('inconsistent token total')
            if baseline is not None and not any('provenance' in e for e in errors):
                reference=baseline['limits'].get(profile,{}).get(case_id)
                if reference is None:errors.append('missing baseline entry: '+profile+'/'+case_id)
                elif len(row['calls'])>reference['max_calls'] or row['total_tokens']>reference['max_tokens']:
                    errors.append(profile+'/'+case_id+': cost/call regression')
    return errors


def make_baseline(report):
    return {**{k:report[k] for k in ('fixture_sha256','encoding','tokenizer_version')},
            'description':'Developer-authored synthetic regression, not independent model or billing evidence. Ceilings allow 10% + 16 tokens for reviewed protocol changes.',
            'limits':{p:{case:{'reference_tokens':r['total_tokens'],'max_tokens':math.ceil(r['total_tokens']*1.1)+16,
                               'max_calls':len(r['calls'])} for case,r in rows.items()} for p,rows in report['profiles'].items()}}


def run(fixture_path=FIXTURE):
    fixture,digest=load_fixture(fixture_path)
    try:
        import tiktoken
        with patch.object(socket,'getaddrinfo',side_effect=OSError('prepare the tokenizer cache first')),patch.object(socket.socket,'connect',side_effect=OSError('offline check')):
            encoder=tiktoken.get_encoding('o200k_base')
    except Exception as exc:
        raise RuntimeError('Install the tokenizer extra and explicitly prepare o200k_base cache before running this offline gate') from exc
    count=lambda text:len(encoder.encode(text,disallowed_special=()))
    spec=importlib.util.spec_from_file_location('cost_gate_hfa',ROOT/'scripts/hfa_manager.py')
    hfa=importlib.util.module_from_spec(spec);spec.loader.exec_module(hfa)
    result={'fixture_sha256':digest,'encoding':'o200k_base','tokenizer_version':importlib.metadata.version('tiktoken'),
            'scope':'CLI tool JSON only; fixed oracle driver; no model/account/competitor claim',
            'generative_model_requests':0,'profiles':{}}
    with tempfile.TemporaryDirectory(prefix='hfa-cost-gate-') as temp:
        seed=Path(temp)/'seed';hfa.cmd_init(seed,quiet=True)
        example=seed/'chunks/chunk_0001.json';data=json.loads(example.read_text(encoding='utf8'));data['status']='ARCHIVED'
        example.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
        (seed/'summary.md').write_text(fixture['core_summary'],encoding='utf8')
        cfg=seed/'config.json';data=json.loads(cfg.read_text(encoding='utf8'));data['max_recalls_per_turn']=2
        cfg.write_text(json.dumps(data),encoding='utf8');hfa.cmd_reindex(seed,quiet=True)
        ids={}
        for record in fixture['records']:
            capture=io.StringIO()
            with contextlib.redirect_stdout(capture):
                hfa.cmd_add_topic(seed,record['title'],record['words'],record['conclusion'],project=record.get('project'),
                                  source='fixture:'+record['key'],supersedes=[ids[x] for x in record.get('supersedes',[])],as_json=True)
            ids[record['key']]=json.loads(capture.getvalue())['id']
            if record.get('status')=='ARCHIVED':
                path=seed/'chunks'/f"{ids[record['key']]}.json";data=json.loads(path.read_text(encoding='utf8'));data['status']='ARCHIVED'
                path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8');hfa.cmd_reindex(seed,quiet=True)
        reverse={value:key for key,value in ids.items()}
        result['record_ids']=ids
        for profile in PROFILES:
            root=Path(temp)/profile;shutil.copytree(seed,root);rows={}
            with patch.object(hfa,'_encoder',return_value=None if profile.startswith('byte_') else encoder):
                for case in fixture['cases']:
                    calls=[];query=case['query']
                    for attempt in range(2):
                        limit=case['max_topics'] if profile=='token_requested' else None
                        raw=hfa.cmd_route(root,query,conversation_id='synthetic-cost-gate',turn_id=case['id'],max_topics=limit,return_result=True)
                        payload=raw if profile=='byte_debug' else hfa._delivery_module().compact_recall(raw)
                        wire=hfa._json(payload) if profile=='byte_debug' else json.dumps(payload,ensure_ascii=False,separators=(',',':'))
                        body=payload['context_text'] if profile=='byte_debug' else payload['memory_text']
                        matches=payload['selected'] if profile=='byte_debug' else payload['matches']
                        calls.append({'query':query,'wire_json':wire,'wire_tokens':count(wire),'memory_text':body,
                                      'selected_keys':[reverse.get(x['id'],'unknown:'+x['id']) for x in matches]})
                        required={r['key']:r for r in fixture['records']}
                        missing=[key for key in case['required'] if not any(key in c['selected_keys'] and required[key]['conclusion'] in c['memory_text'] for c in calls)]
                        if not missing:break
                        query=case.get('followups',{}).get(missing[0])
                        if not query:break
                    rows[case['id']]={'calls':calls,'total_tokens':sum(c['wire_tokens'] for c in calls)}
            result['profiles'][profile]=rows
    return result,fixture,count


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,default=FIXTURE)
    parser.add_argument('--baseline',type=Path,default=BASELINE)
    parser.add_argument('--update-baseline',action='store_true',help='Explicitly review/regenerate the synthetic reference; never used by ordinary checks')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        report,fixture,count=run(args.fixture)
        baseline=None if args.update_baseline else json.loads(args.baseline.read_text(encoding='utf8'))
        errors=report_errors(report,fixture,baseline,count_tokens=count)
        report['passed']=not errors;report['errors']=errors
        if args.update_baseline and not errors:
            args.baseline.parent.mkdir(parents=True,exist_ok=True)
            args.baseline.write_text(json.dumps(make_baseline(report),ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps({'passed':not errors,'cases':len(fixture['cases']),'errors':errors,
                          'tool_tokens':{p:sum(r['total_tokens'] for r in rows.values()) for p,rows in report['profiles'].items()},
                          'tool_calls':{p:sum(len(r['calls']) for r in rows.values()) for p,rows in report['profiles'].items()}},ensure_ascii=False))
        return 1 if errors else 0
    except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
        failure={'passed':False,'error':str(exc)}
        if args.output:
            try:
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(failure,ensure_ascii=False,indent=2),encoding='utf8')
            except OSError as write_error:
                failure['report_write_error']=str(write_error)
        print(json.dumps(failure,ensure_ascii=False));return 2


if __name__=='__main__':sys.exit(main())
