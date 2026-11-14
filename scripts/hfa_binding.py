"""Conservative clause-level binding for explicit numeric requests.

Zero external dependencies (Python stdlib only). Checks whether literal entities,
explicit metric names, calendar/cadence anchors, and current numeric values
co-occur within the same clause of an already scoped, active, budgeted record.
This filters false positives only; similarity or binding pass never proves
universal factual entailment.
"""
from __future__ import annotations

from datetime import date
import re

NUMBER = r"(?:\d+(?:[.,]\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
ENG_SMALL_NUMBER = r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\b"
VALUES = {
    "currency": rf"(?:[$¥￥]|\b(?:USD|CNY|RMB)\s*){NUMBER}|{NUMBER}\s*(?:元|美元|人民币|USD|CNY|RMB|dollars?)",
    "percentage": rf"{NUMBER}\s*[%％]|百分之\s*{NUMBER}",
    "duration": rf"(?:{NUMBER}|{ENG_SMALL_NUMBER})[-\s]*(?:天|日|小时|分钟|毫秒|秒|days?|hours?|minutes?|milliseconds?|seconds?|ms\b)",
    "hash_value": r"(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])",
    "storage_size": rf"{NUMBER}\s*(?:GiB|GB|MiB|MB|KiB|KB|字节|bytes?)\b",
    "named_actor": r"(?:由|批准人[:：]?|负责人[:：]?|approved by\s+|authorized by\s+)[^。；;\n]{1,35}(?:批准|确认|[。.;]|$)",
    "ip_address": r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b",
}

METRICS = {
    "budget": ("currency", r"预算|经费|拨款|\bbudget\b|\ballocat(?:ed|ion)\b"),
    "price": ("currency", r"售价|价格|单价|\bprice\b|\bpurchase\s+(?:cost|fee)\b|\blicen[cs]e\s+(?:cost|fee)\b"),
    "spending": ("currency", r"实付|支出|花费|花了|费用|\b(?:paid|spent|spending|cost)\b"),
    "success_rate": ("percentage", r"成功率|\bsuccess\s+rate\b"),
    "retry_rate": ("percentage", r"重试率|(?:重试|重发|再次提交).{0,12}(?:比例|占比)|(?:比例|占比).{0,16}(?:重试|重发|再次提交)|\bretr(?:y|ied)\s+rate\b"),
    "failure_rate": ("percentage", r"失败率|错误率|\b(?:failure|error)\s+rate\b"),
    "refund_rate": ("percentage", r"退款率|\brefund\s+rate\b"),
    "duplicate_charge_rate": ("percentage", r"重复扣款率|\bduplicate\s+charg(?:e|ing)\s+rate\b"),
    "availability": ("percentage", r"可用率|可用性比例|\bavailability\b"),
    "retention": ("duration", r"保留|保存|留存|\bretention\b|\b(?:kept|retain(?:ed)?)\b"),
    "timeout": ("duration", r"超时|\btimeout\b|\btime\s+out\b"),
    "backoff": ("duration", r"退避|\bbackoffs?\b"),
    "latency": ("duration", r"延迟|响应时间|\blatency\b|\bresponse\s+time\b"),
    "cache_lifetime": ("duration", r"有效期|\bTTL\b|\blifetime\b|\btime\s+to\s+live\b"),
}

