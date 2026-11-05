"""Create an engine-visible bundle. No engine/model calls or global settings."""
import argparse,hashlib,json,random
from pathlib import Path

ROOT=Path(__file__).resolve().parent
def load_cases():return [json.loads(line) for line in (ROOT/'cases.jsonl').read_text(encoding='utf8').splitlines() if line.strip()]

def bundle(case_id,mode):
 case=next(c for c in load_cases() if c['id']==case_id)
 if mode not in case['modes']:raise ValueError('Mode not applicable to this case')
 scenario=json.loads((ROOT/'scenarios.json').read_text(encoding='utf8'))[case['scenario_id']]
 task={k:v for k,v in case.items() if k not in ('semantic_pair','family','tier','instruction_language')}
 result={'task.json':task,'history.jsonl':scenario['events']}
 records=scenario['records']
 recipe=case.get('recipe')
 if recipe and recipe['kind']=='scale_records':
  rng=random.Random(recipe['seed']);filler=[]
  for i in range(recipe['count']-1):
   rid='G-'+hashlib.sha256((case_id+':'+str(i)).encode()).hexdigest()[:16]
   filler.append(dict(id=rid,title=f'Worker note {i}',body=f'Cedar worker {i}: unrelated queue setting {rng.randrange(1,1000)}.',tags=['worker','queue'],scope=dict(project='river' if i%7==0 else 'cedar',branch='main',environment='test'),source_event_ids=['GE-'+rid[2:]]))
  target=records[0];placement=recipe['target_placement']
  if placement=='start':records=[target]+filler
  elif placement=='middle':records=filler[:len(filler)//2]+[target]+filler[len(filler)//2:]
  elif placement=='end':records=filler+[target]
  else:records=filler+[target];rng.shuffle(records)
  # Raw native input must include exactly the same generated facts.
  result['history.jsonl']=[dict(id=r['source_event_ids'][0],sequence=i+1,timestamp='2026-10-01T09:00:00Z',role='user',kind='message',scope=r['scope'],content=r['body']) for i,r in enumerate(records)]
  task['initial_record_ids']=[r['id'] for r in records]
 if recipe and recipe['kind']=='turn_trajectory':
  trajectory=[]
  for n in range(1,recipe['turns']+1):
   ident=f'synthetic-turn-{n:04d}'
   if recipe['operation']=='many_decision_replacements':
    content=f'Explicit replacement: service alias is now amber-{n}; previous aliases are historical.'
   elif recipe['operation']=='noisy_long_history':
    content='Remember validated release label cedar-final-73.' if n==17 else f'Unrelated synthetic status update {n}.'
   else:content=f'Unrelated synthetic status update {n}.'
   trajectory.append(dict(event_id=ident,turn_id=ident,content=content,role='user'))
   if recipe['operation']=='distinct_turn_reminder' and n%5==0:trajectory.append(dict(event_id=ident,turn_id=ident,content=content,role='user',redelivery=True))
  result['trajectory.jsonl']=trajectory
  task['trajectory_execution']='Event replay is data preparation. Model-per-event execution needs its own declared run budget; do not silently make 1000 paid calls.'
 if mode=='records':result['records.jsonl']=records
 elif mode=='operational':
  result['fixtures.jsonl']=records
  initial=set(task['initial_record_ids'])
  initial_events={e for r in records if r['id'] in initial for e in r['source_event_ids']}
  result['history.jsonl']=[e for e in result['history.jsonl'] if e['id'] in initial_events]
  task['fixture_policy']='Only initial_record_ids are preloaded. Other records are input fixtures for the declared later actions.'
 # Native deliberately receives no pre-extracted record list or answer key.
 return result

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--case',required=True);p.add_argument('--mode',choices=['records','native','operational'],required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
 if a.out.exists() and any(a.out.iterdir()):raise SystemExit('Refusing to overwrite a nonempty bundle directory')
 a.out.mkdir(parents=True,exist_ok=True)
 data=bundle(a.case,a.mode)
 for name,value in data.items():
  text=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in value) if name.endswith('.jsonl') else json.dumps(value,ensure_ascii=False,indent=2)+'\n'
  (a.out/name).write_text(text,encoding='utf8')
 print(json.dumps({'case':a.case,'mode':a.mode,'files':list(data),'engine_requests':0}))
if __name__=='__main__':main()
