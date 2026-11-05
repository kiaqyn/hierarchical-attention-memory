"""Small agent-facing projection; complete measured memory text is never truncated.

The full debug envelope remains available. Compact metadata is not an invoice,
and does not claim host acceptance or model use. No retrieval runs in this module.
"""


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
        **({"query_plan": result["query_plan"]} if "query_plan" in result else {}),
    }
