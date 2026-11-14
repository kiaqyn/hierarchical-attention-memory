"""Integration tests for clause-level claim binding in HFA 3.0 core, CLI, and MCP."""
from __future__ import annotations

from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from validate_hfa import ROOT, hfa, invoke

try:
    from test_mcp import Client, StdioServerParameters, _open_client
except Exception:  # pragma: no cover - optional MCP SDK
    Client = None
    StdioServerParameters = None
    _open_client = None


def _binding_module():
    spec = importlib.util.spec_from_file_location("hfa_binding_test", ROOT / "scripts/hfa_binding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaimBindingUnitChecks(unittest.TestCase):
    """All core clause-level binding invariants against scripts/hfa_binding.py."""

    @classmethod
    def setUpClass(cls):
        cls.binding = _binding_module()

    def test_parse_reference_date_validation(self):
        self.assertIsNone(self.binding.parse_reference_date(None))
        self.assertEqual(self.binding.parse_reference_date("2026-03-15"), date(2026, 3, 15))
        self.assertEqual(self.binding.parse_reference_date(date(2026, 3, 15)), date(2026, 3, 15))
        for bad in ("", "2026/03/15", "2026-02-30", "today", 20260315):
            with self.assertRaises(ValueError):
                self.binding.parse_reference_date(bad)

    def test_wrong_product_and_metric_conflicts(self):
        b = self.binding
        mismatch = b.binding_support("Mercury预算多少元？", "Venus预算 60 元。")
        self.assertFalse(mismatch["allowed"])
        self.assertEqual(mismatch["status"], "explicit_value_binding_not_established")
        self.assertEqual(mismatch["binding"]["reasons"][0]["reason"], "explicit_entity_not_in_value_clause")

        metric_conflict = b.binding_support("What was the Mercury failure rate?", "Mercury success rate: 98.75%.")
        self.assertFalse(metric_conflict["allowed"])
        self.assertEqual(metric_conflict["status"], "explicit_value_binding_not_established")

        cross_clause = b.binding_support(
            "2024年 Mercury 成功率是多少？",
            "2023年 Mercury 成功率 95%；2024年 Mercury 重试率 5%。",
        )
        self.assertFalse(cross_clause["allowed"])

        valid = b.binding_support("2024年 Mercury 成功率是多少？", "2023年 Mercury 成功率 95%；2024年 Mercury 成功率 98%。")
        self.assertTrue(valid["allowed"])
        self.assertEqual(valid["status"], "explicit_value_binding_found")
        self.assertEqual(valid["binding"]["evidence"][0]["quote"], "98%")

    def test_year_month_cadence_and_ms_duration_checks(self):
        b = self.binding
        self.assertFalse(b.binding_support("What was Acme Q3 2025 latency?", "Acme Q3 2026 latency was 14ms.")["allowed"])
        self.assertFalse(b.binding_support("What was Acme 2026-03 latency?", "Acme 2026-04 latency was 14ms.")["allowed"])
        self.assertTrue(b.binding_support("What was Acme 2026-03 latency?", "Acme 2026-03 latency measured 14ms.")["allowed"])
        self.assertFalse(b.binding_support("2024年9月 Mercury 重试率是多少？", "2023年9月 Mercury 重试率5%；2024年8月 Mercury 重试率2%。")["allowed"])
        self.assertFalse(b.binding_support("Mercury每月预算多少元？", "Mercury年度预算1200元。")["allowed"])
        self.assertTrue(b.binding_support("Mercury每月预算多少元？", "Mercury月度预算100元。")["allowed"])

    def test_numeric_decimal_and_changed_values(self):
        b = self.binding
        r_dec = b.binding_support("What percentage is the Mercury failure rate?", "Mercury failure rate is 1.25%.")
        self.assertTrue(r_dec["allowed"])
        self.assertEqual(r_dec["binding"]["evidence"][0]["quote"], "1.25%")

        r_cn_change = b.binding_support("Mercury缓存有效期多久？", "Mercury缓存有效期从60秒改为300秒。")
        self.assertTrue(r_cn_change["allowed"])
        self.assertEqual(r_cn_change["binding"]["evidence"][0]["quote"], "300秒")

        r_en_change = b.binding_support("What is Acme timeout duration?", "Acme timeout changed from 60s to 300 seconds.")
        self.assertTrue(r_en_change["allowed"])
        self.assertEqual(r_en_change["binding"]["evidence"][0]["quote"], "300 seconds")

    def test_negation_planned_vs_measured_and_ambiguity(self):
        b = self.binding
        self.assertFalse(b.binding_support("Mercury成功率是多少？", "Mercury成功率不是98%。")["allowed"])
        self.assertFalse(b.binding_support("What is the Mercury success rate?", "Mercury success rate is not 98%.")["allowed"])
        self.assertFalse(b.binding_support("Mercury实际成功率是多少？", "Mercury计划成功率99%；Venus实测成功率98%。")["allowed"])
        self.assertFalse(b.binding_support("Mercury成功率是多少？", "Mercury 未记录成功率，Venus成功率98%。")["allowed"])
        self.assertFalse(b.binding_support("Mercury预算多少元？", "Mercury and Venus budgets are 60元 and 80元 respectively.")["allowed"])
        self.assertFalse(b.binding_support("Mercury 成功率是多少？", "Mercury success rate and retry rate are 95% and 5%.")["allowed"])

    def test_relative_reference_date_handling(self):
        b = self.binding
        body = "Acme 2025 latency was 18ms."
        unanchored = b.binding_support("What was Acme last year latency?", body)
        self.assertFalse(unanchored["allowed"])
        self.assertEqual(unanchored["status"], "binding_reference_date_required")

        anchored_ok = b.binding_support("What was Acme last year latency?", body, date(2026, 4, 10))
        self.assertTrue(anchored_ok["allowed"])

        anchored_wrong = b.binding_support("What was Acme last year latency?", "Acme 2024 latency was 18ms.", date(2026, 4, 10))
        self.assertFalse(anchored_wrong["allowed"])

        with self.assertRaises(TypeError):
            b.binding_support("去年预算多少元？", "预算100元。", "2026-01-01")

    def test_non_numeric_passthrough_secrets_aliases_and_post_filter(self):
        b = self.binding
        plain = b.binding_support("以前怎么解决登录回跳？", "Cookie设置SameSite=Lax。")
        self.assertTrue(plain["allowed"])
        self.assertEqual(plain["binding"]["coverage"], "not_checked_non_numeric_request")

        secret = b.binding_support("管理员密码是什么？", "密码未记录。")
        self.assertFalse(secret["allowed"])
        self.assertEqual(secret["status"], "needs_authorized_secret_source")

        self.assertTrue(b.binding_support("Mercury副本保留多久？", "Mercury缓存有效期10秒。")["allowed"])
        self.assertFalse(b.binding_support("Mercury缓存有效期多久？", "Mercury日志保留10天。")["allowed"])
        self.assertEqual(b.metric_names("订单处理完成的比例是多少？", "percentage"), set())
        self.assertEqual(b.metric_names("请求中有多大比例需要再次提交？", "percentage"), {"retry_rate"})

        docs = {"wrong": {"body": "Venus预算60元。"}, "right": {"body": "Mercury预算80元。"}}
        res = b.filter_bound_candidates("Mercury预算多少元？", ["wrong", "right"], docs)
        self.assertEqual(res["selected"], ["right"])
        with self.assertRaises(ValueError):
            b.filter_bound_candidates("问题", ["wrong", "wrong"], docs)
        with self.assertRaises(ValueError):
            b.filter_bound_candidates("问题", ["a", "b", "c"], docs)


class ClaimBindingManagerAndCliChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-bind-int-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "memory"
        invoke(hfa.cmd_init, self.root)

    def add(self, title, hit_words, conclusion, source="test:claim", project=None):
        _, raw = invoke(hfa.cmd_add_topic, self.root, title, hit_words, conclusion, None, project, source, None, None, True)
        return json.loads(raw)["id"]

    def test_binding_filters_mismatched_hit_without_consuming_slot(self):
        wrong = self.add("Beta 2026 latency baseline", "latency-probe", "Beta 2026 latency measured at 99ms.")
        right = self.add("Acme 2026 latency baseline", "latency-probe", "Acme 2026 latency measured at 14ms.")

        res = hfa.cmd_route(
            self.root,
            "What was Acme 2026 latency?",
            mode="hybrid",
            max_topics=1,
            bind_claims=True,
            return_result=True,
        )
        self.assertEqual([s["id"] for s in res["selected"]], [right])
        self.assertEqual(res["claim_binding"]["accepted_ids"], [right])
        self.assertEqual(res["claim_binding"]["rejected_ids"], [wrong])
        deferred_reasons = {d["id"]: d["reason"] for d in res["deferred"]}
        self.assertEqual(deferred_reasons[wrong], "claim_binding:explicit_value_binding_not_established")

    def test_config_opt_in_validation_and_cli_override(self):
        self.add("Beta config baseline", "cfg-probe", "Beta 2026 latency measured at 33ms.")
        cfg_path = self.root / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["claim_binding"] = "invalid"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            hfa._config(self.root)

        cfg["claim_binding"] = True
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.root)

        enabled = hfa.cmd_route(self.root, "What was Acme 2026 latency?", return_result=True)
        self.assertEqual(enabled["selected"], [])
        self.assertTrue(enabled["claim_binding"]["enabled"])
        self.assertEqual(len(enabled["claim_binding"]["rejected_ids"]), 1)

        overridden = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/hfa_manager.py"),
                "recall",
                str(self.root),
                "--query",
                "What was Acme 2026 latency?",
                "--no-bind-claims",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        payload = json.loads(overridden.stdout)
        self.assertNotIn("claim_binding", payload)
        self.assertEqual(len(payload["selected"]), 1)

    def test_branch_isolation_and_relative_reference_date_cli(self):
        side = self.add("Beta 2025 secret latency", "branch-probe", "Beta 2025 latency was 77ms.", project="S1")
        main_id = self.add("Acme 2025 archive", "acme-lat", "Acme 2025 latency measured at 15ms.")

        # Without --unlock, branch chunks remain gated behind unlock reason (no claim_binding leakage).
        locked = hfa.cmd_route(
            self.root,
            "What was Acme last year latency?",
            bind_claims=True,
            reference_date="2026-06-01",
            return_result=True,
        )
        self.assertEqual([s["id"] for s in locked["selected"]], [main_id])
        self.assertFalse(any(d["id"] == side and d["reason"].startswith("claim_binding:") for d in locked["deferred"]))

        unlocked = hfa.cmd_route(
            self.root,
            "What was Acme branch-probe 2025 latency?",
            unlock="audit branch",
            bind_claims=True,
            return_result=True,
        )
        self.assertEqual([s["id"] for s in unlocked["selected"]], [main_id])
        self.assertEqual(
            {d["id"]: d["reason"] for d in unlocked["deferred"]}[side],
            "claim_binding:explicit_value_binding_not_established",
        )

        # Invalid reference date fails loudly.
        with self.assertRaises(hfa.MemoryError):
            hfa.cmd_route(self.root, "What was Acme 2025 latency?", bind_claims=True, reference_date="2026-99-99", return_result=True)

        cli_run = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/hfa_manager.py"),
                "route",
                str(self.root),
                "--query",
                "What was Acme last year latency?",
                "--bind-claims",
                "--reference-date",
                "2026-06-01",
                "--output",
                "compact",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(cli_run.returncode, 0, cli_run.stderr)
        compact = json.loads(cli_run.stdout)
        self.assertEqual([m["id"] for m in compact["matches"]], [main_id])
        self.assertEqual(compact["claim_binding"]["accepted_ids"], [main_id])
        self.assertNotIn("decisions", compact["claim_binding"])

    def test_print_config_and_stdio_mcp_claim_binding(self):
        cfg = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/hfa_mcp.py"),
                "--memory-dir",
                str(self.root),
                "--bind-claims",
                "--compact",
                "--print-config",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(cfg.returncode, 0, cfg.stderr)
        args = json.loads(cfg.stdout)["mcpServers"]["hfa-project-memory"]["args"]
        self.assertIn("--bind-claims", args)
        self.assertIn("--compact", args)

        if Client is None:
            self.skipTest("optional MCP SDK not installed in current Python")

        wrong = self.add("Beta 2025 outage", "mcp-claim-probe", "Beta 2025 latency was 40ms.")
        right = self.add("Acme 2025 latency", "mcp-claim-probe", "Acme 2025 latency was 11ms.")

        import anyio

        async def exercise():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", str(ROOT / "scripts/hfa_mcp.py"), "--memory-dir", str(self.root), "--no-auto-init"],
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            async with _open_client(params) as client:
                out = await client.call_tool(
                    "recall_memory",
                    {
                        "query": "What was Acme last year mcp-claim-probe latency?",
                        "bind_claims": True,
                        "reference_date": "2026-07-01",
                    },
                )
                self.assertFalse(out.is_error)
                data = out.structured_content
                self.assertEqual([m["id"] for m in data["matches"]], [right])
                self.assertIn("binding", data["matches"][0])
                self.assertEqual(data["claim_binding"]["rejected_ids"], [wrong])
                self.assertEqual(data["claim_binding"]["accepted_ids"], [right])

        anyio.run(exercise)

    def test_multi_direction_refinement_preserves_valid_directions(self):
        wrong_a = self.add("Beta 2026 latency", "dir-a", "Beta 2026 latency measured at 90ms.")
        right_a = self.add("Acme 2026 latency", "dir-a", "Acme 2026 latency measured at 12ms.")
        right_b = self.add("Nova 2026 retry rate", "dir-b", "Nova 2026 retry rate measured at 1.5%.")

        res = hfa.cmd_route(
            self.root,
            "What were Acme 2026 latency and Nova 2026 retry rate?",
            refinement_queries=["What was Acme 2026 latency?", "What is Nova 2026 retry rate?"],
            max_topics=2,
            bind_claims=True,
            return_result=True,
        )
        self.assertEqual(set(s["id"] for s in res["selected"]), {right_a, right_b})
        self.assertIn(wrong_a, res["claim_binding"]["rejected_ids"])
        self.assertEqual(res["query_plan"]["uncovered_query_indices"], [])


if __name__ == "__main__":
    unittest.main()
