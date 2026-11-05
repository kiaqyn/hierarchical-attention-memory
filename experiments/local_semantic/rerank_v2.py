"""Local pair relevance plus LIMITED, inspectable requested-field checks.

Relevance and field presence are not proof of entailment or answer correctness.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import time
import numpy as np

NUMBER = r"(?:\d+(?:[.,]\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
VALUES = {
    "currency": rf"(?:[$¥￥]|\b(?:USD|CNY|RMB)\s*){NUMBER}|{NUMBER}\s*(?:元|美元|人民币|USD|CNY|RMB|dollars?)",
    "percentage": rf"{NUMBER}\s*[%％]|百分之\s*{NUMBER}",
    "duration": rf"{NUMBER}\s*(?:天|日|小时|分钟|毫秒|秒|days?|hours?|minutes?|milliseconds?|seconds?)",
    "hash_value": r"(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])",
    "storage_size": rf"{NUMBER}\s*(?:GiB|GB|MiB|MB|KiB|KB|字节|bytes?)\b",
    "named_actor": r"(?:由|批准人[:：]?|负责人[:：]?|approved by\s+|authorized by\s+)[^。；;\n]{1,35}(?:批准|确认|[。.;]|$)",
}


def requested_fields(query):
    fields = []
    tests = {
        "currency": r"多少(?:钱|元|美元)|费用(?:是|为|多少)|预算(?:是|为|多少|额度)|(?:how much|purchase price|cost in|spending budget|monthly budget|allocated|what amount)",
        "percentage": r"占比|比例|百分比|率[^。？?]{0,12}(?:多少|几)|\b(?:percentage|fraction|share|percent)\b|what[^?]{0,30}\brate\b",
        "duration": r"多久|几天|多长时间|how long|how many days|\blifetime\b|retention period",
        "hash_value": r"(?:实际|具体).{0,20}(?:哈希|校验值)|(?:哈希值|校验值).{0,8}(?:多少|什么)|actual.{0,25}(?:hash|digest)|hash value",
        "storage_size": r"多少\s*(?:GB|GiB|MB|MiB|字节)|how many\s+(?:gigabytes|megabytes|bytes)",
        "named_actor": r"谁|哪位|\bwho\b",
    }
    for name,pattern in tests.items():
        if re.search(pattern,query,re.I):
            fields.append(name)
    return fields


def field_support(query, body, reference_year=2026):
    """Return exact source spans, never a generated answer or confidence claim."""
    if re.search(r"(?:密码|口令).{0,6}(?:多少|什么|是)|what.{0,45}password|(?:give|show).{0,30}password",query,re.I):
        return {"allowed":False,"status":"needs_authorized_secret_source","checks":[]}
    checks = []
    for name in requested_fields(query):
        match = re.search(VALUES[name],body,re.I)
        checks.append({"field":name,"present":bool(match),
                       "quote":match.group() if match else None,
                       "start":match.start() if match else None,"end":match.end() if match else None})
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)",query))
    if re.search(r"去年|last year",query,re.I):
        years.add(str(reference_year-1))
    for year in sorted(years):
        match = re.search(re.escape(year),body)
        checks.append({"field":"explicit_year","present":bool(match),"quote":year if match else None,
                       "start":match.start() if match else None,"end":match.end() if match else None})
    asks_measured = bool(re.search(r"实际|实测|measured|observed|actually",query,re.I))
    planned_only = bool(re.search(r"计划|预计|目标|planned|expected|target",body,re.I)) and not re.search(r"实际|实测|验证|measured|observed|verified",body,re.I)
    if asks_measured and planned_only:
        checks.append({"field":"measured_not_just_planned","present":False,"quote":None,"start":None,"end":None})
    allowed = all(check["present"] for check in checks)
    return {"allowed":allowed,
            "status":("requested_fields_present" if allowed else "requested_fields_missing") if checks else "relevance_only_unverified",
            "checks":checks}


class LocalReranker:
    def __init__(self, directory, threads=2):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        directory = Path(directory)
        self.manifest = json.loads((directory/"manifest.json").read_text(encoding="utf8"))
        for name in ("model.onnx","tokenizer.json"):
            entry = next(f for f in self.manifest["files"] if f["local"]==name)
            if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=entry["sha256"]:
                raise ValueError("Reranker checksum changed: "+name)
        started = time.perf_counter()
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory/"model.onnx"),sess_options=options,
                                           providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(directory/"tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1,pad_token="<pad>")
        self.tokenizer.enable_truncation(max_length=512)
        self.load_seconds = time.perf_counter()-started

    def score_pairs(self,pairs,batch_size=8):
        result = []
        for start in range(0,len(pairs),batch_size):
            encodings = self.tokenizer.encode_batch(pairs[start:start+batch_size])
            available = {"input_ids":np.array([item.ids for item in encodings],dtype=np.int64),
                         "attention_mask":np.array([item.attention_mask for item in encodings],dtype=np.int64),
                         "token_type_ids":np.array([item.type_ids for item in encodings],dtype=np.int64)}
            logits = self.session.run(None,{item.name:available[item.name] for item in self.session.get_inputs()})[0].reshape(-1)
            if len(logits)!=len(encodings) or not np.isfinite(logits).all():
                raise ValueError("Invalid pair-score shape or nonfinite score")
            scores = 1/(1+np.exp(-np.clip(logits,-60,60)))
            result.extend(map(float,scores))
        return result


def select_verified(ranking, support, threshold, margin):
    if (not math.isfinite(threshold) or not 0<=threshold<=1 or not math.isfinite(margin)
            or not 0<=margin<=1 or any(not math.isfinite(score) for _,score in ranking)):
        raise ValueError("Invalid pair relevance gate")
    candidates = [(key,score) for key,score in ranking if support[key]["allowed"]]
    if not candidates or candidates[0][1]<threshold:
        return []
    if len(candidates)>1 and candidates[0][1]-candidates[1][1]<margin:
        return []
    # One final candidate reduces irrelevant context; it is still NOT an answer.
    return [candidates[0][0]]


def choose_with_preservation(old_candidates, ranking, support, threshold, margin):
    """Keep an existing field-supported candidate; use pair scoring to fill gaps.

    This avoids silently discarding the older retriever's strengths, but may
    retain an older false match when no covered field can disprove it.
    """
    if (not math.isfinite(threshold) or not 0<=threshold<=1 or not math.isfinite(margin)
            or not 0<=margin<=1 or any(not math.isfinite(score) for _,score in ranking)):
        raise ValueError("Invalid pair relevance gate")
    supported_old = [key for key in old_candidates if support[key]["allowed"]]
    if supported_old:
        # The older candidate list is already budgeted. Keep its second source
        # too: a low pair score must not silently discard that existing evidence.
        return supported_old[:2]
    return select_verified(ranking,support,threshold,margin)