STOP_NAMES = set(
    "What Which How Who When Where Why The A An In On At For From Is Was Were Does Did It We Our "
    "This That These Those After Before During Since Until Given Following Under With Without Please "
    "Tell Show Could Can Do Are Have Has Would Should Actual Measured Observed Approved Planned Target "
    "Monthly Annual January February March April May June July August September October November December "
    "USD CNY RMB GB GiB MB MiB KB KiB SHA256 SHA CPU GPU API".casefold().split()
)
MONTHS = {
    word: idx
    for idx, word in enumerate(
        "january february march april may june july august september october november december".split(), 1
    )
}
CN_MONTHS = {
    word: idx
    for idx, word in enumerate(("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1)
}
CLAUSE_BREAK = re.compile(
    r"(?<!\d)\.|\.(?!\d)|[。！？!?；;\n]|(?<!\d)[,，]|[,，](?!\d)|但是|但|\b(?:but|whereas|while)\b",
    re.I,
)
NEGATED = re.compile(
    r"没有记录|未记录|未测量|未提供|未公布|不是|并非|不等于|不为|不详|未知"
    r"|\bunknown\b|\bnot\b|\bnever\b|\bno\s+(?:measurement|measured|recorded|value)\b",
    re.I,
)
MEASURED = re.compile(r"实际|实测|测得|\b(?:actual(?:ly)?|measured|observed)\b", re.I)
PLANNED = re.compile(r"计划|预计|目标|\b(?:planned|expected|target)\b", re.I)
_CADENCE_PATTERNS = {
    "monthly": r"每月|月度|\bmonthly\b|\bper\s+month\b",
    "annual": r"每年|年度|\bannual(?:ly)?\b|\bper\s+year\b",
    "weekly": r"每周|周度|\bweekly\b|\bper\s+week\b",
    "daily": r"每天|每日|\bdaily\b|\bper\s+day\b",
}


def parse_reference_date(value):
    """Parse an explicit YYYY-MM-DD date or datetime.date; never guess today()."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise ValueError("reference_date must be an ISO YYYY-MM-DD date string")
    return date.fromisoformat(value.strip())


def requested_fields(query):
    fields = []
    tests = {
        "currency": r"多少(?:钱|元|美元)|费用(?:是|为|多少)|预算(?:是|为|多少|额度)|(?:how much|purchase price|cost in|spending budget|monthly budget|allocated|what amount)",
        "percentage": r"占比|比例|百分比|率[^。？?]{0,12}(?:多少|几)|\b(?:percentage|fraction|share|percent)\b|what[^?]{0,30}\brate\b",
        "duration": r"多久|几天|多长时间|how long|how many days|\blifetime\b|retention period",
        "hash_value": r"(?:实际|具体).{0,20}(?:哈希|校验值)|(?:哈希值|校验值).{0,8}(?:多少|什么)|(?:actual|exact).{0,25}(?:hash|digest|checksum)|hash value",
        "storage_size": r"多少\s*(?:GB|GiB|MB|MiB|字节)|how many\s+(?:gigabytes|megabytes|bytes)",
        "named_actor": r"谁|哪位|\bwho\b",
        "ip_address": r"(?<![a-z0-9_])(?:ip\s*(?:address|地址)?(?=\s*(?:是|为|多少|什么|\?|$|？))|\bwhat\b[^?]{0,50}\bip\b)",
    }
    for name, pattern in tests.items():
        if re.search(pattern, query, re.I):
            fields.append(name)
    return fields


def field_support(query, body, reference_year=2026):
    """Return exact source spans for requested field categories."""
    if re.search(r"(?:密码|口令).{0,6}(?:多少|什么|是)|what.{0,45}password|(?:give|show).{0,30}password", query, re.I):
        return {"allowed": False, "status": "needs_authorized_secret_source", "checks": []}
    checks = []
    for name in requested_fields(query):
        match = re.search(VALUES[name], body, re.I)
        checks.append({
            "field": name,
            "present": bool(match),
            "quote": match.group() if match else None,
            "start": match.start() if match else None,
            "end": match.end() if match else None,
        })
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", query))
    if re.search(r"去年|last year", query, re.I):
        years.add(str(reference_year - 1))
    for year in sorted(years):
        match = re.search(re.escape(year), body)
        checks.append({
            "field": "explicit_year",
            "present": bool(match),
            "quote": year if match else None,
            "start": match.start() if match else None,
            "end": match.end() if match else None,
        })
    asks_measured = bool(re.search(r"实际|实测|measured|observed|actually", query, re.I))
    planned_only = (
        bool(re.search(r"计划|预计|目标|planned|expected|target", body, re.I))
        and not re.search(r"实际|实测|验证|measured|observed|verified", body, re.I)
    )
    if asks_measured and planned_only:
        checks.append({"field": "measured_not_just_planned", "present": False, "quote": None, "start": None, "end": None})
    allowed = all(check["present"] for check in checks)
    return {
        "allowed": allowed,
        "status": ("requested_fields_present" if allowed else "requested_fields_missing") if checks else "relevance_only_unverified",
        "checks": checks,
    }


def fields_present(query, body):
    """Boolean compatibility check for requested field presence."""
    return field_support(query, body)["allowed"]


def explicit_entities(text):
    """Literal proper-name anchors only; never invent cross-language aliases."""
    found = set()
    for match in re.finditer(r"[“「《\"]([^”」》\"\n]{1,40})[”」》\"]", text):
        if not re.fullmatch(r"[\d\W]+", match.group(1)):
            found.add(match.group(1).casefold())
    for match in re.finditer(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9_-]*(?![A-Za-z0-9_])", text):
        word = match.group().casefold()
        if word not in STOP_NAMES and len(word) > 1 and not re.fullmatch(r"v?\d+", word):
            found.add(word)
    return found


def metric_names(text, field):
    names = {name for name, (kind, pattern) in METRICS.items() if kind == field and re.search(pattern, text, re.I)}
    if "price" in names:
        names.discard("spending")
    return names


def binding_fields(query):
    fields = [x for x in requested_fields(query) if x in ("currency", "percentage", "duration", "hash_value", "storage_size")]
    numeric_question = re.search(
        r"多少|几|\bhow\s+(?:much|many|long)\b|\bwhat\b[^?]{0,100}\b(?:rate|budget|price|timeout|latency|lifetime|retention)\b",
        query,
        re.I,
    )
    if numeric_question and not fields:
        for field in ("currency", "percentage", "duration"):
            if metric_names(query, field) and field not in fields:
                fields.append(field)
    return fields


def compatible_metrics(requested, present):
    return all(name in present or (name == "retention" and "cache_lifetime" in present) for name in requested)


def explicit_time(text, reference_date=None):
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", text))
    months = {int(x) for x in re.findall(r"(?<!\d)(1[0-2]|0?[1-9])\s*月", text)}
    for name, number in CN_MONTHS.items():
        if re.search(r"(?<![一二三四五六七八九十])" + name + "月", text):
            months.add(number)
    for name, number in MONTHS.items():
        if re.search(r"\b" + name + r"\b", text, re.I):
            months.add(number)
    for year, month in re.findall(r"(?<!\d)(20\d{2})[-/](0?[1-9]|1[0-2])(?:\b|(?=[^\d]))", text):
        years.add(year)
        months.add(int(month))
    relative = re.search(r"去年|今年|last year|this year", text, re.I)
    if relative and reference_date is not None:
        if type(reference_date) is not date:
            raise TypeError("reference_date must be datetime.date")
        years.add(str(reference_date.year - (1 if re.search(r"去年|last year", relative.group(), re.I) else 0)))
    cadence = [name for name, pattern in _CADENCE_PATTERNS.items() if re.search(pattern, text, re.I)]
    return {
        "years": sorted(years),
        "months": sorted(months),
        "cadence": cadence,
        "needs_reference_date": bool(relative and reference_date is None),
    }


def clauses(body):
    start = 0
    for sep in CLAUSE_BREAK.finditer(body):
        if sep.start() > start:
            yield start, sep.start(), body[start:sep.start()]
        start = sep.end()
    if start < len(body):
        yield start, len(body), body[start:]


_UNRECORDED_RE = re.compile(
    r"没有记录|未记录|未测量|未提供|未公布|不详|未知|\bunknown\b"
    r"|\bno\s+(?:measurement|measured|recorded|value|backup|record)\b|\bnot\s+recorded\b|\bunrecorded\b",
    re.I,
)
_COUNT_RE = re.compile(rf"({NUMBER}|{ENG_SMALL_NUMBER})\s*(?:次|attempts?|retries?|times\b)", re.I)
_COUNT_ALIASES = {
    "0": "0", "零": "0", "〇": "0", "zero": "0",
    "1": "1", "一": "1", "one": "1",
    "2": "2", "二": "2", "两": "2", "two": "2",
    "3": "3", "三": "3", "three": "3",
    "4": "4", "四": "4", "four": "4",
    "5": "5", "五": "5", "five": "5",
    "6": "6", "六": "6", "six": "6",
    "7": "7", "七": "7", "seven": "7",
    "8": "8", "八": "8", "eight": "8",
    "9": "9", "九": "9", "nine": "9",
    "10": "10", "十": "10", "ten": "10",
}


def _normalize_count(token):
    key = token.casefold().strip()
    return _COUNT_ALIASES.get(key, key)


def _compound_clause_support(query, body, base, fields, entities, times, result):
    """Allow one record to satisfy one concrete clause of a multi-condition query."""
    if base.get("status") == "needs_authorized_secret_source" or times["needs_reference_date"]:
        return None
    if _UNRECORDED_RE.search(body):
        return None
    if any(
        not check["present"] and check["field"] in ("explicit_year", "measured_not_just_planned", "named_actor", "hash_value", "ip_address")
        for check in base["checks"]
    ):
        return None
    subclauses = [s.strip() for s in re.split(r"且|以及|\band\b|\bplus\b|[，,、；;]", query, flags=re.I) if s.strip()]
    if len(subclauses) < 2:
        return None
    body_clauses = list(clauses(body))
    for sc in subclauses:
        sc_entities = explicit_entities(sc)
        req_entities = sc_entities or entities
        sc_time = explicit_time(sc)
        req_time = sc_time if (sc_time["years"] or sc_time["months"] or sc_time["cadence"]) else times
        for field in fields:
            sc_metrics = metric_names(sc, field)
            if len(sc_metrics) != 1:
                continue
            for begin, end, unit in body_clauses:
                matches = list(re.finditer(VALUES[field], unit, re.I))
                if not matches or NEGATED.search(unit):
                    continue
                if MEASURED.search(query) and PLANNED.search(unit):
                    continue
                unit_entities = explicit_entities(unit)
                for entity in entities:
                    if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(entity) + r"(?![A-Za-z0-9_-])", unit, re.I):
                        unit_entities.add(entity)
                if not req_entities.issubset(unit_entities) or (entities and unit_entities - entities):
                    continue
                unit_time = explicit_time(unit)
                if not (set(req_time["years"]).issubset(unit_time["years"])
                        and set(req_time["months"]).issubset(unit_time["months"])
                        and set(req_time["cadence"]).issubset(unit_time["cadence"])):
                    continue
                unit_metrics = metric_names(unit, field)
                if not compatible_metrics(sorted(sc_metrics), unit_metrics) or len(unit_metrics) > 1:
                    continue
                change = list(re.finditer(r"改为|调整为|变更为|\b(?:changed(?:\s+from\s+[^,.;\n]{1,30})?\s+to|now)\b", unit, re.I))
                current = [m for m in matches if not change or m.start() >= change[-1].end()]
                if len(current) != 1:
                    continue
                match = current[0]
                found = {"field": field, "present": True, "quote": match.group(), "start": begin + match.start(), "end": begin + match.end()}
                evidence = {**found, "clause": unit, "clause_start": begin, "clause_end": end,
                            "entities": sorted(unit_entities), "metrics": sorted(unit_metrics), "time": unit_time}
                binding = {**result["binding"], "evidence": [evidence], "reasons": []}
                untouched = [c for c in base["checks"] if c["field"] not in fields]
                return {**result, "allowed": True, "status": "explicit_value_binding_found", "checks": untouched + [found], "binding": binding}
        sc_counts = (
            {_normalize_count(m.group(1)) for m in _COUNT_RE.finditer(sc)}
            if re.search(
                r"重试|尝试|超时|退避|\bretr(?:y|ies)\b|\battempts?\b|\btimeouts?\b|\bbackoffs?\b",
                sc,
                re.I,
            )
            else set()
        )
        if sc_counts:
            for begin, end, unit in body_clauses:
                if NEGATED.search(unit) or not re.search(r"重试|尝试|\bretr(?:y|ies)\b|\battempts?\b", unit, re.I):
                    continue
                unit_counts = [(m.group(), _normalize_count(m.group(1)), m.start(), m.end()) for m in _COUNT_RE.finditer(unit)]
                matched_count = next((item for item in unit_counts if item[1] in sc_counts), None)
                if not matched_count:
                    continue
                unit_entities = explicit_entities(unit)
                for entity in entities:
                    if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(entity) + r"(?![A-Za-z0-9_-])", unit, re.I):
                        unit_entities.add(entity)
                if not req_entities.issubset(unit_entities) or (entities and unit_entities - entities):
                    continue
                quote, _, mstart, mend = matched_count
                found = {"field": "count", "present": True, "quote": quote, "start": begin + mstart, "end": begin + mend}
                evidence = {**found, "clause": unit, "clause_start": begin, "clause_end": end,
                            "entities": sorted(unit_entities), "metrics": ["retry_count"], "time": explicit_time(unit)}
                binding = {**result["binding"], "evidence": [evidence], "reasons": []}
                return {**result, "allowed": True, "status": "explicit_value_binding_found", "checks": [found], "binding": binding}
    return None


def binding_support(query, body, reference_date=None):
    """Gate one candidate using literal source-clause anchors."""
    if not isinstance(query, str) or not isinstance(body, str):
        raise TypeError("query and body must be strings")
    if reference_date is not None and type(reference_date) is not date:
        raise TypeError("reference_date must be datetime.date")
    base = field_support(query, body, reference_year=reference_date.year if reference_date else 2026)
    fields = binding_fields(query)
    entities = explicit_entities(query)
    times = explicit_time(query, reference_date)
    requested = {
        "entities": sorted(entities),
        "metrics": {f: sorted(metric_names(query, f)) for f in fields},
        "time": times,
    }
    binding = {"coverage": "finite_explicit_anchors_only", "requested": requested, "evidence": [], "reasons": []}
    result = {**base, "binding": binding}
    if not fields:
        binding["coverage"] = "not_checked_non_numeric_request"
        return result
    if times["needs_reference_date"]:
        return {**result, "allowed": False, "status": "binding_reference_date_required"}
    if not base["allowed"]:
        compound = _compound_clause_support(query, body, base, fields, entities, times, result)
        return compound if compound is not None else result
    if any(len(names) > 1 for names in requested["metrics"].values()):
        compound = _compound_clause_support(query, body, base, fields, entities, times, result)
        return compound if compound is not None else {**result, "allowed": False, "status": "binding_ambiguous_requested_metric"}

    bound_checks = []
    for field in fields:
        found = None
        for begin, end, unit in clauses(body):
            matches = list(re.finditer(VALUES[field], unit, re.I))
            if not matches:
                continue
            unit_entities = explicit_entities(unit)
            for entity in entities:
                if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(entity) + r"(?![A-Za-z0-9_-])", unit, re.I):
                    unit_entities.add(entity)
            unit_metrics = metric_names(unit, field)
            unit_time = explicit_time(unit)
            reason = None
            if not entities.issubset(unit_entities):
                reason = "explicit_entity_not_in_value_clause"
            elif entities and unit_entities - entities:
                reason = "multiple_explicit_entities_in_value_clause"
            elif not compatible_metrics(requested["metrics"][field], unit_metrics):
                reason = "requested_metric_not_in_value_clause"
            elif requested["metrics"][field] and len(unit_metrics) > 1:
                reason = "multiple_metrics_in_value_clause"
            elif not set(times["years"]).issubset(unit_time["years"]):
                reason = "requested_year_not_in_value_clause"
            elif not set(times["months"]).issubset(unit_time["months"]):
                reason = "requested_month_not_in_value_clause"
            elif not set(times["cadence"]).issubset(unit_time["cadence"]):
                reason = "requested_period_not_in_value_clause"
            elif NEGATED.search(unit):
                reason = "value_clause_has_explicit_absence"
            elif MEASURED.search(query) and PLANNED.search(unit):
                reason = "planned_clause_does_not_establish_measurement"
            if reason:
                binding["reasons"].append({"field": field, "reason": reason, "start": begin, "end": end})
                continue
            change = list(re.finditer(r"改为|调整为|变更为|\b(?:changed(?:\s+from\s+[^,.;\n]{1,30})?\s+to|now)\b", unit, re.I))
            current = [m for m in matches if not change or m.start() >= change[-1].end()]
            if len(current) != 1:
                binding["reasons"].append({"field": field, "reason": "multiple_or_unresolved_values", "start": begin, "end": end})
                continue
            match = current[0]
            found = {"field": field, "present": True, "quote": match.group(), "start": begin + match.start(), "end": begin + match.end()}
            binding["evidence"].append({
                **found,
                "clause": unit,
                "clause_start": begin,
                "clause_end": end,
                "entities": sorted(unit_entities),
                "metrics": sorted(unit_metrics),
                "time": unit_time,
            })
            break
        if found is None:
            bound_checks.append({"field": field, "present": False, "quote": None, "start": None, "end": None})
        else:
            bound_checks.append(found)
    passed = all(check["present"] for check in bound_checks)
    if not passed:
        compound = _compound_clause_support(query, body, base, fields, entities, times, result)
        if compound is not None:
            return compound
    untouched = [check for check in base["checks"] if check["field"] not in fields]
    return {
        **result,
        "allowed": passed,
        "status": "explicit_value_binding_found" if passed else "explicit_value_binding_not_established",
        "checks": untouched + bound_checks,
    }


def filter_bound_candidates(query, candidates, documents, reference_date=None):
    """Filter at most two ALREADY scoped/budgeted candidates; never add records."""
    if len(candidates) > 2 or len(set(candidates)) != len(candidates):
        raise ValueError("Expected at most two unique scoped candidates")
    parsed_date = parse_reference_date(reference_date)
    selected = []
    decisions = {}
    for key in candidates:
        decision = binding_support(query, documents[key]["body"], parsed_date)
        decisions[key] = decision
        if decision["allowed"]:
            selected.append(key)
    return {"selected": selected, "decisions": decisions}
