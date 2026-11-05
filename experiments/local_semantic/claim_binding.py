"""Conservative local binding of explicit numeric requests to source clauses.

Not general semantic entailment. Unknown names/aliases, implicit subjects and
cross-sentence references are deliberately not resolved. No model/API calls.
"""
from datetime import date
import re

from rerank_v2 import VALUES, field_support, requested_fields

METRICS = {
    'budget': ('currency', r'预算|经费|拨款|\bbudget\b|\ballocat(?:ed|ion)\b'),
    'price': ('currency', r'售价|价格|单价|\bprice\b|\bpurchase\s+(?:cost|fee)\b|\blicen[cs]e\s+(?:cost|fee)\b'),
    'spending': ('currency', r'实付|支出|花费|花了|费用|\b(?:paid|spent|spending|cost)\b'),
    'success_rate': ('percentage', r'成功率|\bsuccess\s+rate\b'),
    'retry_rate': ('percentage', r'重试率|(?:重试|重发|再次提交).{0,12}(?:比例|占比)|(?:比例|占比).{0,16}(?:重试|重发|再次提交)|\bretr(?:y|ied)\s+rate\b'),
    'failure_rate': ('percentage', r'失败率|错误率|\b(?:failure|error)\s+rate\b'),
    'refund_rate': ('percentage', r'退款率|\brefund\s+rate\b'),
    'duplicate_charge_rate': ('percentage', r'重复扣款率|\bduplicate\s+charg(?:e|ing)\s+rate\b'),
    'availability': ('percentage', r'可用率|可用性比例|\bavailability\b'),
    'retention': ('duration', r'保留|保存|留存|\bretention\b|\b(?:kept|retain(?:ed)?)\b'),
    'timeout': ('duration', r'超时|\btimeout\b|\btime\s+out\b'),
    'latency': ('duration', r'延迟|响应时间|\blatency\b|\bresponse\s+time\b'),
    'cache_lifetime': ('duration', r'有效期|\bTTL\b|\blifetime\b|\btime\s+to\s+live\b'),
}
STOP_NAMES = set('What Which How Who When Where Why The A An In On At For From Is Was Were Does Did It We Our This That These Those After Before During Since Until Given Following Under With Without Please Tell Show Could Can Do Are Have Has Would Should Actual Measured Observed Approved Planned Target Monthly Annual January February March April May June July August September October November December USD CNY RMB GB GiB MB MiB KB KiB SHA256 SHA CPU GPU API'.casefold().split())
MONTHS = {word: i for i,word in enumerate('january february march april may june july august september october november december'.split(),1)}
CN_MONTHS = {word:i for i,word in enumerate(('一','二','三','四','五','六','七','八','九','十','十一','十二'),1)}
CLAUSE_BREAK = re.compile(r'(?<!\d)\.|\.(?!\d)|[。！？!?；;\n]|(?<!\d)[,，]|[,，](?!\d)|但是|但|\b(?:but|whereas|while)\b',re.I)
NEGATED = re.compile(r'没有记录|未记录|未测量|未提供|未公布|不是|并非|不等于|不为|不详|未知|\bunknown\b|\bnot\b|\bnever\b|\bno\s+(?:measurement|measured|recorded|value)\b',re.I)
MEASURED = re.compile(r'实际|实测|测得|\b(?:actual(?:ly)?|measured|observed)\b',re.I)
PLANNED = re.compile(r'计划|预计|目标|\b(?:planned|expected|target)\b',re.I)


def explicit_entities(text):
    """Only literal proper-name anchors, never invented synonym mappings."""
    found = set()
    for match in re.finditer(r'[“「《\"]([^”」》\"\n]{1,40})[”」》\"]',text):
        if not re.fullmatch(r'[\d\W]+',match.group(1)):
            found.add(match.group(1).casefold())
    for match in re.finditer(r'(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9_-]*(?![A-Za-z0-9_])',text):
        word = match.group().casefold()
        if word not in STOP_NAMES and len(word)>1 and not re.fullmatch(r'v?\d+',word):
            found.add(word)
    return found


