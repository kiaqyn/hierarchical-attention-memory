"""Frozen synthetic experiment. No input pointing at real memories is accepted."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import statistics
import tempfile
import time

import numpy as np
from local_semantic import LocalEncoder, SemanticCandidates, accepted_dense, reciprocal_rank_fusion


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(fn,*args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = fn(*args)
    if rc:
        raise RuntimeError(output.getvalue())
    return json.loads(output.getvalue()) if output.getvalue().lstrip().startswith("{") else None


def offline_only(*args,**kwargs):
    raise RuntimeError("Network is disabled inside the local evaluation process")


def evaluate(base, fixture, model, output):
    output.mkdir(parents=True,exist_ok=True)
    freeze = json.loads((fixture/"freeze-manifest.json").read_text(encoding="utf8"))
    for name,item in freeze["files"].items():
        if hashlib.sha256((fixture/(name+".json")).read_bytes()).hexdigest()!=item["sha256"]:
            raise ValueError("Frozen fixture changed: "+name)
    corpus = json.loads((fixture/"corpus.json").read_text(encoding="utf8"))
    dev = json.loads((fixture/"dev.json").read_text(encoding="utf8"))
    hfa = load_module(base/"scripts/hfa_manager.py","hfa_baseline")
    adapter = load_module(base/"scripts/hfa_mcp.py","hfa_adapter")
    # Installed dependency loading and local sessions happen with sockets denied.
    socket.create_connection = offline_only
    socket.socket.connect = offline_only
    encoder = LocalEncoder(model)
    with tempfile.TemporaryDirectory(prefix="hfa-semantic-only-synthetic-") as temp:
        root = Path(temp)/"memory"
        invoke(hfa.cmd_init,root)
        mapping,reverse = {},{}
        for doc in corpus:
            result = invoke(hfa.cmd_add_topic,root,doc["title"],"fixture-"+doc["id"],doc["body"],
                            None,doc["project"],"fixture:"+doc["id"],
                            [mapping[key] for key in doc["supersedes"]],"fixture-"+doc["id"],True)
            mapping[doc["id"]] = result["id"]
            reverse[result["id"]] = doc["id"]
            if doc["status"]!="ACTIVE":
                path = root/result["path"]
                value = json.loads(path.read_text(encoding="utf8"))
                value["status"] = doc["status"]
                path.write_text(json.dumps(value,ensure_ascii=False),encoding="utf8")
        invoke(hfa.cmd_reindex,root)
        state = hfa.parse_directory_nodes(root/"directory.md")
        chunks = hfa._chunks(root,state)
        reverse["chunk_0001"] = "initial"
        docs = [{"id":reverse[chunk["id"]], "title":chunk["title"],"body":"\n".join(chunk["summary"]),
                 "project":chunk["project"],"status":chunk["status"],
                 "supersedes":[reverse.get(key,key) for key in chunk["supersedes"]]} for chunk in chunks]
        chunk_by_key = {reverse[chunk["id"]]:chunk for chunk in chunks}
        protected = {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in root.rglob("*") if p.is_file() and p.name!=".hfa.lock"
                     and not p.relative_to(root).as_posix().startswith("history/")}
        started = time.perf_counter()
        index = SemanticCandidates(encoder,docs)
        document_encode_seconds = time.perf_counter()-started

        def budget(keys):
            selected,text = [],""
            for key in keys:
                if key not in index.ids:
                    raise ValueError("Forbidden semantic candidate: "+key)
                addition = text+("\n\n" if text else "")+hfa._chunk_text(chunk_by_key[key])
                if len(selected)>=2 or hfa.measure_tokens(addition)["count"]>600:
                    continue
                selected.append(key)
                text = addition
            return selected

        def observations(cases):
            queries = [hfa._positive_query(case["query"]) for case in cases]
            started = time.perf_counter()
            vectors = encoder.encode(queries)
            batch_seconds = time.perf_counter()-started
            rows = []
            for case,query,vector in zip(cases,queries,vectors):
                blocked = not query.strip() or bool(adapter._clarification_question(query))
                lexical = [] if blocked else [reverse[item["id"]] for item in invoke(hfa.cmd_route,root,query)["selected"]]
                ranks = [] if blocked else index.ranked(vector)
                rows.append({**case,"lexical":lexical,"dense_ranking":ranks,"blocked":blocked})
            return rows,batch_seconds

        def methods(row,threshold,margin):
            dense = accepted_dense(row["dense_ranking"],threshold,margin)
            return {"lexical":row["lexical"],
                    "semantic_ungated":budget([key for key,_ in row["dense_ranking"][:2]]),
                    "semantic_gated":budget(dense),
                    "hybrid_gated":budget(reciprocal_rank_fusion(row["lexical"],dense))}

        dev_rows,dev_seconds = observations(dev)
        candidates = []
        # Integer grid avoids arange producing 1.0000000000000002.
        for step in range(140,201):
            threshold = step/200
            for margin in (0.0,0.01,0.02,0.04,0.06):
                answers = [(row,methods(row,float(threshold),margin)["hybrid_gated"]) for row in dev_rows]
                additional_false = sum(bool(ids) and not row["lexical"] and row["expected"] is None for row,ids in answers)
                hits = sum(row["expected"] in ids for row,ids in answers if row["expected"])
                extras = sum(len([key for key in ids if key!=row["expected"]]) for row,ids in answers if row["expected"])
                candidates.append((additional_false,-hits,extras,-float(threshold),-margin,float(threshold),margin))
        chosen = min(candidates)
        threshold,margin = chosen[-2:]
        calibration = {"threshold":threshold,"margin":margin,"additional_dev_false_hits":chosen[0],
                       "dev_hits":-chosen[1],"dev_positives":sum(bool(c["expected"]) for c in dev),
                       "objective":"minimize new no-answer returns, then maximize recall, then minimize extras; dev only",
                       "fixture_hashes":freeze["files"]}
        # Persist the frozen decision BEFORE loading/evaluating the test queries.
        (output/"calibration.json").write_text(json.dumps(calibration,indent=2),encoding="utf8")
        test = json.loads((fixture/"test.json").read_text(encoding="utf8"))
        test_rows,test_seconds = observations(test)

        def summarize(rows):
            report = {}
            for method in ("lexical","semantic_ungated","semantic_gated","hybrid_gated"):
                predictions = [(row,methods(row,threshold,margin)[method]) for row in rows]
                positive = [(row,ids) for row,ids in predictions if row["expected"]]
                negative = [(row,ids) for row,ids in predictions if row["expected"] is None]
                report[method] = {"hits_at_2":sum(row["expected"] in ids for row,ids in positive),
                    "positive_cases":len(positive),"top1_correct":sum(bool(ids) and ids[0]==row["expected"] for row,ids in positive),
                    "no_answer_returns":sum(bool(ids) for _,ids in negative),"negative_cases":len(negative),
                    "extra_candidates_on_positive":sum(len([key for key in ids if key!=row["expected"]]) for row,ids in positive),
                    "no_answer_by_kind":{kind:sum(bool(ids) for row,ids in negative if row["kind"]==kind)
                                         for kind in sorted({row["kind"] for row,_ in negative})}}
            return report

        cases = [{**row,"predictions":methods(row,threshold,margin)} for row in test_rows]
        health = invoke(hfa.cmd_status,root,True)
        after = {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob("*") if p.is_file() and p.name!=".hfa.lock"
                 and not p.relative_to(root).as_posix().startswith("history/")}
        warm = []
        for query in [case["query"] for case in dev[:9]]:
            started = time.perf_counter()
            encoder.encode([query])
            warm.append((time.perf_counter()-started)*1000)
        result = {"kind":"frozen developer-authored synthetic prototype; not independent external evaluation",
            "model":encoder.manifest,"baseline_sha256":hashlib.sha256((base/"scripts/hfa_manager.py").read_bytes()).hexdigest(),
            "calibration":calibration,"dev":summarize(dev_rows),"test":summarize(test_rows),"cases":cases,
            "boundaries":{"encoded_ids":index.ids,"excluded_ids":[doc["id"] for doc in docs if doc["id"] not in index.ids],
                          "python_socket_calls_blocked":True,"system_network_disabled":False,
                          "inference_uses_only_local_files":True,"real_memories_opened":False,
                          "synthetic_source_and_index_unchanged":protected==after,"health_passed":not health["problems"],
                          "candidate_budget":600,"candidate_limit":2,"budget_method":"HFA measurement; JSON metadata excluded",
                          "memory_files_only_in_task_owned_temporary_directory":True},
            "performance":{"cpu_threads":2,"load_seconds":encoder.load_seconds,"document_encode_seconds":document_encode_seconds,
                           "dev_query_batch_seconds":dev_seconds,"test_query_batch_seconds":test_seconds,
                           "single_query_warm_ms_samples":warm,"single_query_warm_median_ms":statistics.median(warm)},
            "limits":["Similarity is not evidence sufficiency or calibrated correctness probability",
                      "No-answer return means a candidate was returned, not that an answer was fabricated",
                      "No native host integration, online mutation handling, persistent vector cache or reranker",
                      "Only this quantized E5 model and synthetic fixtures were tested"]}
        (output/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
        print(json.dumps({"calibration":calibration,"test":result["test"],"performance":result["performance"],"boundaries":result["boundaries"]},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("baseline","fixture","model","output"):
        parser.add_argument("--"+name,required=True,type=Path)
    args = parser.parse_args()
    evaluate(args.baseline.resolve(),args.fixture.resolve(),args.model.resolve(),args.output.resolve())
