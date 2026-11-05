"""Validate the question bank and grader controls, without running either engine."""
from collections import Counter
import hashlib,json
from pathlib import Path
from grade import grade,matches
from materialize import bundle
ROOT=Path(__file__).resolve().parent

def validate():
 cases=[json.loads(x) for x in (ROOT/'cases.jsonl').read_text(encoding='utf8').splitlines() if x.strip()]
 scenarios=json.loads((ROOT/'scenarios.json').read_text(encoding='utf8'));keys=json.loads((ROOT/'answer-key.json').read_text(encoding='utf8'))
 coverage=json.loads((ROOT/'coverage.json').read_text(encoding='utf8'))
 errors=[];controls={}
 def check(condition,message):
  if not condition:errors.append(message)
 check(len(cases)==200,'case count')
 check(len({c['id'] for c in cases})==len(cases),'duplicate IDs')
 check(set(keys)=={c['id'] for c in cases},'answer key mapping')
 check(Counter(c['tier'] for c in cases)==Counter(main=120,auxiliary=32,resilience=32,cost_scale=16),'tier balance')
 check(Counter(c['instruction_language'] for c in cases)==Counter(zh=100,en=100),'language balance')
 interests=Counter(next(f['design_interest'] for f in coverage['families'] if f['id']==c['family']) for c in cases if c['tier']=='main')
 check(interests==Counter(neutral=72,scoped_control=24,workflow_continuity=24),'design coverage balance')
 all_ids=[]
 for c in cases:
  s=scenarios[c['scenario_id']];g=keys[c['id']];ids={r['id'] for r in s['records']};events={e['id'] for e in s['events']}
  all_ids.extend(ids)
  check(bool(c['query']) or c['id'].startswith('A07-02'),'accidental empty question:'+c['id'])
  check(set(c['initial_record_ids'])<=ids,'initial schedule:'+c['id'])
  check(all(set(r['source_event_ids'])<=events for r in s['records']),'provenance:'+c['id'])
  check(all(set(group)<=ids and group for group in g['required_groups']),'gold evidence:'+c['id'])
  check(set(g['forbidden_record_ids'])<=ids,'forbidden refs:'+c['id'])
  check(not any(set(group)<=set(g['forbidden_record_ids']) for group in g['required_groups']),'required forbidden overlap:'+c['id'])
  check(set(c['answer_fields'])==set(g['answer_fields']),'answer format fields:'+c['id'])
  check(bool(g['answer_fields'] or g['harness_checks'] or g['status'] in ('unknown','clarify')),'missing objective condition:'+c['id'])
  check(not g['additional_checks'],'unimplemented additional check:'+c['id'])
  check(all(e['timestamp']<=c['context']['as_of'] for e in s['events']),'future event accidentally preloaded:'+c['id'])
  for r in s['records']:
   check(set(r.get('supersedes',[]))<=ids,'supersedes unknown:'+c['id'])
   check('required_groups' not in r and 'expected' not in r,'gold in public record:'+c['id'])
  public=bundle(c['id'],c['modes'][0])
  check(not any('answer-key' in name or 'gold' in name for name in public),'gold file exported:'+c['id'])
  task=public['task.json']
  check(not set(task)&{'required_groups','expected','design_interest','answer_fields_values'},'gold task fields:'+c['id'])
  for a in c['actions']:
   check('op' in a and ('record_id' not in a or a['record_id'] in ids),'invalid action reference:'+c['id'])
  if c['tier']=='main':check(len(s['records'])>=1,'empty main history:'+c['id'])
 check(len(all_ids)==len(set(all_ids)),'global source ID collision')
 for family in [f for f in coverage['families'] if f['tier']=='main']:
  subset=[c for c in cases if c['family']==family['id']]
  check(len(subset)==6 and Counter(c['instruction_language'] for c in subset)==Counter(zh=3,en=3),'main family balance:'+family['id'])
 c=next(c for c in cases if c['id']=='M01-01-zh');g=keys[c['id']];s=scenarios[c['scenario_id']]
 target=[group[0] for group in g['required_groups']]
 obs={'delivered_ids':target,'memory_context_tokens':100,'recall_calls':1,'checks':{}}
 response={'status':g['status'],'answer':g['answer_fields'],'evidence_ids':target,'retrieved_ids':target}
 controls['valid_standardized_answer']=grade(c,response,g,s,'native',obs)['passed']
 wrong={**response,'answer':{'package':'incorrect-package','version':'0'}}
 controls['wrong_answer_rejected']=not grade(c,wrong,g,s,'native',obs)['passed']
 controls['missing_evidence_rejected']=not grade(c,response,g,s,'native',{**obs,'delivered_ids':[]})['passed']
 controls['invented_source_rejected']=not grade(c,{**response,'evidence_ids':['R-imaginary']},g,s,'native',obs)['passed']
 controls['over_budget_rejected']=not grade(c,response,g,s,'native',{**obs,'memory_context_tokens':99999})['passed']
 controls['missing_budget_not_passed']=not grade(c,response,g,s,'native',{'delivered_ids':target})['passed']
 controls['excessive_calls_rejected']=not grade(c,response,g,s,'native',{**obs,'recall_calls':3})['passed']
 p=next(x for x in cases if x['id']=='M16-01-zh');pg=keys[p['id']];ps=scenarios[p['scenario_id']]
 fake={'status':pg['status'],'answer':pg['answer_fields'],'evidence_ids':[],'checks':{'persistent_canary_present':False}}
 controls['model_cannot_attest_own_privacy']=not grade(p,fake,pg,ps,'native',{'delivered_ids':[],'memory_context_tokens':0,'recall_calls':0})['passed']
 scope=next(x for x in cases if x['id']=='M09-01-zh');sg=keys[scope['id']];ss=scenarios[scope['scenario_id']]
 sid=sg['required_groups'][0][0];foreign=sg['forbidden_record_ids'][0]
 controls['scope_leak_rejected']=not grade(scope,{'status':sg['status'],'answer':sg['answer_fields'],'evidence_ids':[sid]},sg,ss,'native',{'delivered_ids':[sid,foreign],'memory_context_tokens':50,'recall_calls':1})['passed']
 controls['bool_not_numeric']=not matches(True,1)
 controls['equivalent_unicode_normalized']=matches('ＣＡＦÉ','cafe\u0301')
 for name,passed in controls.items():check(passed,'grader control failed:'+name)
 manifest=ROOT/'freeze-manifest.json'
 if manifest.exists():
  for name,digest in json.loads(manifest.read_text(encoding='utf8'))['files'].items():
   check((ROOT/name).is_file() and hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,'freeze mismatch:'+name)
 return dict(passed=not errors,errors=errors,cases=len(cases),families=len(coverage['families']),grader_controls=controls,engine_runs=0,note='This validates the authored suite and negative controls, not HFA/competitor performance.')

if __name__=='__main__':
 result=validate();print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['passed'] else 1)
