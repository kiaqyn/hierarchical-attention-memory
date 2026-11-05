"""Frozen pair checks plus post-filter replay of known retrieval scores.

No embeddings, new model calls, calibration, or real-memory input. New evidence
pairs test this checker only; cached retrieval replay is known regression.
"""
import argparse
import hashlib
import json
from pathlib import Path
import socket

from claim_binding import binding_support, filter_bound_candidates
from rerank_v2 import field_support


def offline_only(*args,**kwargs):
    raise RuntimeError('No network calls permitted in binding evaluation')


def validate_quotes(body,decision):
    count=0
    for item in decision['binding']['evidence']:
        assert body[item['start']:item['end']]==item['quote']
        assert body[item['clause_start']:item['clause_end']]==item['clause']
        assert item['clause_start']<=item['start']<item['end']<=item['clause_end']
        count+=1
    return count


def metrics(rows,key):
    positive=[x for x in rows if x['expected'] is not None]
    negative=[x for x in rows if x['expected'] is None]
    return {'positive_cases':len(positive),
            'correct_first':sum(bool(x[key]) and x[key][0]==x['expected'] for x in positive),
            'correct_within_two':sum(x['expected'] in x[key] for x in positive),
            'negative_cases':len(negative),'no_answer_returns':sum(bool(x[key]) for x in negative),
            'wrong_first_on_positive':sum(bool(x[key]) and x[key][0]!=x['expected'] for x in positive),
            'abstained_on_positive':sum(not x[key] for x in positive)}


def run(fixture,report_path,output):
    output.mkdir(parents=True,exist_ok=True)
    freeze=json.loads((fixture/'freeze-manifest.json').read_text(encoding='utf8'))
    assert hashlib.sha256((fixture/'test.json').read_bytes()).hexdigest()==freeze['test_sha256'], 'Frozen pairs changed'
    cases=json.loads((fixture/'test.json').read_text(encoding='utf8'))
    socket.create_connection=offline_only
    socket.socket.connect=offline_only
    pair_rows=[]
    quotes=0
    for case in cases:
        old=field_support(case['query'],case['body'])
        new=binding_support(case['query'],case['body'])
        quotes+=validate_quotes(case['body'],new)
        cited=[x['quote'] for x in new['binding']['evidence']]
        pair_rows.append({**case,'before_allowed':old['allowed'],'after_allowed':new['allowed'],
                          'expected_value_cited':case['expected_value'] in cited if case['expected_allowed'] else None,
                          'decision':new})
    positives=[x for x in pair_rows if x['expected_allowed']]
    negatives=[x for x in pair_rows if not x['expected_allowed']]
    pair_stats={'positive_pairs':len(positives),'negative_pairs':len(negatives),
                'before_accepted_positive':sum(x['before_allowed'] for x in positives),
                'after_accepted_positive':sum(x['after_allowed'] for x in positives),
                'before_accepted_negative':sum(x['before_allowed'] for x in negatives),
                'after_accepted_negative':sum(x['after_allowed'] for x in negatives),
                'correct_values_cited_on_positive':sum(x['after_allowed'] and x['expected_value_cited'] for x in positives)}
    cache=json.loads(report_path.read_text(encoding='utf8'))
    documents={x['id']:x for x in cache['corpus']}
    allowed=set(cache['boundaries']['eligible'])
    groups={}
    for group,rows in [('fixed40',cache['cases']),('known60',cache['legacy_cases']),('known52',cache['older_cases'])]:
        replay=[]
        for row in rows:
            before=row['predictions']['v21_preserve']
            assert set(before).issubset(allowed)
            result=filter_bound_candidates(row['query'],before,documents)
            assert set(result['selected']).issubset(before)
            for key,decision in result['decisions'].items():
                quotes+=validate_quotes(documents[key]['body'],decision)
            replay.append({'id':row['id'],'query':row['query'],'expected':row['expected'],
                           'before':before,'after':result['selected'],'decisions':result['decisions']})
        groups[group]={'before':metrics(replay,'before'),'after':metrics(replay,'after'),
                       'lost_previous_correct_source':[x['id'] for x in replay if x['expected'] is not None and x['expected'] in x['before'] and x['expected'] not in x['after']],
                       'cases':replay}
    result={'kind':'finite explicit-claim binding experiment; not production or answer-generation accuracy',
            'pair_fixture':freeze,'pair_metrics':pair_stats,'pair_cases':pair_rows,'known_retrieval_replay':groups,
            'exact_source_spans_checked':quotes,'cached_report_sha256':hashlib.sha256(report_path.read_bytes()).hexdigest(),
            'boundaries':{'model_calls':0,'calibration_changed':False,'new_candidates_added':False,
                          'final_candidate_limit':2,'final_budget_can_only_shrink':True,
                          'real_memory_opened':False,'daily_installation_changed':False,
                          'python_socket_calls_blocked':True,'system_network_disabled':False},
            'limits':['Finite literal proper-name anchors and metric vocabulary, not full semantics',
                      'Same-clause evidence only; implicit subjects/cross-sentence references may be rejected',
                      'No universal currency/period/denominator disambiguation or contradiction resolution',
                      'New pairs were authored and frozen by the same developer; no independent external holdout',
                      'Known retrieval scores are replayed; no new retrieval/end-to-end host benchmark']}
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'pair_metrics':pair_stats,'regressions':{k:{a:b for a,b in v.items() if a!='cases'} for k,v in groups.items()},'exact_source_spans_checked':quotes},ensure_ascii=False))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for arg in ('fixture','cached-report','output'):
        parser.add_argument('--'+arg,type=Path,required=True)
    args=parser.parse_args()
    run(args.fixture.resolve(),args.cached_report.resolve(),args.output.resolve())
