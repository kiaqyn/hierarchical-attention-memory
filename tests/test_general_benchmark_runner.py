"""Fast contract tests for benchmarks/general-memory-v1/run_hfa3.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = ROOT / "benchmarks" / "general-memory-v1"


def _load_runner():
    spec = importlib.util.spec_from_file_location("gm_run_hfa3", BENCH_DIR / "run_hfa3.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GeneralBenchmarkRunnerChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = _load_runner()

    def test_representative_records_cases_pass_independent_grader(self):
        report = self.runner.run_benchmark(
            modes=["records"],
            retrieval_mode="hybrid",
            bind_claims=True,
            case_ids={"M01-01-zh", "M04-03-zh", "M15-01-zh", "A07-02-en"},
        )
        summary = report["summary"]["by_mode"]["records"]
        self.assertEqual(summary["applicable"], 4)
        self.assertEqual(summary["passed"], 4, summary["failed_cases"])
        self.assertEqual(summary["failed"], 0)
        self.assertLessEqual(report["summary"]["records_context_tokens"]["max"], 1600)

    def test_operational_cases_execute_cleanly_and_report_unsupported_honestly(self):
        report = self.runner.run_benchmark(
            modes=["operational", "native"],
            retrieval_mode="hybrid",
            case_ids={
                "A08-04-en",
                "R01-01-zh",
                "R03-01-zh",
                "R05-02-en",
                "R07-01-zh",
                "R07-02-en",
                "R07-03-zh",
                "R08-02-en",
                "R08-04-en",
                "C02-01-zh",
                "C04-01-zh",
                "C01-01-zh",
            },
        )
        op_summary = report["summary"]["by_mode"]["operational"]
        self.assertEqual(op_summary["applicable"], 11)
        self.assertEqual(op_summary["failed"], 0, op_summary["failed_cases"])
        self.assertEqual(op_summary["passed"], 10)
        self.assertEqual(op_summary["unsupported"], 1)
        self.assertEqual(op_summary["unsupported_cases"][0]["case"], "R08-02-en")

        nat_summary = report["summary"]["by_mode"]["native"]
        self.assertEqual(nat_summary["applicable"], 1)
        self.assertEqual(nat_summary["unsupported"], 1)


if __name__ == "__main__":
    unittest.main()