def metric_names(text, field):
    names = {name for name,(kind,pattern) in METRICS.items() if kind==field and re.search(pattern,text,re.I)}
    # Purchase-price phrases contain "cost", but are not a second metric.
    if 'price' in names:
        names.discard('spending')
    return names


def binding_fields(query):
    fields=[x for x in requested_fields(query) if x in ('currency','percentage','duration','hash_value','storage_size')]
    # The older detector misses past-tense "What was ... rate?". Explicit
    # metric-valued questions still need a numeric field, regardless of tense.
    numeric_question=re.search(r'多少|几|\bhow\s+(?:much|many|long)\b|\bwhat\b[^?]{0,100}\b(?:rate|budget|price|timeout|latency|lifetime|retention)\b',query,re.I)
    # Infer only when the older detector found no numeric field. Otherwise a
    # duration request about a "price cache" would spuriously ask for money.
    if numeric_question and not fields:
        for field in ('currency','percentage','duration'):
            if metric_names(query,field) and field not in fields:
                fields.append(field)
    return fields


def compatible_metrics(requested, present):
    # A cache lifetime establishes generic retention duration, while generic
    # log retention does not establish the more specific cache lifetime.
    return all(name in present or (name=='retention' and 'cache_lifetime' in present) for name in requested)


def explicit_time(text, reference_date=None):
    years = set(re.findall(r'(?<!\d)(20\d{2})(?!\d)',text))
    months = {int(x) for x in re.findall(r'(?<!\d)(1[0-2]|0?[1-9])\s*月',text)}
    for name,number in CN_MONTHS.items():
        if re.search(r'(?<![一二三四五六七八九十])'+name+'月',text):
            months.add(number)
    for name,number in MONTHS.items():
        if re.search(r'\b'+name+r'\b',text,re.I):
            months.add(number)
    for year,month in re.findall(r'(?<!\d)(20\d{2})[-/](0?[1-9]|1[0-2])(?:\b|(?=[^\d]))',text):
        years.add(year)
        months.add(int(month))
    relative = re.search(r'去年|今年|last year|this year',text,re.I)
    if relative and reference_date is not None:
        if not isinstance(reference_date,date):
            raise TypeError('reference_date must be datetime.date')
        years.add(str(reference_date.year-(1 if re.search(r'去年|last year',relative.group(),re.I) else 0)))
    cadence=[]
    for name,pattern in {'monthly':r'每月|月度|\bmonthly\b|\bper\s+month\b',
                         'annual':r'每年|年度|\bannual(?:ly)?\b|\bper\s+year\b',
                         'weekly':r'每周|周度|\bweekly\b|\bper\s+week\b',
                         'daily':r'每天|每日|\bdaily\b|\bper\s+day\b'}.items():
        if re.search(pattern,text,re.I):cadence.append(name)
    return {'years':sorted(years),'months':sorted(months),'cadence':cadence,'needs_reference_date':bool(relative and reference_date is None)}


def clauses(body):
    start = 0
    for sep in CLAUSE_BREAK.finditer(body):
        if sep.start()>start:
            yield start,sep.start(),body[start:sep.start()]
        start=sep.end()
    if start<len(body):
        yield start,len(body),body[start:]


