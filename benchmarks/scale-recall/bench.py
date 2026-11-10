#!/usr/bin/env python3
"""Synthetic recall-latency comparison for the source-stat cache.

Builds an isolated memory, writes N L1 files, reindexes once, then times
`full_verify` recalls against the stat fast path. Numbers are from this
process only. They are not a host, disk, or production SLA.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from validate_hfa import hfa  # noqa: E402


def build(root: Path, count: int) -> None:
    hfa.cmd_init(root, quiet=True, profile="minimal")
    chunks = root / "chunks"
    chunks.mkdir(parents=True, exist_ok=True)
    per_epoch = 50
    for i in range(1, count + 1):
        epoch = (i - 1) // per_epoch + 1
        number = (i - 1) % per_epoch + 1
        body = {
            "id": f"chunk_{i:04d}",
            "topic_id": f"Topic#{number:02d}",
            "number": number,
            "epoch": epoch,
            "project": "M1",
            "title": f"Synthetic fact {i}",
            "summary": [f"SYNTHETIC_FACT_{i} is stored only for the latency probe."],
            "hitwords": [f"synthetic-fact-{i}"],
            "status": "ACTIVE",
            "source": "benchmark:scale",
            "supersedes": [],
        }
        (chunks / f"chunk_{i:04d}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    directory = (root / "directory.md").read_text(encoding="utf-8")
    epoch = (count - 1) // per_epoch + 1
    directory = directory.replace("epoch_01", f"epoch_{epoch:02d}")
    (root / "directory.md").write_text(directory, encoding="utf-8")
    hfa.cmd_reindex(root, quiet=True)


def time_recall(root: Path, query: str, repeats: int, **kwargs) -> dict:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = hfa.cmd_route(root, query, return_result=True, **kwargs)
        samples.append((time.perf_counter() - started) * 1000)
        if not result["selected"]:
            raise SystemExit(f"Synthetic query missed during timing: {query}")
    return {
        "repeats": repeats,
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,5000")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    report = {"kind": "synthetic local latency, not a real-world SLA", "runs": []}
    for raw in args.sizes.split(","):
        count = int(raw)
        with tempfile.TemporaryDirectory(prefix=f"hfa-scale-{count}-") as temp:
            root = Path(temp) / "memory"
            started = time.perf_counter()
            build(root, count)
            setup_s = round(time.perf_counter() - started, 3)
            query = f"synthetic-fact-{count}"
            before = time_recall(root, query, args.repeats, index_source="json")
            after = time_recall(root, query, args.repeats, index_source="sqlite")
            report["runs"].append({
                "chunks": count,
                "setup_reindex_seconds": setup_s,
                "json_index": before,
                "sqlite_index": after,
                "note": "json_index parses search.json, hitwords.json, and catalog.json; sqlite_index queries FTS and hit words and reads only selected chunk files. Synthetic, not an SLA.",
            })
            print(json.dumps(report["runs"][-1], ensure_ascii=False), flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
