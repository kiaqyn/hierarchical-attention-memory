"""Actual miss regression, query-only compatibility and conservative abstention."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from validate_hfa import hfa, invoke

FAILED_QUERY = "启动 加载 loading 小圆圈 进不了首页 解决办法"


class QueryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-query-boundaries-")
        self.mem = Path(self.temp.name).resolve()/"memory"
        hfa.cmd_init(self.mem, quiet=True, profile="example")
        rc, output = invoke(hfa.cmd_add_topic, self.mem, "加载停滞修复", "boot-ticket",
                            "将 Renderer 依赖固定为 4.2，清理本地缓存后验证启动通过。")
        self.assertEqual(rc, 0, output)

    def tearDown(self):
        self.temp.cleanup()

    def recall(self, query):
        rc, output = invoke(hfa.cmd_route, self.mem, query)
        self.assertEqual(rc, 0, output)
        return json.loads(output)

    def test_actual_failed_followup_recalls_without_adding_alias_or_keyword(self):
        result = self.recall(FAILED_QUERY)
        self.assertEqual(result["selected"][0]["id"], "chunk_0002")
        self.assertEqual(result["selected"][0]["match"]["method"], "content")
        self.assertIn("Renderer", result["recall_text"])
        self.assertLessEqual(result["context_tokens"]["count"], result["context_token_budget"])

    def test_separated_query_terms_do_not_create_cross_word_bigrams(self):
        terms = self.recall(FAILED_QUERY)["query_terms"]
        self.assertTrue({"启动", "加载", "loading"}.issubset(terms))
        self.assertFalse({"动加", "圈进", "页解"}.intersection(terms))
        self.assertIn("动加", hfa._search_terms("启动 加载", {}))
        self.assertNotIn("动加", hfa._search_terms("启动 加载", {}, query=True))

    def test_fts_and_fallback_produce_same_match(self):
        normal = self.recall(FAILED_QUERY)
        with patch.object(hfa, "_fts_search", return_value=(None, None)):
            fallback = self.recall(FAILED_QUERY)
        self.assertEqual(normal["selected"], fallback["selected"])

    def test_character_spacing_and_fullwidth_input_remain_supported(self):
        self.assertEqual(hfa._search_terms("数 据 库", {}, query=True), {"数据", "据库"})
        result = self.recall("启动　加载　ｌｏａｄｉｎｇ　小圆圈　进不了首页　解决办法")
        self.assertEqual(result["selected"][0]["id"], "chunk_0002")

    def test_repeated_or_one_shared_word_is_not_two_independent_clues(self):
        for query in ("加载 加载 loading singer audition", "加载 天气 保险 报销 地铁 公交",
                      "火星 大气 成分 资料", "不要查启动加载的问题"):
            with self.subTest(query=query):
                self.assertFalse(self.recall(query)["selected"])

    def test_overlapping_bigrams_do_not_become_independent_words(self):
        invoke(hfa.cmd_add_topic, self.mem, "数据库维护", "db-ticket", "定期备份数据库。")
        for query in ("数据库 singer audition", "数据 据库 singer audition", "backup singer audition"):
            with self.subTest(query=query):
                self.assertFalse(self.recall(query)["selected"])

    def test_other_separated_chinese_clues_benefit_too(self):
        invoke(hfa.cmd_add_topic, self.mem, "网络修复", "net-ticket", "重置连接后恢复网络。")
        result = self.recall("网络 连接 router 断线 排查 处理办法")
        self.assertEqual(result["selected"][0]["id"], "chunk_0003")

    def test_reads_preserve_sources_config_and_existing_index_bytes(self):
        files = [self.mem/"config.json", self.mem/"directory.md", self.mem/"summary.md",
                 *sorted((self.mem/"chunks").glob("*.json")),
                 *sorted((self.mem/"index").glob("*"))]
        before = {p:p.read_bytes() for p in files if p.is_file()}
        self.recall(FAILED_QUERY)
        self.recall("加载 转圈 首页 启动")
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 0)
        self.assertEqual(before, {p:p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
