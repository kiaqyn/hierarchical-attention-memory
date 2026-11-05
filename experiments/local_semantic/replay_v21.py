"""Verify saved score/field decisions after non-behavioral validation changes.

No re-calibration, network, new model scoring, private memory or fixture edits.
"""
import json
from pathlib import Path
from rerank_v2 import field_support, select_verified, choose_with_preservation


def replay(path):
    report = json.loads(path.read_text(encoding="utf8"))
    docs = {doc["id"]:doc for doc in report["corpus"]}
    eligible = set(report["boundaries"]["eligible"])
    threshold = report["calibration"]["threshold"]
    margin = report["calibration"]["margin"]
    checked,quotes = 0,0
    for rows in (report["cases"],report["legacy_cases"],report["older_cases"]):
        for row in rows:
            for key,support in row["field_support"].items():
                assert key in eligible
                assert field_support(row["query"],docs[key]["body"])==support
                for check in support["checks"]:
                    if check["present"]:
                        assert docs[key]["body"][check["start"]:check["end"]]==check["quote"]
                        quotes += 1
            strict = select_verified(row["reranking"],row["field_support"],threshold,margin)
            compatible = choose_with_preservation(row["predictions"]["v1_hybrid"],row["reranking"],
                                                  row["field_support"],threshold,margin)
            assert strict==row["predictions"]["v2_with_fields"]
            assert compatible==row["predictions"]["v21_preserve"]
            checked += 1
    result = {"decisions_replayed":checked,"exact_source_spans_checked":quotes,
              "all_same":True,"threshold_unchanged":threshold,"margin_unchanged":margin}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    import sys
    replay(Path(sys.argv[1]))
