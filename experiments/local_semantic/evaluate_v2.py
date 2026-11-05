"""V2 offline experiment. New test data is loaded only after dev calibration."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time

from evaluate import invoke, load_module, offline_only
from local_semantic import LocalEncoder, SemanticCandidates, accepted_dense, reciprocal_rank_fusion
from rerank_v2 import LocalReranker, field_support, select_verified


def run(base, fixture, encoder_dir, reranker_dir, output, v1_calibration, legacy_fixture):
    output.mkdir(parents=True,exist_ok=True)
    freeze = json.loads((fixture/"freeze-manifest.json").read_text(encoding="utf8"))
    for name,item in freeze["files"].items():
        if hashlib.sha256((fixture/(name+".json")).read_bytes()).hexdigest()!=item["sha256"]:
            raise ValueError("Frozen V2 data changed: "+name)
    corpus = json.loads((fixture/"corpus.json").read_text(encoding="utf8"))
    dev = json.loads((fixture/"dev.json").read_text(encoding="utf8"))
    v1_params = json.loads(v1_calibration.read_text(encoding="utf8"))
    hfa = load_module(base/"scripts/hfa_manager.py","hfa_v2_baseline")
    adapter = load_module(base/"scripts/hfa_mcp.py","hfa_v2_adapter")
    socket.create_connection = offline_only
    socket.socket.connect = offline_only
    encoder = LocalEncoder(encoder_dir)
    reranker = LocalReranker(reranker_dir)

    with tempfile.TemporaryDirectory(prefix="hfa-v2-synthetic-") as temp:
        root = Path(temp)/"memory"
        invoke(hfa.cmd_init,root)
        mapping,reverse = {},{"chunk_0001":"initial"}
        for doc in corpus:
            result = invoke(hfa.cmd_add_topic,root,doc["title"],"fixture-"+doc["id"],doc["body"],
                            None,doc["project"],"fixture:"+doc["id"],
                            [mapping[key] for key in doc["supersedes"]],"fixture-"+doc["id"],True)
            mapping[doc["id"]] = result["id"]
            reverse[result["id"]] = doc["id"]
            if doc["status"]!="ACTIVE":
                path = root/result["path"]
                raw = json.loads(path.read_text(encoding="utf8"))
                raw["status"] = doc["status"]
                path.write_text(json.dumps(raw,ensure_ascii=False),encoding="utf8")
        invoke(hfa.cmd_reindex,root)
        chunks = hfa._chunks(root,hfa.parse_directory_nodes(root/"directory.md"))
        docs = [{"id":reverse[c["id"]],"title":c["title"],"body":"\n".join(c["summary"]),
                 "project":c["project"],"status":c["status"],
                 "supersedes":[reverse[key] for key in c["supersedes"]]} for c in chunks]
        chunks_by_key = {reverse[c["id"]]:c for c in chunks}
        protected = {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in root.rglob("*") if p.is_file() and p.name!=".hfa.lock"
                     and not p.relative_to(root).as_posix().startswith("history/")}
        index = SemanticCandidates(encoder,docs)
        allowed = {doc["id"]:doc for doc in index.documents}

        def budget(keys):
            selected,text = [],""
            for key in keys:
                if key not in allowed:
                    raise ValueError("Forbidden candidate: "+key)
                addition = text+("\n\n" if text else "")+hfa._chunk_text(chunks_by_key[key])
                if len(selected)>=2 or hfa.measure_tokens(addition)["count"]>600:
                    continue
                selected.append(key)
                text = addition
            return selected

        def observe(cases):
            queries = [hfa._positive_query(case["query"]) for case in cases]
            started = time.perf_counter()
            vectors = encoder.encode(queries)
            rows,pairs,locations = [],[],[]
            for i,(case,query,vector) in enumerate(zip(cases,queries,vectors)):
                blocked = not query.strip() or bool(adapter._clarification_question(query))
                lexical = [] if blocked else [reverse[c["id"]] for c in invoke(hfa.cmd_route,root,query)["selected"]]
                dense = [] if blocked else index.ranked(vector)
                pool = list(dict.fromkeys(lexical+[key for key,_ in dense[:8]]))
                support = {key:field_support(case["query"],allowed[key]["body"]) for key in pool}
                for key in pool:
                    pairs.append((query,allowed[key]["title"]+"\n"+allowed[key]["body"]))
                    locations.append((i,key))
                rows.append({**case,"lexical":lexical,"dense_ranking":dense,"pool":pool,
                             "field_support":support,"reranking":[],"blocked":blocked})
            scores = reranker.score_pairs(pairs)
            for (i,key),score in zip(locations,scores):
                rows[i]["reranking"].append((key,score))
            for row in rows:
                row["reranking"].sort(key=lambda pair:(-pair[1],pair[0]))
            return rows,{"seconds":time.perf_counter()-started,"queries":len(cases),"local_pairs":len(pairs)}

        def predict(row,threshold,margin):
            v1_dense = accepted_dense(row["dense_ranking"],v1_params["threshold"],v1_params["margin"])
            all_supported = {key:{"allowed":True} for key in row["pool"]}
            return {"lexical":row["lexical"],
                    "v1_hybrid":budget(reciprocal_rank_fusion(row["lexical"],v1_dense)),
                    "v2_rerank_only":budget(select_verified(row["reranking"],all_supported,threshold,margin)),
                    "v2_with_fields":budget(select_verified(row["reranking"],row["field_support"],threshold,margin))}

        def metrics(rows,method,threshold,margin):
            predictions = [(row,predict(row,threshold,margin)[method]) for row in rows]
            positive = [(row,ids) for row,ids in predictions if row["expected"]]
            negative = [(row,ids) for row,ids in predictions if row["expected"] is None]
            correct = sum(bool(ids) and ids[0]==row["expected"] for row,ids in positive)
            hits = sum(row["expected"] in ids for row,ids in positive)
            false = sum(bool(ids) for _,ids in negative)
            returned = sum(bool(ids) for _,ids in predictions)
            return {"positive_cases":len(positive),"correct_top1":correct,"hits_at_2":hits,
                    "negative_cases":len(negative),"no_answer_returns":false,"returned_queries":returned,
                    "returned_query_precision":correct/returned if returned else None,
                    "balanced_decision_accuracy":.5*(correct/len(positive)+(len(negative)-false)/len(negative)),
                    "extra_candidates_on_positive":sum(len([key for key in ids if key!=row["expected"]]) for row,ids in positive),
                    "language":{lang:{"total":sum(("English" if row["query"][0].isascii() else "Chinese")==lang for row,_ in positive),
                                      "correct_top1":sum(bool(ids) and ids[0]==row["expected"] for row,ids in positive if ("English" if row["query"][0].isascii() else "Chinese")==lang)}
                                for lang in ("Chinese","English")},
                    "no_answer_by_kind":{kind:sum(bool(ids) for row,ids in negative if row["kind"]==kind)
                                         for kind in sorted({row["kind"] for row,_ in negative})}}

        dev_rows,dev_time = observe(dev)
        old_dev = metrics(dev_rows,"v1_hybrid",0,0)
        choices = []
        for step in range(0,21):
            for margin in (0,.025,.05,.1,.2):
                threshold = step/20
                measure = metrics(dev_rows,"v2_with_fields",threshold,margin)
                choices.append((measure["no_answer_returns"]>old_dev["no_answer_returns"],
                                -measure["balanced_decision_accuracy"],measure["no_answer_returns"],
                                -measure["correct_top1"],-threshold,-margin,threshold,margin))
        threshold,margin = min(choices)[-2:]
        calibration = {"threshold":threshold,"margin":margin,"v1_params":v1_params,
            "objective":"Dev only: no more no-answer returns than V1; maximize balanced top1/abstention accuracy, then reduce false returns",
            "fixture":freeze,"dev_v1":old_dev,"dev_v2":metrics(dev_rows,"v2_with_fields",threshold,margin)}
        (output/"calibration-v2.json").write_text(json.dumps(calibration,ensure_ascii=False,indent=2),encoding="utf8")
        # No V2 test question is loaded/scored until the decision is frozen above.
        test = json.loads((fixture/"test.json").read_text(encoding="utf8"))
        test_rows,test_time = observe(test)
        legacy = json.loads((legacy_fixture/"test.json").read_text(encoding="utf8"))
        legacy_rows,legacy_time = observe(legacy)
        methods = ("lexical","v1_hybrid","v2_rerank_only","v2_with_fields")
        test_report = {method:metrics(test_rows,method,threshold,margin) for method in methods}
        legacy_report = {method:metrics(legacy_rows,method,threshold,margin) for method in methods}
        cases = [{**row,"predictions":predict(row,threshold,margin)} for row in test_rows]
        legacy_cases = [{**row,"predictions":predict(row,threshold,margin)} for row in legacy_rows]
        after = {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob("*") if p.is_file() and p.name!=".hfa.lock"
                 and not p.relative_to(root).as_posix().startswith("history/")}
        health = invoke(hfa.cmd_status,root,True)
        report = {"kind":"developer-authored V2 synthetic; not independent external validation", "calibration":calibration,
            "model":encoder.manifest,"reranker":reranker.manifest,
            "test":test_report,"cases":cases,"legacy_known_regression":legacy_report,"legacy_cases":legacy_cases,
            "corpus":docs,"boundaries":{"eligible":index.ids,"excluded":[doc["id"] for doc in docs if doc["id"] not in allowed],
                "source_and_index_unchanged":protected==after,"health_passed":not health["problems"],
                "real_memory_opened":False,"python_socket_calls_blocked":True,"system_network_disabled":False,
                "daily_installation_changed":False,"final_candidate_budget":600,"final_v2_candidate_limit":1,
                "prefetch_dense":8,"prefetch_also_lexical":True},
            "timing":{"dev":dev_time,"test":test_time,"legacy":legacy_time,
                      "reranker_load_seconds":reranker.load_seconds},
            "limits":["Field checks only cover a finite set of explicit information types",
                      "Field presence and reranker relevance do not prove entailment or generated answer correctness",
                      "Legacy viewed questions are development/regression data, not new holdout",
                      "No real host integration or vector lifecycle synchronization tested"]}
        (output/"result-v2.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
        print(json.dumps({"calibration":{"threshold":threshold,"margin":margin},"test":test_report,
                          "legacy":legacy_report,"timing":report["timing"],"boundaries":report["boundaries"]},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("baseline","fixture","encoder","reranker","output","v1-calibration","legacy-fixture"):
        parser.add_argument("--"+name,type=Path,required=True)
    args = parser.parse_args()
    run(args.baseline.resolve(),args.fixture.resolve(),args.encoder.resolve(),args.reranker.resolve(),args.output.resolve(),
        args.v1_calibration.resolve(),args.legacy_fixture.resolve())
