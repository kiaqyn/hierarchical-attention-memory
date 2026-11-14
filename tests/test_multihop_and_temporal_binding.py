import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from validate_hfa import ROOT, hfa, invoke

sys.path.insert(0, str(ROOT / "scripts"))
import hfa_binding  # noqa: E402

BENCH_DIR = ROOT / "benchmarks" / "general-memory-v1"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))
import run_hfa3  # noqa: E402


class MultiHopAndTemporalBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mem = Path(self.temp.name) / "memory"
        self.assertEqual(invoke(hfa.cmd_init, self.mem, None, "minimal")[0], 0)

    def tearDown(self):
        self.temp.cleanup()

    def _add(self, title, hitword, summary, *, supersedes=None, source="test:suite#L1"):
        rc, out = invoke(
            hfa.cmd_add_topic,
            self.mem,
            title,
            hitword,
            summary,
            None,
            None,
            source,
            supersedes,
            None,
            True,
        )
        self.assertEqual(rc, 0, out)
        return json.loads(out)["id"]

    def test_compound_clause_numeric_and_retry_count_binding_preserves_vetoes(self):
        cfg_path = self.mem / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["max_recall_topics"] = 4
        cfg["recall_token_budget"] = 1000
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        c_timeout = self._add("Timeout", "timeout-policy", "客户端超时设为 8 秒。")
        c_retry = self._add("Retries", "retry-policy", "自动重试：总共最多三次尝试。")
        c_backoff = self._add("Backoff", "backoff-policy", "退避上限：每次退避最多一秒。")
        c_unrecorded = self._add("DB", "db-backup", "主库使用 PostgreSQL，未记录备份频率。")
        c_no_ip = self._add("Host", "db-host", "主库托管在 cloud-east-1 集群节点。")

        q_zh = "客户端超时 8 秒、自动重试 3 次、每次退避最多 1 秒，请找出这三条完整配置"
        res_zh = hfa.cmd_route(self.mem, q_zh, bind_claims=True, max_topics=4, return_result=True)
        sel_ids = {s["id"] for s in res_zh["selected"]}
        self.assertEqual(sel_ids, {c_timeout, c_retry, c_backoff})

        # English duration with hyphenated word numbers and written counts
        en_query = "What are the client timeout of 8 seconds, auto-retry 3 times, and one-second backoff cap?"
        self.assertTrue(
            hfa_binding.binding_support(en_query, "Per-attempt backoff is capped at one-second maximum.")["allowed"]
        )
        self.assertTrue(
            hfa_binding.binding_support(en_query, "Automatic retry runs at most three attempts.")["allowed"]
        )
        # Timeout/backoff count clauses without repeating "retry/attempt" still bind retry-policy records,
        # while unrelated non-retry occurrence counts stay vetoed.
        compound_dur_q = "不计多余耗时，四次都超时且三次退避两秒，最多共多少秒？"
        self.assertTrue(
            hfa_binding.binding_support(compound_dur_q, "自动重试：总共最多四次尝试，含首次。")["allowed"]
        )
        self.assertFalse(
            hfa_binding.binding_support(compound_dur_q, "上次批量导入失败了四次，请排查。")["allowed"]
        )

        # Conservative vetoes must remain strictly blocked
        unrecorded_res = hfa.cmd_route(
            self.mem, "主库是什么、多久备份一次？", bind_claims=True, return_result=True
        )
        self.assertEqual(unrecorded_res["selected"], [])
        self.assertEqual(
            {d["id"]: d["reason"] for d in unrecorded_res["deferred"]}.get(c_unrecorded),
            "claim_binding:requested_fields_missing",
        )

        self.assertFalse(
            hfa_binding.binding_support(
                "What is the IP address of the primary host?",
                "Primary host runs on cloud-east-1 cluster node.",
            )["allowed"]
        )
        ip_res = hfa.cmd_route(
            self.mem, "主库所在机器的 IP 地址是多少？", bind_claims=True, return_result=True
        )
        self.assertEqual(ip_res["selected"], [])
        self.assertFalse(
            hfa_binding.binding_support(
                "主库所在机器的 IP 地址是多少？",
                "主库托管在 cloud-east-1 集群节点。",
            )["allowed"]
        )
        self.assertIn(c_no_ip, {c["id"] for c in hfa._load_verified_chunks(self.mem, hfa.parse_directory_nodes(self.mem / "directory.md"))})

    def test_in_ram_identifier_subtokens_and_morphology_without_index_migration(self):
        c_health = self._add("Health check", "readiness", "Poll health.index_ready until pipeline status is validated.")
        c_parser = self._add("Parser path", "import-rename", "Moved legacy file to src/imports/parser_v3.py.")
        c_env = self._add("Test env", "ci-setup", "Keep test-environment variables pinned to one-second timeout.")
        c_prod = self._add("Catalog", "retail", "Manage catalog products and inventory counts.")

        search_before = (self.mem / "index/search.json").read_bytes()
        fts_before = (self.mem / "index/fts.sqlite").read_bytes()

        for query, expected_id in (
            ("health index validation", c_health),
            ("which parser to check", c_parser),
            ("test environment one second", c_env),
        ):
            live = hfa.cmd_route(self.mem, query, return_result=True)
            self.assertIn(expected_id, [s["id"] for s in live["selected"]], f"live miss for {query}")
            with patch.object(hfa, "_fts_search", return_value=(None, None)):
                fallback = hfa.cmd_route(self.mem, query, return_result=True)
            self.assertEqual(
                [s["id"] for s in live["selected"]],
                [s["id"] for s in fallback["selected"]],
                f"FTS vs fallback mismatch for {query}",
            )

        # Lexical boundary preservation: "prod" must never match "products"
        prod_miss = hfa.cmd_route(self.mem, "prod", return_result=True)
        self.assertNotIn(c_prod, [s["id"] for s in prod_miss["selected"]])

        # Read-only queries must never migrate persisted index files
        self.assertEqual((self.mem / "index/search.json").read_bytes(), search_before)
        self.assertEqual((self.mem / "index/fts.sqlite").read_bytes(), fts_before)

    def test_disjoint_cjk_clues_and_bilingual_aliases(self):
        c_exp = self._add("旧经验范围", "retry-scope", "Windows 共享冲突曾通过短暂重试解决，这是此前积累的局部经验。")
        c_path = self._add("重命名", "path-migration", "原文件已迁到 src/imports/parser_v3.py。")

        res_exp = hfa.cmd_route(self.mem, "能否直接套用之前的重试经验处理新问题？", return_result=True)
        self.assertIn(c_exp, [s["id"] for s in res_exp["selected"]])

        res_path = hfa.cmd_route(self.mem, "修导入解析器时应该打开哪个文件？", return_result=True)
        self.assertIn(c_path, [s["id"] for s in res_path["selected"]])

    def test_troubleshooting_symptom_aliases_exclude_unrelated_font_loading(self):
        c_glyph = self._add("缺字排查", "glyph-bug", "导出 PDF 出现豆腐块是因为字体缺字。")
        c_slow = self._add("启动优化", "startup-slow", "启动慢：第一次加载字体会耗时两秒。")

        res = hfa.cmd_route(self.mem, "导出表格里为什么会出现小方格？", return_result=True)
        sel = [s["id"] for s in res["selected"]]
        self.assertIn(c_glyph, sel)
        self.assertNotIn(c_slow, sel)

    def test_point_in_time_as_of_unlock_vs_plain_unlock(self):
        c_oct1 = self._add("早期方案", "db-choice", "10 月 1 日决定主数据库（primary database recommendation）采用 PostgreSQL 方案。")
        c_oct3 = self._add("后续方案", "db-choice", "10 月 3 日决定主数据库（primary database recommendation）改为采用 SQLite 方案。", supersedes=[c_oct1])

        # Plain query: only current active c_oct3
        current_res = hfa.cmd_route(self.mem, "当前推荐采用哪种数据库方案？", return_result=True)
        self.assertEqual([s["id"] for s in current_res["selected"]], [c_oct3])

        # Plain unlock without historical point-in-time date MUST NOT revive superseded c_oct1
        plain_unlock = hfa.cmd_route(self.mem, "数据库方案", unlock="review", return_result=True)
        plain_sel = [s["id"] for s in plain_unlock["selected"]]
        self.assertNotIn(c_oct1, plain_sel)
        self.assertEqual(
            {d["id"]: d["reason"] for d in plain_unlock["deferred"]}.get(c_oct1),
            "superseded; historical evidence only",
        )

        # Retrospective query WITHOUT unlock stays blocked
        locked_hist = hfa.cmd_route(
            self.mem,
            "回顾 10 月 2 日当时的决定，当时推荐采用哪种数据库方案？",
            reference_date="2026-10-05",
            return_result=True,
        )
        self.assertNotIn(c_oct1, [s["id"] for s in locked_hist["selected"]])

        # Authorized point-in-time query returns Oct 1 and defers Oct 3 as postdating target date
        unlocked_zh = hfa.cmd_route(
            self.mem,
            "回顾 10 月 2 日当时的决定，当时推荐采用哪种数据库方案？",
            unlock="caller authorized",
            reference_date="2026-10-05",
            return_result=True,
        )
        self.assertEqual([s["id"] for s in unlocked_zh["selected"]], [c_oct1])
        self.assertEqual(
            {d["id"]: d["reason"] for d in unlocked_zh["deferred"]}.get(c_oct3),
            "postdates historical target date",
        )

        unlocked_en = hfa.cmd_route(
            self.mem,
            "What database recommendation was current as of October 2? Historical access is allowed.",
            unlock="caller authorized",
            reference_date="2026-10-05",
            return_result=True,
        )
        self.assertEqual([s["id"] for s in unlocked_en["selected"]], [c_oct1])
        self.assertEqual(
            {d["id"]: d["reason"] for d in unlocked_en["deferred"]}.get(c_oct3),
            "postdates historical target date",
        )

        semantic_dir = os.environ.get("HFA_TEST_SEMANTIC_MODEL")
        if semantic_dir:
            rc_idx = hfa.cmd_semantic_index(self.mem, Path(semantic_dir))
            self.assertEqual(rc_idx, 0)
            sem_res = hfa.cmd_route(
                self.mem,
                "What database choice was active as of October 2?",
                unlock="caller authorized",
                mode="semantic",
                semantic_model=Path(semantic_dir),
                reference_date="2026-10-05",
                return_result=True,
            )
            self.assertEqual([s["id"] for s in sem_res["selected"]], [c_oct1])
            self.assertEqual(
                {d["id"]: d["reason"] for d in sem_res["deferred"]}.get(c_oct3),
                "postdates historical target date",
            )
            # In-RAM historical scoring must not pollute persisted semantic cache
            self.assertEqual(hfa.cmd_semantic_index(self.mem, Path(semantic_dir), verify=True), 0)

    def test_benchmark_runner_refinement_and_bridge_helpers(self):
        facets = run_hfa3._plan_refinement_queries(
            "All three are needed: client timeout, automatic retries, and max backoff?"
        )
        self.assertEqual(facets, ["client timeout", "automatic retries", "max backoff"])

        covered_single = {"selected": [{"id": "c1", "text": "[c1] Normal fact\nUse PostgreSQL"}], "query_plan": {"uncovered_query_indices": []}}
        self.assertFalse(run_hfa3._should_run_hop2("Which database do we use?", [], covered_single))

        caveat_hit = {"selected": [{"id": "c1", "text": "[c1] Experiment result\nValidated only on synthetic data with health.index_ready."}], "query_plan": {"uncovered_query_indices": []}}
        self.assertTrue(run_hfa3._should_run_hop2("Does this carry over to our current project?", [], caveat_hit))
        hop2_q = run_hfa3._build_hop2_queries("Does this carry over to our current project?", [], caveat_hit)
        self.assertTrue(any("health.index_ready" in q for q in hop2_q))
        self.assertTrue(all(len(q) <= 256 for q in hop2_q))


if __name__ == "__main__":
    unittest.main()
