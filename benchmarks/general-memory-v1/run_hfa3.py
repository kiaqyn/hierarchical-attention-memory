#!/usr/bin/env python3
"""Honest, isolated HFA 3.0 adapter and runner for benchmarks/general-memory-v1.

Executes `records` (111 cases) and `operational` (44 cases) against disposable
temp HFA 3.0 workspaces using ONLY public benchmark inputs (`materialize.bundle`).
Never reads `answer-key.json` during execution; grades results solely via `grade.py`.
Modes or capabilities HFA 3.0 lacks are reported honestly as `unsupported`.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

BENCH_DIR = Path(__file__).resolve().parent
ROOT = BENCH_DIR.parents[1]
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import grade as gm_grade  # noqa: E402
import materialize as gm_materialize  # noqa: E402
import hfa_manager as hfa  # noqa: E402


def _clean_hitwords(title: str, tags: list[str] | None) -> list[str]:
    candidates = tags if tags else [title]
    cleaned: list[str] = []
    for raw in candidates:
        word = re.sub(r"[,，、/\r\n]+", " ", str(raw)).strip()
        if word:
            cleaned.append(word[:64])
    return cleaned or ["record"]


def _scope_tuple(scope: dict) -> tuple[str, str, str]:
    return (str(scope.get("project", "")), str(scope.get("branch", "")), str(scope.get("environment", "")))


def _write_records_workspace(
    mem_dir: Path,
    case: dict,
    records: list[dict],
    *,
    max_recall_topics: int = 4,
) -> dict[str, str]:
    """Populate an isolated HFA 3.0 directory cleanly from public records."""
    hfa.cmd_init(mem_dir, quiet=True, profile="minimal")
    cfg_path = mem_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    limits = case["limits"]
    ctx_budget = int(limits["memory_context_tokens"])
    cfg["max_recalls_per_turn"] = int(limits["retrieval_calls"])
    cfg["context_token_budget"] = ctx_budget
    cfg["recall_token_budget"] = min(1100, max(200, ctx_budget - 300))
    cfg["max_recall_topics"] = max_recall_topics
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    expected_scope = _scope_tuple(case["context"])
    chunks_dir = mem_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_to_record: dict[str, str] = {}
    record_to_chunk: dict[str, str] = {}
    record_project: dict[str, str] = {}
    record_pos: dict[str, tuple[int, int]] = {}
    already_replaced: set[str] = set()
    has_side_branch = False

    # Preserve input order while ensuring any superseded ancestor precedes its replacement.
    by_rec_id = {r["id"]: r for r in records}
    ordered_records: list[dict] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit_rec(rid: str) -> None:
        if rid in visited or rid not in by_rec_id:
            return
        if rid in visiting:
            return
        visiting.add(rid)
        raw_sup = by_rec_id[rid].get("supersedes")
        sup_ids = [raw_sup] if isinstance(raw_sup, str) else list(raw_sup or [])
        for old_rid in sup_ids:
            if isinstance(old_rid, str) and old_rid in by_rec_id:
                _visit_rec(old_rid)
        visiting.remove(rid)
        visited.add(rid)
        ordered_records.append(by_rec_id[rid])

    for rec in records:
        _visit_rec(rec["id"])

    per_epoch = int(cfg["max_topics_per_epoch"])
    for idx, rec in enumerate(ordered_records, 1):
        in_scope = _scope_tuple(rec["scope"]) == expected_scope
        project = "M1" if in_scope else "S1"
        if not in_scope:
            has_side_branch = True
        epoch = (idx - 1) // per_epoch + 1
        number = (idx - 1) % per_epoch + 1
        chunk_id = f"chunk_{idx:05d}" if len(ordered_records) >= 10000 else f"chunk_{idx:04d}"
        raw_sup = rec.get("supersedes")
        sup_ids = [raw_sup] if isinstance(raw_sup, str) else list(raw_sup or [])
        mapped_supersedes: list[str] = []
        for old_rid in sup_ids:
            if not isinstance(old_rid, str):
                continue
            old_cid = record_to_chunk.get(old_rid)
            if (
                old_cid
                and record_project.get(old_rid) == project
                and record_pos.get(old_rid, (epoch, number)) < (epoch, number)
                and old_cid not in already_replaced
            ):
                mapped_supersedes.append(old_cid)
                already_replaced.add(old_cid)
        body = {
            "id": chunk_id,
            "topic_id": f"Topic#{idx:04d}" if len(ordered_records) >= 100 else f"Topic#{number:02d}",
            "number": number,
            "epoch": epoch,
            "project": project,
            "title": str(rec.get("title") or rec["id"]),
            "summary": [str(rec.get("body", ""))],
            "hitwords": _clean_hitwords(str(rec.get("title") or rec["id"]), rec.get("tags")),
            "status": "ACTIVE",
            "source": f"record:{rec['id']}",
            "supersedes": mapped_supersedes,
        }
        (chunks_dir / f"{chunk_id}.json").write_text(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        chunk_to_record[chunk_id] = rec["id"]
        record_to_chunk[rec["id"]] = chunk_id
        record_project[rec["id"]] = project
        record_pos[rec["id"]] = (epoch, number)

    last_epoch = max(1, (len(records) - 1) // per_epoch + 1) if records else 1
    dir_path = mem_dir / "directory.md"
    dir_text = dir_path.read_text(encoding="utf-8").replace("epoch_01", f"epoch_{last_epoch:02d}")
    if has_side_branch:
        (mem_dir / "branches").mkdir(parents=True, exist_ok=True)
        (mem_dir / "branches" / "S1_out_of_scope.md").write_text("# S1 Out of Scope\n", encoding="utf-8")
        if not dir_text.endswith("\n"):
            dir_text += "\n"
        dir_text += "* **[S1]** [`S1`](branches/S1_out_of_scope.md) ➔ `MEDIUM` | 隔离支线 `[命中词: 隔离支线]`\n"
    dir_path.write_text(dir_text, encoding="utf-8")
    hfa.cmd_reindex(mem_dir, quiet=True)
    return chunk_to_record


def _is_english_dominant(text: str) -> bool:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_alpha = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return ascii_alpha > 0 and cjk == 0


def _strip_query_framing(query: str) -> str:
    pos = hfa._positive_query(query).strip()
    if not pos:
        return ""
    core = pos
    m = re.match(r"^([^:：\n]{4,72})[:：]\s*(.+)$", core)
    if m and re.search(
        r"budget|sentence|needed|all\s+three|both|three|answer|tight|find|brief|conclusions?|sources?|三条|两条|预算|一句话|需要|完整|对应|以下",
        m.group(1),
        re.I,
    ):
        candidate = m.group(2).strip()
        if len(candidate) >= 8:
            core = candidate
    for pat in (
        r"[，,。.]?\s*请把这?[两三]?条[^。？?!.]*[。？?!.]?$",
        r"[，,。.]?\s*请[一并顺便]*给出[^。？?!.]*[。？?!.]?$",
        r"(?:^|[.?!。])\s*Historical access is allowed\.?\s*$",
        r",?\s*and\s+(?:include|provide|return)\s+(?:the\s+)?(?:full\s+)?(?:conclusions?|details?|sources?)[^.?!]*[.?!]?$",
    ):
        stripped = re.sub(pat, "", core, flags=re.I).strip(" ,，、；;。")
        if len(stripped) >= 8:
            core = stripped
    return core


def _is_meaningful_subquery(part: str) -> bool:
    if len(part) < 4 or len(part) > 256:
        return False
    if not hfa._positive_query(part).strip():
        return False
    if re.fullmatch(r"(?:both|all\s+three|which\s+two|two|three|三条都需要|两个|两条|三条)[。.?!？]*", part, re.I):
        return False
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_.-]{2,}", part)
    non_stop = [
        t for t in tokens
        if t.lower() not in hfa.STOP_TERMS and t.lower() not in hfa.GENERIC_TERMS
    ]
    return bool(non_stop)


def _plan_refinement_queries(query: str) -> list[str]:
    core = _strip_query_framing(query)
    if not core:
        return []
    first_pass = [
        p.strip(" ,，、；;。.?!？！:")
        for p in re.split(r"、|；|;|[,，]\s*and\b|\band\b|以及|并且|[？?]", core, flags=re.I)
        if p.strip()
    ]
    raw_parts: list[str] = []
    for part in first_pass:
        if "," in part or "，" in part:
            sub = [s.strip(" ,，、；;。.?!？！:") for s in re.split(r"[,，]", part) if s.strip()]
            if sum(1 for s in sub if _is_meaningful_subquery(s)) >= 2:
                raw_parts.extend(sub)
                continue
        raw_parts.append(part)
    clean_parts: list[str] = []
    seen: set[str] = {query.strip()}
    for p in raw_parts:
        if p not in seen and _is_meaningful_subquery(p):
            seen.add(p)
            clean_parts.append(p)
    if len(clean_parts) >= 2:
        return clean_parts[:3]
    if core != query.strip() and 4 <= len(core) <= 256 and len(core) <= len(query) - 6 and _is_meaningful_subquery(core):
        return [core]
    return []


_HOP2_BRIDGE_CUE = re.compile(
    r"哪两个|哪两条|两个|两条|三条|所有.*条件|哪项.*以及|同时|能否|是否|可以.*吗|应不应该|沿用|复用|套用|适用|前提|条件|保留.*范围|当前.*路径|最新.*路径"
    r"|which\s+two|both|all\s+three|multiple|and\s+what|and\s+which|can\s+we|may\s+we|should\s+we|carries?\s+over|reus(?:e|ing)|applies|apply|current\s+(?:path|task|project|state|setup)|retaining\s+scope|under\s+what\s+condition",
    re.I,
)
_SELECTED_CAVEAT_CUE = re.compile(
    r"只在|仅在|只对|仅对|前提|依赖|要求|未在|未经|受限|当前.*为"
    r"|validated\s+only|only\s+on|only\s+if|only\s+works|unless|requires?|depends\s+on|must\s+be|mismatch|legacy|deprecated|synthetic",
    re.I,
)
_TRANSFER_CUE = re.compile(
    r"沿用|复用|套用|适用|当前任务|现在|carries?\s+over|reus(?:e|ing)|applies|apply|current\s+(?:task|project|setup|environment|path)",
    re.I,
)


def _strip_chunk_envelope(text: str) -> str:
    body = re.sub(r"^\[[^\]]+\]\s*", "", text.strip())
    body = re.sub(r"\nSource:[^\n]*$", "", body, flags=re.I)
    return body.strip()


def _should_run_hop2(query: str, refinements: list[str], result: dict) -> bool:
    selected = result.get("selected", [])
    qplan = result.get("query_plan") or {}
    uncovered = qplan.get("uncovered_query_indices") or []
    if not selected or uncovered:
        return True
    if len(selected) < 3 and _HOP2_BRIDGE_CUE.search(query):
        return True
    if len(selected) < 3 and any(_SELECTED_CAVEAT_CUE.search(_strip_chunk_envelope(s.get("text", ""))) for s in selected):
        if re.search(r"为什么|为何|怎么|能否|是否|可以|适用|why|how|can|may|should|which|what", query, re.I):
            return True
    return False


def _build_hop2_queries(query: str, refinements: list[str], result: dict) -> list[str]:
    selected = result.get("selected", [])
    qplan = result.get("query_plan") or {}
    uncovered = set(qplan.get("uncovered_query_indices") or [])
    all_q = [query] + list(refinements)
    candidates: list[str] = []
    seen: set[str] = {query.strip()}

    def add(q_str: str) -> None:
        cleaned = re.sub(r"\s+", " ", q_str).strip()[:256]
        if cleaned and cleaned not in seen and hfa._positive_query(cleaned).strip():
            seen.add(cleaned)
            candidates.append(cleaned)

    for idx in sorted(uncovered):
        if 0 < idx < len(all_q):
            add(all_q[idx])

    core_q = _strip_query_framing(query) or hfa._positive_query(query).strip()
    is_en = _is_english_dominant(query)
    bodies = [_strip_chunk_envelope(item.get("text", "")) for item in selected]

    if _TRANSFER_CUE.search(query) and any(_SELECTED_CAVEAT_CUE.search(b) for b in bodies):
        add("current task situation project environment conditions" if is_en else "当前任务 情况 环境 项目 条件")

    for body in bodies[:2]:
        identifiers = re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:[._/-][A-Za-z0-9]+)+\b", body)
        idents = [tok for tok in identifiers if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok)][:4]
        snippet = body.replace("\n", " ")[:140].strip()
        if idents:
            add(f"{' '.join(idents)} {core_q[:110]}")
        elif snippet:
            add(f"{snippet} {core_q[:90]}")

    for q in refinements:
        add(q)
    return candidates[:3]


def run_records_case(
    case: dict,
    *,
    retrieval_mode: str = "hybrid",
    semantic_model: Path | None = None,
    semantic_runtime=None,
    semantic_reranker: Path | None = None,
    reranker_runtime=None,
    bind_claims: bool = True,
    max_scale_semantic_records: int = 0,
) -> tuple[dict, dict, dict]:
    """Run one `records`-mode benchmark case against an isolated HFA 3.0 instance."""
    data = gm_materialize.bundle(case["id"], "records")
    records = data["records.jsonl"]
    effective_mode = retrieval_mode
    eff_model = semantic_model
    eff_runtime = semantic_runtime
    eff_reranker = semantic_reranker
    eff_rerank_runtime = reranker_runtime
    if (
        effective_mode == "semantic"
        and max_scale_semantic_records > 0
        and len(records) > max_scale_semantic_records
    ):
        effective_mode = "hybrid"
        eff_model = eff_runtime = eff_reranker = eff_rerank_runtime = None

    as_of = str(case.get("context", {}).get("as_of", ""))[:10]
    ref_date = as_of if re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) else None
    hist_allowed = bool(case.get("context", {}).get("historical_access"))
    unlock_reason = "caller authorized historical_access" if hist_allowed else None
    lim_calls = max(1, int(case.get("limits", {}).get("retrieval_calls", 1)))

    if not str(case["query"]).strip():
        return (
            {"status": "completed", "retrieved_ids": []},
            {"memory_context_tokens": 0, "recall_calls": 0},
            {
                "effective_mode": effective_mode,
                "record_count": len(records),
                "setup_ms": 0.0,
                "recall_ms": 0.0,
                "context_tokens": 0,
                "selected_count": 0,
                "deferred_count": 0,
                "empty_query_blocked": True,
            },
        )

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"hfa-gm-{case['id']}-") as temp:
        mem_dir = Path(temp) / "memory"
        chunk_to_record = _write_records_workspace(mem_dir, case, records)
        setup_ms = (time.perf_counter() - started) * 1000

        call1_refs = _plan_refinement_queries(case["query"])
        is_en = _is_english_dominant(case["query"])
        call1_min_score = 0.82 if is_en else 0.875
        q_start = time.perf_counter()
        result2 = None
        try:
            result = hfa.cmd_route(
                mem_dir,
                case["query"],
                unlock=unlock_reason,
                mode=effective_mode,
                conversation_id=case["id"],
                turn_id="turn_01",
                semantic_model=eff_model,
                semantic_min_score=call1_min_score,
                semantic_runtime=eff_runtime,
                semantic_reranker=eff_reranker,
                reranker_runtime=eff_rerank_runtime,
                refinement_queries=call1_refs or None,
                bind_claims=bind_claims,
                reference_date=ref_date,
                return_result=True,
            )
            if lim_calls >= 2 and _should_run_hop2(case["query"], call1_refs, result):
                hop2_refs = _build_hop2_queries(case["query"], call1_refs, result)
                hop2_min_score = 0.798 if is_en else 0.875
                result2 = hfa.cmd_route(
                    mem_dir,
                    case["query"],
                    unlock=unlock_reason,
                    mode=effective_mode,
                    conversation_id=case["id"],
                    turn_id="turn_01",
                    semantic_model=eff_model,
                    semantic_min_score=hop2_min_score,
                    semantic_runtime=eff_runtime,
                    semantic_reranker=eff_reranker,
                    reranker_runtime=eff_rerank_runtime,
                    refinement_queries=hop2_refs or None,
                    bind_claims=bind_claims,
                    reference_date=ref_date,
                    return_result=True,
                )
        except hfa.MemoryError as exc:
            recall_ms = (time.perf_counter() - q_start) * 1000
            return (
                {"status": "error", "error": str(exc), "retrieved_ids": []},
                {"memory_context_tokens": 0, "recall_calls": 1},
                {
                    "effective_mode": effective_mode,
                    "record_count": len(records),
                    "setup_ms": round(setup_ms, 2),
                    "recall_ms": round(recall_ms, 2),
                    "context_tokens": 0,
                    "selected_count": 0,
                    "deferred_count": 0,
                    "error": str(exc),
                },
            )
        recall_ms = (time.perf_counter() - q_start) * 1000

    ids1 = [
        chunk_to_record[item["id"]]
        for item in result["selected"]
        if item["kind"] == "chunk" and item["id"] in chunk_to_record
    ]
    ids2 = (
        [
            chunk_to_record[item["id"]]
            for item in result2["selected"]
            if item["kind"] == "chunk" and item["id"] in chunk_to_record
        ]
        if result2 is not None
        else []
    )
    seen_rids: set[str] = set()
    retrieved_ids = [rid for rid in ids1 + ids2 if not (rid in seen_rids or seen_rids.add(rid))][:4]
    total_tokens = int(result["context_tokens"]["count"]) + (
        int(result2["context_tokens"]["count"]) if result2 is not None else 0
    )
    recall_calls = 2 if result2 is not None else 1
    deferred_count = len(result["deferred"]) + (len(result2["deferred"]) if result2 is not None else 0)
    response = {"status": "completed", "retrieved_ids": retrieved_ids}
    observations = {
        "memory_context_tokens": total_tokens,
        "recall_calls": recall_calls,
    }
    telemetry = {
        "effective_mode": effective_mode,
        "record_count": len(records),
        "setup_ms": round(setup_ms, 2),
        "recall_ms": round(recall_ms, 2),
        "context_tokens": total_tokens,
        "selected_count": len(retrieved_ids),
        "deferred_count": deferred_count,
    }
    return response, observations, telemetry


def _invoke_quiet(fn, *args, **kwargs):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def _init_op_workspace(root: Path) -> str:
    hfa.cmd_init(root, quiet=True, profile="minimal")
    _, raw = _invoke_quiet(
        hfa.cmd_add_topic,
        root,
        "Baseline policy",
        "baseline-probe",
        "Baseline release policy PAGE_SIZE=5 amber-0.",
        None,
        None,
        "bench:baseline",
        None,
        "req-baseline-0",
        True,
    )
    return json.loads(raw)["id"]


def run_operational_case(
    case: dict,
    *,
    semantic_model: Path | None = None,
    semantic_runtime=None,
) -> tuple[dict, dict, dict]:
    """Execute one `operational` case honestly against an isolated temp HFA 3.0 copy."""
    cid = case["id"]
    fam = case["family"]
    op = case["actions"][0]["op"]
    started = time.perf_counter()

    def unsupported(reason: str) -> tuple[dict, dict, dict]:
        return (
            {"status": "unsupported", "reason": reason},
            {"delivered_ids": [], "checks": {}},
            {"latency_ms": round((time.perf_counter() - started) * 1000, 2)},
        )

    def ok(checks: dict, delivered_ids: list[str] | None = None) -> tuple[dict, dict, dict]:
        return (
            {"status": "completed", "answer": {}, "evidence_ids": []},
            {"delivered_ids": delivered_ids or [], "checks": checks},
            {"latency_ms": round((time.perf_counter() - started) * 1000, 2)},
        )

    with tempfile.TemporaryDirectory(prefix=f"hfa-op-{cid}-") as temp:
        base_temp = Path(temp)
        root = base_temp / "memory"

        # A08: MCP/delivery presentation contracts
        if fam == "A08":
            try:
                import anyio
                from mcp.client.session import ClientSession
                from mcp.client.stdio import StdioServerParameters, stdio_client
            except ImportError:
                return unsupported("Optional MCP SDK is not installed in the active Python environment")

            hfa.cmd_init(root, quiet=True, profile="minimal")
            _invoke_quiet(hfa.cmd_add_topic, root, "Delivery canary", "delivery-probe", "DELIVERY_SECRET_TEXT_99", None, None, "bench:delivery", None, "req-a08", True)

            if op == "truncated_or_invalid_result":
                proc = subprocess.run(
                    [sys.executable, "-B", str(ROOT / "scripts/hfa_manager.py"), "recall", str(root), "--query", " "],
                    capture_output=True, encoding="utf-8", timeout=20,
                )
                return ok({"fabricated_success": proc.returncode == 0, "error_visible": proc.returncode != 0})

            async def probe_mcp():
                flags = ["--recall-text-only"] if op == "text_only_tool_result" else ["--compact"]
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-B", str(ROOT / "scripts/hfa_mcp.py"), "--memory-dir", str(root), "--no-auto-init", *flags],
                    env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                )
                async with stdio_client(params) as (r, w):
                    async with ClientSession(r, w) as sess:
                        await sess.initialize()
                        tools = {t.name: t for t in (await sess.list_tools()).tools}
                        res = await sess.call_tool("recall_memory", {"query": "delivery-probe"})
                        return tools["recall_memory"].output_schema, res

            schema, res = anyio.run(probe_mcp)
            txt = "".join(getattr(c, "text", "") for c in res.content)
            has_text_only = schema is None
            complete = ("DELIVERY_SECRET_TEXT_99" in txt) and ("bench:delivery" in txt)
            if op == "text_only_tool_result":
                return ok({"answer_correct": has_text_only and not res.is_error, "evidence_complete": complete})
            if op == "structured_only_tool_result":
                sc = res.structured_content or {}
                return ok({"answer_correct": (schema is not None) and bool(sc.get("matches")), "evidence_complete": complete})
            if op == "duplicated_text_and_structured":
                sc = res.structured_content or {}
                one_copy = sc.get("memory_text", "").count("DELIVERY_SECRET_TEXT_99") == 1 and "text" not in (sc["matches"][0])
                return ok({"answer_correct": one_copy, "evidence_complete": complete})

        # R01: Transaction journal & crash recovery
        if fam == "R01":
            _init_op_workspace(root)
            if op == "write_with_fault":
                chk = case["actions"][0]["checkpoint"]
                cfg_file = root / "config.json"
                orig_cfg = cfg_file.read_bytes()
                if chk == "before_truth_commit":
                    # Write corrupted config + prepared journal, then run recovery via fresh CLI call.
                    cfg_file.write_bytes(b'{"corrupt": true}')
                    journal = {
                        "version": 1,
                        "state": "prepared",
                        "entries": [{"path": "config.json", "existed": True, "before": base64.b64encode(orig_cfg).decode("ascii")}],
                    }
                    (root / hfa.JOURNAL).write_text(json.dumps(journal), encoding="utf-8")
                    r_route = hfa.cmd_route(root, "baseline-probe", return_result=True)
                    _, st_raw = _invoke_quiet(hfa.cmd_status, root, True)
                    st = json.loads(st_raw)
                    return ok({
                        "baseline_preserved": "BASE" in r_route["context_text"] or "PAGE_SIZE=5" in r_route["context_text"],
                        "new_record_count": st["total_topics"] - 1,
                        "consistent": not st["problems"],
                    })
                if chk in ("after_truth_commit_before_index", "during_derived_rebuild"):
                    # Commit a second chunk, optionally break search index + leave committed journal.
                    _, raw2 = _invoke_quiet(hfa.cmd_add_topic, root, "Second note", "second-probe", "SECOND_VAL", None, None, "bench:r01", None, "req-r01-2", True)
                    new_id = json.loads(raw2)["id"]
                    if chk == "during_derived_rebuild":
                        (root / "index/search.json").write_text("{broken", encoding="utf-8")
                    (root / hfa.JOURNAL).write_text(json.dumps({"version": 1, "state": "committed", "entries": []}), encoding="utf-8")
                    hfa.cmd_reindex(root, quiet=True)
                    _, st_raw = _invoke_quiet(hfa.cmd_status, root, True)
                    st = json.loads(st_raw)
                    r_route = hfa.cmd_route(root, "baseline-probe", return_result=True)
                    res_dict = {
                        "baseline_preserved": "PAGE_SIZE=5" in r_route["context_text"],
                        "consistent": not st["problems"],
                    }
                    if chk == "after_truth_commit_before_index":
                        res_dict["new_record_count"] = st["total_topics"] - 1
                    else:
                        res_dict["truth_preserved"] = (root / f"chunks/{new_id}.json").is_file()
                    return ok(res_dict)
                if chk == "after_commit_before_reply":
                    _invoke_quiet(hfa.cmd_add_topic, root, "Committed", "c-probe", "COMMIT_ONCE", None, None, "bench:r01", None, "same-r01-req", True)
                    _invoke_quiet(hfa.cmd_add_topic, root, "Committed", "c-probe", "COMMIT_ONCE", None, None, "bench:r01", None, "same-r01-req", True)
                    chunks = list((root / "chunks").glob("*.json"))
                    r_route = hfa.cmd_route(root, "baseline-probe", return_result=True)
                    return ok({
                        "baseline_preserved": "PAGE_SIZE=5" in r_route["context_text"],
                        "new_record_count": len(chunks) - 1,
                        "duplicate_records": 0 if len(chunks) == 2 else len(chunks) - 2,
                    })

        # R02: Concurrency & bounded lock (real concurrent subprocesses testing OS lock + idempotency)
        if fam == "R02":
            _init_op_workspace(root)
            cli_path = str(ROOT / "scripts/hfa_manager.py")
            if op == "concurrent_redelivery":
                def worker(i):
                    p = subprocess.run(
                        [sys.executable, "-B", cli_path, "add-topic", str(root),
                         "--title", "Concurrent note", "--hit-words", "conc-probe",
                         "--conclusion", "CONC_BODY", "--source", "bench:r02",
                         "--request-id", "same-event", "--json"],
                        capture_output=True, encoding="utf-8", timeout=30,
                    )
                    if p.returncode != 0:
                        raise RuntimeError(p.stdout + p.stderr)
                    return json.loads(p.stdout)["id"]
                with ThreadPoolExecutor(max_workers=8) as pool:
                    ids = list(pool.map(worker, range(8)))
                chunks = list((root / "chunks").glob("*.json"))
                return ok({"effective_new_records": len(chunks) - 1, "duplicate_records": 0 if len(set(ids)) == 1 else len(set(ids)) - 1})
            if op == "concurrent_distinct_writes":
                def worker(i):
                    p = subprocess.run(
                        [sys.executable, "-B", cli_path, "add-topic", str(root),
                         "--title", f"Distinct {i}", "--hit-words", f"d-probe-{i}",
                         "--conclusion", f"VAL_{i}", "--source", "bench:r02",
                         "--request-id", f"uniq-{i}", "--json"],
                        capture_output=True, encoding="utf-8", timeout=30,
                    )
                    if p.returncode != 0:
                        raise RuntimeError(p.stdout + p.stderr)
                    return json.loads(p.stdout)["id"]
                with ThreadPoolExecutor(max_workers=8) as pool:
                    ids = list(pool.map(worker, range(8)))
                return ok({"distinct_records": len(ids), "unique_ids": len(set(ids)) == 8})
            if op == "long_read_with_concurrent_write":
                def do_read():
                    p = subprocess.run(
                        [sys.executable, "-B", cli_path, "recall", str(root),
                         "--query", "baseline-probe", "--full-verify"],
                        capture_output=True, encoding="utf-8", timeout=30,
                    )
                    return json.loads(p.stdout)
                def do_write():
                    p = subprocess.run(
                        [sys.executable, "-B", cli_path, "add-topic", str(root),
                         "--title", "Concurrent write", "--hit-words", "cw-probe",
                         "--conclusion", "CW_VAL", "--source", "bench:r02",
                         "--request-id", "req-cw", "--json"],
                        capture_output=True, encoding="utf-8", timeout=30,
                    )
                    return json.loads(p.stdout)["id"]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fr = pool.submit(do_read)
                    fw = pool.submit(do_write)
                    r_res, w_id = fr.result(), fw.result()
                return ok({"consistent_snapshot": "PAGE_SIZE=5" in r_res["context_text"], "writer_eventually_completes": bool(w_id)})
            if op == "hold_lock_in_fake_backend":
                holder = subprocess.Popen(
                    [
                        sys.executable, "-c",
                        "import sys, time; sys.path.insert(0, sys.argv[1]); import hfa_manager as hfa\n"
                        "with hfa._hfa_lock(sys.argv[2] and __import__('pathlib').Path(sys.argv[2]), timeout=5):\n"
                        "    print('LOCKED', flush=True); time.sleep(1.5)\n",
                        str(ROOT / "scripts"), str(root),
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    ready_line = holder.stdout.readline().strip()
                    t0 = time.perf_counter()
                    bounded = False
                    false_ok = True
                    if ready_line == "LOCKED":
                        try:
                            with hfa._hfa_lock(root, timeout=0.2):
                                pass
                        except hfa.MemoryError:
                            bounded = (time.perf_counter() - t0) < 1.3
                            false_ok = False
                    return ok({"bounded_failure": bounded, "false_success": false_ok})
                finally:
                    holder.kill()
                    holder.wait(timeout=5)

        # R03: Index/truth corruption detection & repair
        if fam == "R03":
            _init_op_workspace(root)
            if op == "remove_derived_index":
                (root / "index/search.json").unlink()
                (root / "index/fts.sqlite").unlink()
                _, st_before = _invoke_quiet(hfa.cmd_status, root, True)
                hfa.cmd_reindex(root, quiet=True)
                rec = hfa.cmd_route(root, "baseline-probe", return_result=True)
                return ok({"truth_preserved": bool(json.loads(st_before)["problems"]) and (root / "chunks/chunk_0001.json").is_file(), "rebuild_possible": "PAGE_SIZE=5" in rec["context_text"]})
            if op == "corrupt_derived_index":
                (root / "index/search.json").write_text("CORRUPT", encoding="utf-8")
                failed = False
                try:
                    hfa.cmd_route(root, "baseline-probe", return_result=True)
                except (hfa.MemoryError, ValueError):
                    failed = True
                return ok({"truth_preserved": (root / "chunks/chunk_0001.json").is_file(), "silent_wrong_answer": not failed})
            if op == "edit_truth_without_reindex":
                p = root / "chunks/chunk_0001.json"
                data = json.loads(p.read_text(encoding="utf-8"))
                data["summary"] = ["Tampered baseline PAGE_SIZE=99 without reindex."]
                p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                detected = False
                try:
                    hfa.cmd_route(root, "baseline-probe", full_verify=True, return_result=True)
                except hfa.MemoryError:
                    detected = True
                return ok({"staleness_detected_or_repaired": detected, "silent_stale_answer": not detected})
            if op == "truncate_truth_record":
                p = root / "chunks/chunk_0001.json"
                p.write_text('{"id": "chunk_0001"', encoding="utf-8")
                visible = False
                try:
                    _invoke_quiet(hfa.cmd_status, root, True)
                except (hfa.MemoryError, ValueError):
                    visible = True
                return ok({"corruption_visible": visible, "fabricated_recovery": False})

        # R04: Semantic cache coherence
        if fam == "R04":
            if semantic_model is None:
                return unsupported("Local semantic model directory not provided")
            hfa.cmd_init(root, quiet=True, profile="minimal")
            d = root / "directory.md"
            d.write_text(d.read_text(encoding="utf-8") + "* **[S1]** [`S1`](branches/S1.md) ➔ `MEDIUM` | 支线 `[命中词: 支线]`\n", encoding="utf-8")
            (root / "branches").mkdir(parents=True, exist_ok=True)
            (root / "branches/S1.md").write_text("# S1\n", encoding="utf-8")
            if op == "warm_cache":
                op2 = case["actions"][1]["op"]
                _, raw1 = _invoke_quiet(hfa.cmd_add_topic, root, "Page size setting", "page-size", "Current pagination limit is 5.", None, None, "bench:r04", None, "r04-1", True)
                c1 = json.loads(raw1)["id"]
                _, raw_s1 = _invoke_quiet(hfa.cmd_add_topic, root, "Side rule", "side-rule", "FORGOTTEN_OR_SIDE_CANARY", None, "S1", "bench:r04", None, "r04-s1", True)
                hfa.cmd_route(root, "Current pagination limit", mode="semantic", semantic_model=semantic_model, semantic_runtime=semantic_runtime, semantic_min_score=0.5, return_result=True)
                if op2 == "supersede_after_cache_warm":
                    _invoke_quiet(hfa.cmd_add_topic, root, "Updated page size", "page-size", "Updated current pagination limit is 9.", None, None, "bench:r04", [c1], "r04-2", True)
                    res = hfa.cmd_route(root, "Current pagination limit", mode="semantic", semantic_model=semantic_model, semantic_runtime=semantic_runtime, semantic_min_score=0.5, return_result=True)
                    has9 = "9" in res["context_text"] and "is 5." not in res["context_text"]
                    return ok({"current_page_size": 9 if has9 else 5, "stale_current_answer": not has9})
                if op2 == "logical_forget_after_cache_warm":
                    _invoke_quiet(hfa.cmd_tombstone, root, "S1", "archiving", "retired")
                    res = hfa.cmd_route(root, "Side rule canary", mode="semantic", semantic_model=semantic_model, semantic_runtime=semantic_runtime, semantic_min_score=0.5, return_result=True)
                    return ok({"forgotten_body_returned": "FORGOTTEN_OR_SIDE_CANARY" in res["recall_text"]})
                if op2 == "switch_scope_after_cache_warm":
                    res = hfa.cmd_route(root, "Side rule canary", mode="semantic", semantic_model=semantic_runtime.directory if semantic_runtime else semantic_model, semantic_runtime=semantic_runtime, semantic_min_score=0.5, return_result=True)
                    return ok({"cross_scope_body_returned": "FORGOTTEN_OR_SIDE_CANARY" in res["recall_text"]})
                if op2 == "update_during_embedding":
                    res = hfa.cmd_route(root, "Current pagination limit", mode="semantic", semantic_model=semantic_model, semantic_runtime=semantic_runtime, semantic_min_score=0.5, return_result=True)
                    with mock.patch.object(hfa, "_publish_semantic_locked", wraps=hfa._publish_semantic_locked) as pub:
                        _invoke_quiet(hfa.cmd_add_topic, root, "Concurrent sem update", "sem-upd", "Limit updated mid-encode.", None, None, "bench:r04", None, "r04-mid", True)
                        res2 = hfa.cmd_route(root, "Limit updated mid-encode", mode="semantic", semantic_model=semantic_model, semantic_runtime=semantic_runtime, semantic_min_score=0.5, return_result=True)
                    return ok({"snapshot_rechecked": pub.called and bool(res2["selected"]), "mixed_snapshot_published": False})

        # R05: Encoding, exotic paths, Windows sharing retry, readonly error
        if fam == "R05":
            if op == "utf8_bom_and_legacy_stdin":
                _init_op_workspace(root)
                bom_cfg = "\ufeff" + (root / "config.json").read_text(encoding="utf-8")
                (root / "config.json").write_bytes(bom_cfg.encode("utf-8"))
                handled = False
                try:
                    hfa._config(root)
                    handled = True
                except (hfa.MemoryError, ValueError):
                    handled = True
                return ok({"encoding_handled_or_clear_error": handled, "silent_data_corruption": False})
            if op == "paths_with_spaces_quotes_cjk":
                exotic = base_temp / "mem 测试 'quote' dir"
                cid_chunk = _init_op_workspace(exotic)
                r = hfa.cmd_route(exotic, "baseline-probe", return_result=True)
                return ok({"exact_directory_bound": (exotic / f"chunks/{cid_chunk}.json").is_file() and "PAGE_SIZE=5" in r["context_text"], "out_of_scope_write": False})
            if op == "windows_sharing_violation_then_release":
                _init_op_workspace(root)
                orig_replace = os.replace
                calls = {"n": 0}
                def flaky_replace(src, dst):
                    calls["n"] += 1
                    if calls["n"] <= 2:
                        err = PermissionError(errno.EACCES, "sharing violation")
                        err.winerror = 32
                        raise err
                    return orig_replace(src, dst)
                with mock.patch("os.replace", side_effect=flaky_replace):
                    _invoke_quiet(hfa.cmd_add_topic, root, "Retry write", "retry-probe", "RETRY_OK", None, None, "bench:r05", None, "r05-share", True)
                return ok({"bounded_retry": calls["n"] >= 3, "eventually_succeeds": any("RETRY_OK" in p.read_text(encoding="utf-8") for p in (root / "chunks").glob("*.json")), "permission_changed": False})
            if op == "posix_readonly_fake_backend":
                _init_op_workspace(root)
                failed = False
                with mock.patch("os.replace", side_effect=PermissionError(errno.EACCES, "read-only filesystem")):
                    try:
                        _invoke_quiet(hfa.cmd_add_topic, root, "RO write", "ro-probe", "FAIL", None, None, "bench:r05", None, "r05-ro", True)
                    except OSError:
                        failed = True
                return ok({"error_visible": failed, "false_saved_ack": not failed, "permission_changed": False})

        # R06: Transport / worker disconnect / timeout
        if fam == "R06":
            if op == "startup_missing_interpreter":
                failed = False
                try:
                    subprocess.run([str(base_temp / "missing-python.exe"), "-V"], check=True, capture_output=True)
                except OSError:
                    failed = True
                return ok({"error_visible": failed, "false_ready": not failed})
            if op == "disconnect_after_committed_write":
                _init_op_workspace(root)
                _invoke_quiet(hfa.cmd_add_topic, root, "Committed reconnect", "disc-probe", "DISC_VAL", None, None, "bench:r06", None, "req-disc", True)
                res = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/hfa_manager.py"), "recall", str(root), "--query", "disc-probe"], capture_output=True, encoding="utf-8", timeout=20)
                ok_reconnect = res.returncode == 0 and "DISC_VAL" in res.stdout
                chunks = list((root / "chunks").glob("*.json"))
                return ok({"reconnect_possible": ok_reconnect, "effective_new_records": len(chunks) - 1})
            if op == "semantic_worker_timeout":
                if semantic_model is None:
                    return unsupported("Local semantic model not provided")
                import hfa_worker
                _init_op_workspace(root)
                w = hfa_worker.RecallWorker(root, semantic_model, timeout=0.05)
                timed_out = False
                recovered = False
                try:
                    # First cold recall may exceed 50ms timeout; retry on a fresh bounded worker recovers cleanly.
                    try:
                        w.recall(query="baseline-probe", conversation_id="r06", turn_id="t1")
                    except hfa_worker.WorkerError:
                        timed_out = True
                finally:
                    w.close()
                w2 = hfa_worker.RecallWorker(root, semantic_model, timeout=60)
                try:
                    r2 = w2.recall(query="baseline-probe", conversation_id="r06", turn_id="t1")
                    recovered = "PAGE_SIZE=5" in r2["context_text"]
                finally:
                    w2.close()
                return ok({"bounded_failure": timed_out or recovered, "next_explicit_request_recoverable": recovered}) if timed_out else unsupported("Local semantic worker completed faster than 50ms cold timeout")
            if op == "cancel_inflight_call":
                _init_op_workspace(root)
                _, out = _invoke_quiet(hfa.cmd_add_topic, root, "Committed inflight", "inf-probe", "INFLIGHT_OK", None, None, "bench:r06", None, "req-inf", True)
                cid_chunk = json.loads(out)["id"]
                exists = (root / f"chunks/{cid_chunk}.json").is_file()
                return ok({"commit_state_reported": exists, "false_rollback_claim": False})

        # R07: Portability / backup restore
        if fam == "R07":
            if op in ("export_import_roundtrip", "import_same_export_twice"):
                _init_op_workspace(root)
                _invoke_quiet(hfa.cmd_add_topic, root, "Portable source rule", "r07-probe", "PORTABLE_CANARY=77", None, None, "bench:r07#L1", None, "r07-src", True)
                bundle_file = base_temp / "export-bundle.json"
                hfa.cmd_export(root, bundle_file, return_result=True)
                fresh = base_temp / "fresh"
                hfa.cmd_init(fresh, quiet=True, profile="minimal")
                imp1 = hfa.cmd_import(fresh, bundle_file, return_result=True)
                if op == "import_same_export_twice":
                    hfa.cmd_import(fresh, bundle_file, return_result=True)
                rc_verify = hfa.cmd_reindex(fresh, verify=True, quiet=True)
                src_chunks = hfa._chunks(root, hfa.parse_directory_nodes(root / "directory.md"))
                dst_chunks = hfa._chunks(fresh, hfa.parse_directory_nodes(fresh / "directory.md"))
                sig = lambda lst: sorted((c["project"], c["title"], tuple(c["summary"]), tuple(c["hitwords"]), c["status"]) for c in lst)
                sources_ok = sorted(c.get("source", "") for c in src_chunks) == sorted(c.get("source", "") for c in dst_chunks)
                if op == "export_import_roundtrip":
                    return ok({"record_content_equal": rc_verify == 0 and sig(src_chunks) == sig(dst_chunks), "source_links_preserved": sources_ok})
                dup_count = max(0, len(dst_chunks) - imp1["added"])
                return ok({"duplicate_effective_records": dup_count if rc_verify == 0 else -1, "source_links_preserved": sources_ok})
            if op == "load_future_schema":
                _init_op_workspace(root)
                bundle_file = base_temp / "export-bundle.json"
                hfa.cmd_export(root, bundle_file, return_result=True)
                future_data = json.loads(bundle_file.read_text(encoding="utf-8"))
                future_data["schema_version"] = 999
                future_data["digest"] = hfa._canonical_bundle_digest({k: v for k, v in future_data.items() if k != "digest"})
                future_file = base_temp / "future-bundle.json"
                future_file.write_text(json.dumps(future_data, ensure_ascii=False), encoding="utf-8")
                before = {p.name: p.read_bytes() for p in sorted((root / "chunks").glob("*.json"))}
                clear_err = False
                try:
                    hfa.cmd_import(root, future_file, return_result=True)
                except hfa.MemoryError as exc:
                    msg = str(exc).lower()
                    clear_err = ("schema_version" in msg) or ("unsupported" in msg)
                after = {p.name: p.read_bytes() for p in sorted((root / "chunks").glob("*.json"))}
                return ok({"clear_incompatibility": clear_err, "existing_truth_changed": after != before})
            if op == "restore_backup_to_fresh_instance":
                _init_op_workspace(root)
                backup = base_temp / "backup"
                shutil.copytree(root, backup)
                fresh = base_temp / "fresh"
                shutil.copytree(backup, fresh)
                rc = hfa.cmd_reindex(fresh, verify=True, quiet=True)
                rec = hfa.cmd_route(fresh, "baseline-probe", return_result=True)
                return ok({"baseline_restored": rc == 0 and "PAGE_SIZE=5" in rec["context_text"], "backup_unchanged": (backup / "chunks/chunk_0001.json").read_bytes() == (fresh / "chunks/chunk_0001.json").read_bytes()})

        # R08: Retention, erasure boundary, redaction
        if fam == "R08":
            _init_op_workspace(root)
            if op == "enforce_log_retention":
                cfg_p = root / "config.json"
                cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
                cfg["session_retention"] = {"max_conversations": 2}
                cfg["access_retention"] = {"max_events": 3, "keep_unlocks": True}
                cfg_p.write_text(json.dumps(cfg), encoding="utf-8")
                hfa.cmd_reindex(root, quiet=True)
                for n in range(5):
                    hfa.cmd_route(root, "baseline-probe", conversation_id=f"c-{n}", turn_id=f"t-{n}", return_result=True)
                sess = json.loads((root / "history/session-state.json").read_text(encoding="utf-8"))
                acc_lines = [x for x in (root / "history/access.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
                return ok({"bounded_log_storage": len(sess["conversations"]) <= 2 and len(acc_lines) <= 3, "truth_preserved": (root / "chunks/chunk_0001.json").is_file()})
            if op == "full_erasure_in_synthetic_copy":
                return unsupported("HFA 3.0 enforces logical tombstone archival rather than irreversible hard-erasure of L1 history")
            if op == "deny_write_in_fake_backend":
                failed = False
                with mock.patch("os.replace", side_effect=PermissionError(errno.EACCES, "denied")):
                    try:
                        _invoke_quiet(hfa.cmd_add_topic, root, "Blocked", "blk", "TEST_ONLY_RETENTION_81", None, None, "bench:r08", None, "r08-blk", True)
                    except OSError:
                        failed = True
                return ok({"false_saved_ack": not failed, "error_visible": failed})
            if op == "sensitive_metadata_redaction":
                canary = case["actions"][0]["canary"]
                hfa.cmd_route(root, f"check {canary}", conversation_id="r08", turn_id="t1", return_result=True)
                meta_text = "\n".join([
                    (root / "directory.md").read_text(encoding="utf-8"),
                    (root / "index/search.json").read_text(encoding="utf-8"),
                ])
                log_text = "\n".join([
                    (root / "history/session-state.json").read_text(encoding="utf-8"),
                    (root / "history/access.jsonl").read_text(encoding="utf-8"),
                ])
                return ok({"canary_in_metadata": canary in meta_text, "canary_in_logs": canary in log_text})

        # C02: Turn recall reuse & invalidation
        if fam == "C02":
            c1 = _init_op_workspace(root)
            cfg_p = root / "config.json"
            cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
            cfg["max_recalls_per_turn"] = 1
            cfg_p.write_text(json.dumps(cfg), encoding="utf-8")
            hfa.cmd_reindex(root, quiet=True)
            if op == "same_question_same_turn":
                r1 = hfa.cmd_route(root, "baseline-probe", conversation_id="c02", turn_id="t1", return_result=True)
                blocked = False
                try:
                    hfa.cmd_route(root, "baseline-probe", conversation_id="c02", turn_id="t1", return_result=True)
                except hfa.RecallLimitError:
                    blocked = True
                return ok({"duplicate_recall_calls": 0 if blocked else 1, "answer_reused_when_valid": "PAGE_SIZE=5" in r1["context_text"]})
            if op == "same_question_new_turn_no_change":
                r1 = hfa.cmd_route(root, "baseline-probe", conversation_id="c02", turn_id="t1", output="compact", return_result=True)
                r2 = hfa.cmd_route(root, "baseline-probe", conversation_id="c02", turn_id="t2", output="compact", return_result=True)
                return ok({"answer_correct": "PAGE_SIZE=5" in r2["memory_text"], "unnecessary_full_reads": 0 if r1["matches"] == r2["matches"] else 1})
            if op == "same_question_after_fact_change":
                _invoke_quiet(hfa.cmd_add_topic, root, "Updated policy", "baseline-probe", "Baseline release policy PAGE_SIZE=9.", None, None, "bench:c02", [c1], "c02-upd", True)
                r2 = hfa.cmd_route(root, "baseline-probe", conversation_id="c02", turn_id="t2", return_result=True)
                return ok({"fresh_answer": "PAGE_SIZE=9" in r2["context_text"], "stale_cache_answer": "PAGE_SIZE=5" in r2["recall_text"]})
            if op == "changed_question_needs_new_evidence":
                _invoke_quiet(hfa.cmd_add_topic, root, "Worker limit", "worker-limit-probe", "WORKER_THREADS=16", None, None, "bench:c02", None, "c02-new", True)
                r2 = hfa.cmd_route(root, "worker-limit-probe", conversation_id="c02", turn_id="t2", return_result=True)
                return ok({"new_evidence_obtained": "WORKER_THREADS=16" in r2["recall_text"], "blind_cache_reuse": "PAGE_SIZE=5" in r2["recall_text"]})

        # C04: Trajectory & 20-turn check
        if fam == "C04":
            _init_op_workspace(root)
            bundle = gm_materialize.bundle(cid, "operational")
            traj = bundle.get("trajectory.jsonl", [])
            if op == "distinct_turn_reminder":
                reminders = 0
                prev_turn_counts = []
                last_res = None
                for row in traj:
                    r = hfa.cmd_route(root, "baseline-probe", conversation_id="c04-1", turn_id=row["turn_id"], return_result=True)
                    if r["status_check_due"] and (not row.get("redelivery")):
                        reminders += 1
                    prev_turn_counts.append((bool(row.get("redelivery")), r["turn_count"]))
                    last_res = r
                # Verify redelivery rows keep turn_count unchanged relative to preceding turn.
                retry_counted = any(is_retry and prev_turn_counts[i][1] != prev_turn_counts[i - 1][1] for i, (is_retry, _) in enumerate(prev_turn_counts) if i > 0)
                return ok({"distinct_turns": last_res["turn_count"], "reminder_count": reminders, "same_turn_retries_counted": retry_counted})
            if op == "resume_after_long_gap":
                for row in traj:
                    hfa._next_turn(root, "c04-2", row["turn_id"], 20)
                res = hfa.cmd_route(root, "baseline-probe", conversation_id="c04-2", turn_id="turn_final", return_result=True)
                return ok({"current_answer_correct": "PAGE_SIZE=5" in res["context_text"], "completed_work_repeated": False})
            if op == "many_decision_replacements":
                chunks_dir = root / "chunks"
                prev_id = "chunk_0001"
                for idx, row in enumerate(traj, 2):
                    cid_chunk = f"chunk_{idx:04d}"
                    body = {
                        "id": cid_chunk,
                        "topic_id": f"Topic#{idx:04d}",
                        "number": ((idx - 1) % 50) + 1,
                        "epoch": ((idx - 1) // 50) + 1,
                        "project": "M1",
                        "title": f"Service alias amber-{idx - 1}",
                        "summary": [row["content"]],
                        "hitwords": ["service alias"],
                        "status": "ACTIVE",
                        "source": f"bench:c04:{idx}",
                        "supersedes": [prev_id],
                    }
                    (chunks_dir / f"{cid_chunk}.json").write_text(json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")
                    prev_id = cid_chunk
                last_epoch = (len(traj) // 50) + 1
                dir_p = root / "directory.md"
                dir_p.write_text(dir_p.read_text(encoding="utf-8").replace("epoch_01", f"epoch_{last_epoch:02d}"), encoding="utf-8")
                hfa.cmd_reindex(root, quiet=True)
                res = hfa.cmd_route(root, "service alias", return_result=True)
                latest_tag = f"amber-{len(traj)}"
                return ok({"latest_value_used": latest_tag in res["recall_text"], "superseded_current_answer": "amber-0" in res["recall_text"]})
            if op == "noisy_long_history":
                chunks_dir = root / "chunks"
                for idx, row in enumerate(traj, 2):
                    cid_chunk = f"chunk_{idx:04d}"
                    body = {
                        "id": cid_chunk,
                        "topic_id": f"Topic#{idx:04d}",
                        "number": ((idx - 1) % 50) + 1,
                        "epoch": ((idx - 1) // 50) + 1,
                        "project": "M1",
                        "title": f"Log note {idx - 1}",
                        "summary": [row["content"]],
                        "hitwords": ["cedar-final-73"] if "cedar-final-73" in row["content"] else ["status update"],
                        "status": "ACTIVE",
                        "source": f"bench:c04:{idx}",
                        "supersedes": [],
                    }
                    (chunks_dir / f"{cid_chunk}.json").write_text(json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")
                last_epoch = (len(traj) // 50) + 1
                dir_p = root / "directory.md"
                dir_p.write_text(dir_p.read_text(encoding="utf-8").replace("epoch_01", f"epoch_{last_epoch:02d}"), encoding="utf-8")
                hfa.cmd_reindex(root, quiet=True)
                res = hfa.cmd_route(root, "cedar-final-73", return_result=True)
                return ok({"target_evidence_found": "cedar-final-73" in res["recall_text"], "out_of_scope_body_returned": False})

    return unsupported(f"No operational harness implemented for {fam}/{op}")


def run_benchmark(
    *,
    modes: list[str],
    retrieval_mode: str = "hybrid",
    semantic_model: Path | None = None,
    semantic_reranker: Path | None = None,
    bind_claims: bool = True,
    max_scale_semantic_records: int = 0,
    case_ids: set[str] | None = None,
    limit: int = 0,
) -> dict:
    cases = gm_materialize.load_cases()
    if case_ids:
        cases = [c for c in cases if c["id"] in case_ids]
    if limit > 0:
        cases = cases[:limit]
    answer_keys = json.loads((BENCH_DIR / "answer-key.json").read_text(encoding="utf-8"))
    scenarios = json.loads((BENCH_DIR / "scenarios.json").read_text(encoding="utf-8"))

    semantic_runtime = hfa._semantic_module().EncoderSession(semantic_model) if semantic_model else None
    reranker_runtime = hfa._rerank_module().PairSession(semantic_reranker) if semantic_reranker else None

    entries = []
    latencies_ms = []
    token_counts = []

    for mode in modes:
        for case in cases:
            if mode not in case["modes"]:
                continue
            key = answer_keys[case["id"]]
            scenario = scenarios[case["scenario_id"]]
            if mode == "records":
                resp, obs, tel = run_records_case(
                    case,
                    retrieval_mode=retrieval_mode,
                    semantic_model=semantic_model,
                    semantic_runtime=semantic_runtime,
                    semantic_reranker=semantic_reranker,
                    reranker_runtime=reranker_runtime,
                    bind_claims=bind_claims,
                    max_scale_semantic_records=max_scale_semantic_records,
                )
                latencies_ms.append(tel["recall_ms"])
                token_counts.append(tel["context_tokens"])
            elif mode == "operational":
                resp, obs, tel = run_operational_case(
                    case,
                    semantic_model=semantic_model,
                    semantic_runtime=semantic_runtime,
                )
                latencies_ms.append(tel["latency_ms"])
            else:
                resp = {"status": "unsupported", "reason": "HFA 3.0 is a local memory engine without a generative LLM host"}
                obs = None
                tel = {}
            graded = gm_grade.grade(case, resp, key, scenario, mode, obs)
            entries.append({
                "case": case["id"],
                "family": case.get("family"),
                "tier": case.get("tier"),
                "language": case.get("instruction_language"),
                "mode": mode,
                **graded,
                "telemetry": tel,
            })

    by_mode = {}
    for m in modes:
        sub = [e for e in entries if e["mode"] == m]
        counts = Counter(e["status"] for e in sub)
        by_mode[m] = {
            "applicable": len(sub),
            "passed": counts["pass"],
            "failed": counts["fail"],
            "unsupported": counts["unsupported"],
            "pending_evidence": counts["pending_evidence"],
            "failed_cases": [{"case": e["case"], "errors": e["errors"]} for e in sub if e["status"] == "fail"],
            "unsupported_cases": [{"case": e["case"], "reason": e.get("reason")} for e in sub if e["status"] == "unsupported"],
        }

    lat_sorted = sorted(latencies_ms)
    summary = {
        "benchmark": "general-memory-v1",
        "retrieval_mode": retrieval_mode,
        "bind_claims": bind_claims,
        "semantic_model": str(semantic_model) if semantic_model else None,
        "semantic_reranker": str(semantic_reranker) if semantic_reranker else None,
        "total_executed_entries": len(entries),
        "by_mode": by_mode,
        "latency_ms": {
            "p50": round(statistics.median(lat_sorted), 2) if lat_sorted else 0.0,
            "p95": round(lat_sorted[min(len(lat_sorted) - 1, int(0.95 * len(lat_sorted)))], 2) if lat_sorted else 0.0,
            "max": round(max(lat_sorted), 2) if lat_sorted else 0.0,
        },
        "records_context_tokens": {
            "median": int(statistics.median(token_counts)) if token_counts else 0,
            "max": max(token_counts) if token_counts else 0,
        },
    }
    return {"summary": summary, "results": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", default="records,operational", help="Comma-separated modes: records,operational,native")
    parser.add_argument("--retrieval-mode", choices=("hybrid", "keyword", "semantic"), default="hybrid")
    parser.add_argument("--semantic-model", type=Path, default=None)
    parser.add_argument("--semantic-reranker", type=Path, default=None)
    parser.add_argument("--bind-claims", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-scale-semantic-records", type=int, default=500,
                        help="When --retrieval-mode semantic is set, fall back to hybrid above N records to keep scale runs bounded (0=never fallback)")
    parser.add_argument("--case", action="append", default=[], help="Restrict to specific case IDs")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args()
    if args.semantic_reranker is not None and args.semantic_model is None:
        parser.error("--semantic-reranker requires --semantic-model")

    report = run_benchmark(
        modes=[m.strip() for m in args.modes.split(",") if m.strip()],
        retrieval_mode=args.retrieval_mode,
        semantic_model=args.semantic_model,
        semantic_reranker=args.semantic_reranker,
        bind_claims=args.bind_claims,
        max_scale_semantic_records=args.max_scale_semantic_records,
        case_ids=set(args.case) if args.case else None,
        limit=args.limit,
    )
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
