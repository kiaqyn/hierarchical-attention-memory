"""Objective development-set grader, not an engine runner or LLM judge."""
import argparse,json,math,unicodedata
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def matches(actual,expected):
 if isinstance(expected,dict) and expected.get('op')=='one_of':return any(matches(actual,x) for x in expected['values'])
 if isinstance(expected,dict) and expected.get('op')=='le':return type(actual) in (int,float) and math.isfinite(actual) and actual<=expected['value']
 if isinstance(expected,bool) or expected is None:return actual is expected
 if isinstance(expected,(int,float)):
  return type(actual) in (int,float) and math.isfinite(actual) and math.isclose(actual,expected,rel_tol=1e-9,abs_tol=1e-9)
 if isinstance(expected,str):return isinstance(actual,str) and unicodedata.normalize('NFKC',actual).casefold().strip()==unicodedata.normalize('NFKC',expected).casefold().strip()
 if isinstance(expected,list):return isinstance(actual,list) and len(actual)==len(expected) and all(matches(a,b) for a,b in zip(actual,expected))
 return actual==expected

def grade(case,response,answer_key,scenario,mode,observations=None):
 errors=[];pending=[]
 if mode not in case['modes']:return {'case':case['id'],'status':'not_applicable','passed':False,'reason':'Mode excluded before execution'}
 if response.get('status')=='unsupported':return {'case':case['id'],'status':'unsupported','passed':False,'reason':response.get('reason','No implementation')}
 public_records=scenario['records']
 if case.get('recipe',{}).get('kind')=='scale_records':
  from materialize import bundle
  public_records=bundle(case['id'],'records')['records.jsonl']
 known={r['id'] for r in public_records}
 delivered=response.get('retrieved_ids',[]) if mode=='records' else (observations or {}).get('delivered_ids',[])
 if not isinstance(delivered,list) or any(not isinstance(x,str) for x in delivered):errors.append('invalid_delivered_ids');delivered=[]
 unknown=set(delivered)-known
 if unknown:errors.append('unknown_source_ids')
 forbidden=set(answer_key['forbidden_record_ids'])
 forbidden.update(r['id'] for r in public_records if any(r['scope'][k]!=case['context'][k] for k in ('project','branch','environment')))
 if set(delivered)&forbidden:errors.append('out_of_scope_body_delivered')
 if mode!='operational':
  for group in answer_key['required_groups']:
   if not set(group)&set(delivered):errors.append('required_evidence_missing')
 if mode=='records':
  if answer_key['status']=='unknown' and not answer_key['required_groups'] and delivered:errors.append('unrelated_result_on_no_evidence_probe')
 else:
  if response.get('status')!=answer_key['status']:errors.append('wrong_answer_status')
  fields=response.get('answer',{})
  if not isinstance(fields,dict):errors.append('invalid_answer');fields={}
  for field,value in answer_key['answer_fields'].items():
   if field not in fields or not matches(fields[field],value):errors.append('wrong_or_missing_field:'+field)
  citations=response.get('evidence_ids',[])
  if not isinstance(citations,list) or any(not isinstance(x,str) for x in citations):errors.append('invalid_citations');citations=[]
  if any(x not in delivered for x in citations):errors.append('citation_not_delivered')
  if answer_key['required_groups']:
   if observations is None:pending.append('independent_delivery_trace_required')
   for group in answer_key['required_groups']:
    if not set(group)&set(citations):errors.append('required_citation_missing')
  for name,expected in answer_key['harness_checks'].items():
   if observations is None or name not in observations.get('checks',{}):pending.append('independent_check_missing:'+name)
   elif not matches(observations['checks'][name],expected):errors.append('harness_check_failed:'+name)
 # Enforce body and call budgets from the independent harness, not model claims.
 if mode in ('records','native'):
  budget=(observations or {}).get('memory_context_tokens')
  calls=(observations or {}).get('recall_calls')
  if budget is None or calls is None:pending.append('independent_budget_and_call_measurements_required')
  else:
   if type(budget) is not int or budget<0 or budget>case['limits']['memory_context_tokens']:errors.append('memory_context_budget_exceeded_or_invalid')
   if type(calls) is not int or calls<0 or calls>case['limits']['retrieval_calls']:errors.append('recall_call_limit_exceeded_or_invalid')
 # State and usage objects inside the model response are deliberately not trusted.
 return {'case':case['id'],'mode':mode,'passed':not errors and not pending,'status':'pending_evidence' if pending else ('pass' if not errors else 'fail'),'errors':errors,'pending':pending,'task_completion':not errors and not pending}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--case',required=True);p.add_argument('--mode',choices=['records','native','operational'],required=True);p.add_argument('--response',type=Path,required=True);p.add_argument('--observations',type=Path);a=p.parse_args()
 cases=[json.loads(x) for x in (ROOT/'cases.jsonl').read_text(encoding='utf8').splitlines() if x.strip()]
 c=next(c for c in cases if c['id']==a.case);key=json.loads((ROOT/'answer-key.json').read_text(encoding='utf8'))[a.case];scenario=json.loads((ROOT/'scenarios.json').read_text(encoding='utf8'))[c['scenario_id']]
 result=grade(c,json.loads(a.response.read_text(encoding='utf8')),key,scenario,a.mode,json.loads(a.observations.read_text(encoding='utf8')) if a.observations else None)
 print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