def binding_support(query, body, reference_date=None):
    """Gate a candidate with literal source-clause evidence; never answer it.

    `allowed` means this finite checker did not reject the candidate. It must
    not be interpreted as universal factual support or a final answer verdict.
    """
    if not isinstance(query,str) or not isinstance(body,str):
        raise TypeError('query and body must be strings')
    if reference_date is not None and not isinstance(reference_date,date):
        raise TypeError('reference_date must be datetime.date')
    base=field_support(query,body,reference_year=reference_date.year if reference_date else 2026)
    fields=binding_fields(query)
    entities=explicit_entities(query)
    times=explicit_time(query,reference_date)
    requested={'entities':sorted(entities),'metrics':{f:sorted(metric_names(query,f)) for f in fields},'time':times}
    binding={'coverage':'finite_explicit_anchors_only','requested':requested,'evidence':[],'reasons':[]}
    result={**base,'binding':binding}
    if not fields:
        binding['coverage']='not_checked_non_numeric_request'
        return result
    if times['needs_reference_date']:
        return {**result,'allowed':False,'status':'binding_reference_date_required'}
    # Secrets, absent fields, named-actor requirements etc. retain their gate.
    if not base['allowed']:
        return result
    if any(len(names)>1 for names in requested['metrics'].values()):
        return {**result,'allowed':False,'status':'binding_ambiguous_requested_metric'}

    bound_checks=[]
    for field in fields:
        found=None
        for begin,end,unit in clauses(body):
            matches=list(re.finditer(VALUES[field],unit,re.I))
            if not matches:
                continue
            unit_entities=explicit_entities(unit)
            # Proper-name matching is literal but case-insensitive. A name
            # quoted by the question need not also be quoted in the source.
            for entity in entities:
                if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(entity)+r'(?![A-Za-z0-9_-])',unit,re.I):
                    unit_entities.add(entity)
            unit_metrics=metric_names(unit,field)
            unit_time=explicit_time(unit)
            reason=None
            if not entities.issubset(unit_entities): reason='explicit_entity_not_in_value_clause'
            elif entities and unit_entities-entities: reason='multiple_explicit_entities_in_value_clause'
            elif not compatible_metrics(requested['metrics'][field],unit_metrics): reason='requested_metric_not_in_value_clause'
            elif requested['metrics'][field] and len(unit_metrics)>1: reason='multiple_metrics_in_value_clause'
            elif not set(times['years']).issubset(unit_time['years']): reason='requested_year_not_in_value_clause'
            elif not set(times['months']).issubset(unit_time['months']): reason='requested_month_not_in_value_clause'
            elif not set(times['cadence']).issubset(unit_time['cadence']): reason='requested_period_not_in_value_clause'
            elif NEGATED.search(unit): reason='value_clause_has_explicit_absence'
            elif MEASURED.search(query) and PLANNED.search(unit): reason='planned_clause_does_not_establish_measurement'
            if reason:
                binding['reasons'].append({'field':field,'reason':reason,'start':begin,'end':end})
                continue
            # A change "from 60 to 300" must cite the new value, not the old.
            change=list(re.finditer(r'改为|调整为|变更为|\bchanged\s+to\b|\bnow\b',unit,re.I))
            current=[m for m in matches if not change or m.start()>=change[-1].end()]
            if len(current)!=1:
                binding['reasons'].append({'field':field,'reason':'multiple_or_unresolved_values','start':begin,'end':end})
                continue
            match=current[0]
            found={'field':field,'present':True,'quote':match.group(),'start':begin+match.start(),'end':begin+match.end()}
            binding['evidence'].append({**found,'clause':unit,'clause_start':begin,'clause_end':end,
                                        'entities':sorted(unit_entities),'metrics':sorted(unit_metrics),'time':unit_time})
            break
        if found is None:
            bound_checks.append({'field':field,'present':False,'quote':None,'start':None,'end':None})
        else:
            bound_checks.append(found)
    passed=all(check['present'] for check in bound_checks)
    untouched=[check for check in base['checks'] if check['field'] not in fields]
    return {**result,'allowed':passed,'status':'explicit_value_binding_found' if passed else 'explicit_value_binding_not_established',
            'checks':untouched+bound_checks}


def filter_bound_candidates(query, candidates, documents, reference_date=None):
    """Filter at most two ALREADY scoped/budgeted candidates, never add any.

    Scope, ACTIVE/supersession filtering and the payload budget belong to the
    preceding retriever. This stage neither searches nor expands permissions.
    """
    if len(candidates)>2 or len(set(candidates))!=len(candidates):
        raise ValueError('Expected at most two unique scoped candidates')
    selected=[]
    decisions={}
    for key in candidates:
        decision=binding_support(query,documents[key]['body'],reference_date)
        decisions[key]=decision
        if decision['allowed']:
            selected.append(key)
    return {'selected':selected,'decisions':decisions}
