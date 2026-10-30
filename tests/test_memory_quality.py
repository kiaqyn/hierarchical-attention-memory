"""Regression cases for useful retrieval, decision updates and retry-safe writes."""
import contextlib
import io
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_hfa import HfaTests, hfa, invoke, read, snapshot


class MemoryQualityTests(HfaTests):
    # Keep this suite independent: don't rerun inherited durability cases here.
    def test_content_and_synonyms_recall_without_saved_keyword(self):
        self.add("导出报表", "export-ticket", "中文乱码已通过 UTF-8 修复；导出 CSV 时指定编码。")
        for query in ("导出 CSV 的编码", "中文变问号", "garbled text"):
            result = self.route(query)
            self.assertEqual(result["selected"][0]["id"], "chunk_0002", query)
            self.assertEqual(result["selected"][0]["match"]["method"], "content")
        rc, old = invoke(hfa.cmd_route, self.mem, "中文变问号", None, None, "keyword")
        self.assertEqual(json.loads(old)["selected"], [])

    def test_generic_unrelated_and_negative_queries_abstain(self):
        self.add("导出报表", "export-ticket", "中文乱码已通过 UTF-8 修复。")
        self.add("数据库备份", "db-ticket", "每天备份数据库。")
        for query in ("之前的那个怎么处理", "今天晚饭吃什么", "不要查中文变问号", "provider 模块", "如何处理问题"):
            self.assertEqual(self.route(query)["selected"], [], query)
        self.assertEqual(self.route("backup singer audition")["selected"], [])

    def test_content_retrieval_keeps_branch_permission_boundary(self):
        self.add("导出报表", "branch-ticket", "中文乱码已通过 UTF-8 修复。")
        path = self.mem / "chunks/chunk_0002.json"
        chunk = json.loads(read(path)); chunk["project"] = "S1"
        path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        self.assertEqual(self.route("中文变问号")["selected"], [])
        self.assertEqual(self.route("中文变问号", "用户要求检查导出问题")["selected"][0]["id"], "chunk_0002")

    def test_ranking_prefers_specific_match_then_newer_tie(self):
        self.add("cache", "cache", "old general cache fact")
        self.add("cache", "cache", "new general cache fact")
        self.add("redis cache", "cache, redis", "redis cache expires after 60 seconds")
        result = self.route("redis cache")
        self.assertEqual(result["selected"][0]["id"], "chunk_0004")
        result = self.route("cache")
        self.assertEqual(result["selected"][0]["id"], "chunk_0004")

    def remember(self, title="发布约定", words="发布规则", body="发布前先确认。", source="chat:turn-12", supersedes=None, request_id="event-1"):
        rc, output = invoke(hfa.cmd_add_topic, self.mem, title, words, body, None, None,
                            source, supersedes, request_id, True)
        self.assertEqual(rc, 0)
        return json.loads(output)

    def test_explicit_supersession_keeps_old_bytes_and_only_returns_new_decision(self):
        old = self.remember(body="允许自动发布。")
        path = self.mem / old["path"]; before = path.read_bytes()
        new = self.remember(body="发布前必须先确认。", supersedes=[old["id"]], request_id="event-2")
        result = self.route("发布规则")
        self.assertEqual([c["id"] for c in result["selected"]], [new["id"]])
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("chat:turn-12", result["recall_text"])
        # Even unlock does not silently revive a superseded decision as current.
        self.assertEqual([c["id"] for c in self.route("发布规则", "review")["selected"]], [new["id"]])

    def test_retries_are_noop_and_conflicting_reuse_is_rejected(self):
        first = self.remember(); before = snapshot(self.mem)
        second = self.remember()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(snapshot(self.mem), before)
        with self.assertRaises(hfa.MemoryError):
            self.remember(body="不同的新决定。")
        self.assertEqual(snapshot(self.mem), before)

    def test_concurrent_retry_deduplication(self):
        def writer(i):
            return self.cli("add-topic", str(self.mem), "--title", "Same event", "--hit-words", "same-event",
                            "--conclusion", "one fact", "--request-id", "same-event", "--json")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(writer, range(12)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stdout for r in results])
        items = [json.loads(r.stdout) for r in results]
        self.assertEqual(len({x["id"] for x in items}), 1)
        self.assertEqual(sum(not x["duplicate"] for x in items), 1)
        self.assertEqual(len(list((self.mem / "chunks").glob("*.json"))), 2)

    def test_supersession_failure_does_not_hide_old_decision(self):
        first = self.remember()
        before = snapshot(self.mem)
        original = hfa._atomic_write
        failed = False
        def fail_once(path, content):
            nonlocal failed
            original(path, content)
            if path.name == "search.json" and not failed:
                failed = True
                raise OSError("injected search index error")
        with patch.object(hfa, "_atomic_write", fail_once):
            with self.assertRaises(OSError):
                self.remember(body="Changed", supersedes=[first["id"]], request_id="event-2")
        self.assertEqual(snapshot(self.mem), before)
        self.assertEqual(self.route("发布规则")["selected"][0]["id"], first["id"])

    def test_invalid_supersession_and_aliases_fail_without_writing(self):
        before = snapshot(self.mem)
        with self.assertRaises(hfa.MemoryError):
            self.remember(supersedes=["../../outside"])
        self.assertEqual(snapshot(self.mem), before)
        config = self.mem / "config.json"
        value = json.loads(read(config)); value["retrieval_aliases"] = [["x", 42]]
        config.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            self.route("x")

    def test_custom_aliases_and_normalized_unicode(self):
        self.add("文档保管", "vault-key", "手册存放在保险箱。")
        path = self.mem / "config.json"
        config = json.loads(read(path)); config["retrieval_aliases"] = [["保险箱", "知识仓库"]]
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        self.assertEqual(self.route("知识仓库")["selected"][0]["id"], "chunk_0002")
        self.assertEqual(self.route("ＶＡＵＬＴ－ＫＥＹ")["selected"][0]["id"], "chunk_0002")

    def test_source_included_in_budget_and_queries_are_bounded(self):
        result = self.remember(source="evidence:" + "a" * 220)
        value = self.route("发布规则")
        self.assertLessEqual(value["recall_tokens"]["count"], value["recall_token_budget"])
        self.assertIn("evidence:", value["recall_text"])
        for query in ("", " " * 5, "a" * 8001):
            with self.assertRaises(hfa.MemoryError):
                self.route(query)

    def test_supersession_across_epochs_updates_historical_view(self):
        first = self.remember()
        path = self.mem / "config.json"
        config = json.loads(read(path)); config["max_topics_per_epoch"] = 2
        path.write_text(json.dumps(config), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        new = self.remember(body="改为先确认再发布。", supersedes=[first["id"]], request_id="event-2")
        self.assertIn("已被 " + new["id"] + " 替代", read(self.mem / "history/epoch_01/summaries.md"))
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 0)

    def test_tampered_supersession_cycle_or_fork_is_rejected(self):
        old = self.remember()
        new = self.remember(body="第二版。", supersedes=[old["id"]], request_id="event-2")
        third = self.remember(body="无关决定。", request_id="event-3")
        path = self.mem / third["path"]
        value = json.loads(read(path)); value["supersedes"] = [old["id"]]
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            invoke(hfa.cmd_reindex, self.mem)

    def test_negation_boundaries_acronyms_modifiers_and_profile_drift_precision(self):
        self.add("redis cache", "cache, redis", "redis cache expires after 60 seconds")
        self.add("API gateway", "gw-ticket", "The API gateway uses the CLI for npm, Git and SQL")
        self.add("运行时部署与日志", "runtime-log", "whenever the server restarts avoid unavoidable error in runtime; deploy without downtime; 日志级别是 INFO")
        for q in ("whenever the server restarts", "unavoidable error in runtime", "deploy without downtime", "日志级别是什么"):
            self.assertEqual(self.route(q)["selected"][0]["id"], "chunk_0004", q)
        for q in ("how to use redis cache with python client", "please check redis cache config"):
            self.assertEqual(self.route(q)["selected"][0]["id"], "chunk_0002", q)
            self.assertEqual(self.route(q)["skipped_tombstones"], [])
        for acronym in ("API", "CLI", "Git", "SQL", "npm"):
            self.assertEqual(self.route(acronym)["selected"][0]["id"], "chunk_0003", acronym)
        kw_hit = self.route("redis")
        self.assertIn("redis", kw_hit["selected"][0]["match"]["terms"])
        for technical in ("如何取消自动执行的定时任务？", "把数据库回复超时改成30秒"):
            self.assertFalse(self.route(technical)["profile_drift"], technical)

    def test_fts5_hybrid_retrieval_and_expanded_paraphrase_aliases(self):
        self.add("报表导出", "乱码, CSV, export-case", "中文乱码已通过 UTF-8 修复，CSV 导出必须指定编码。")
        self.add("发行失败", "回滚, undo-case", "出现故障就回滚，恢复上一版本。")
        self.assertEqual(self.route("输出文件全是看不懂的符号")["selected"][0]["id"], "chunk_0002")
        self.assertEqual(self.route("撤回这次更新恢复到昨天那版")["selected"][0]["id"], "chunk_0003")
        with patch.object(hfa, "_fts_search", return_value=(None, None)):
            self.assertEqual(self.route("输出文件全是看不懂的符号")["selected"][0]["id"], "chunk_0002")
            self.assertEqual(self.route("撤回这次更新恢复到昨天那版")["selected"][0]["id"], "chunk_0003")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(MemoryQualityTests(name) for name in MemoryQualityTests.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
