"""Small agent-facing projection; complete measured memory text is never truncated.

The full debug envelope remains available. Compact metadata is not an invoice,
and does not claim host acceptance or model use. No retrieval runs in this module.
"""
import json


def _compact_query_plan(plan):
    """Keep either caller text or an exact input-position map, whichever is smaller.

    Older/mixed-version results without a validated map keep their old envelope.
    Full debug results are never mutated. Positions refer to the original caller
    list, so trimming/deduplication cannot silently relabel a coverage direction.
    """
    if not isinstance(plan, dict):
        return plan
    result = dict(plan)
    queries = plan.get("refinement_queries")
    positions = plan.get("refinement_input_indices")
    if (not isinstance(queries, list) or not isinstance(positions, list)
            or len(queries) != len(positions) or len(queries) > 3
            or any(not isinstance(q, str) for q in queries)
            or any(type(i) is not int or not 0 <= i < 3 for i in positions)
            or positions != sorted(set(positions))):
        return result
    def size(key, value):
        return len(json.dumps({key: value}, ensure_ascii=False, separators=(",", ":")).encode("utf8"))
    omit = ("refinement_queries" if size("refinement_input_indices", positions) < size("refinement_queries", queries)
            else "refinement_input_indices")
    result.pop(omit)
    return result


def compact_recall(result):
    """One copy of evidence text, pointers, scope and actionable state.

    Search/status-check prompts are already in context_text when the budget
    permits them; returning another copy wastes input. Source strings are also
    preserved verbatim in that text, alongside each record's exact source path.
    Diagnostic ranking terms and repeated router/summary/snippet text stay in
    the original full response, rather than silently shrinking the evidence.
    """
    return {
        "status": "retrieved",
        "recall_executed": True,
        "memory_text": result["context_text"],
        "matches": [{k: item[k] for k in ("id", "path")}
                    for item in result["selected"]],
        "zero_hits": result["zero_hits"],
        "deferred": result["deferred"][:20],
        "deferred_total": len(result["deferred"]),
        "mainline": result["active_mainline_lock"],
        "payload_tokens": result["context_tokens"],
        "payload_budget": result["context_token_budget"],
        "delivery": result["delivery"],
        "retrieval": result.get("retrieval", {"backend": "lexical"}),
        "profile_drift": result["profile_drift"],
        "lateral_attention_triggered": result["lateral_attention_triggered"],
        "turn_count": result["turn_count"],
        "turn_count_exact": result["turn_count_exact"],
        "recall_count": result["recall_count"],
        "status_check_due": result["status_check_due"],
        **({"turn_budget": result["turn_budget"]} if "turn_budget" in result else {}),
        **({"query_plan": _compact_query_plan(result["query_plan"])} if "query_plan" in result else {}),
    }
