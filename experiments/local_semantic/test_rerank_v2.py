import unittest
from rerank_v2 import field_support, select_verified, choose_with_preservation


class EvidenceChecks(unittest.TestCase):
    def test_version_number_is_not_a_price(self):
        self.assertFalse(field_support("这次依赖花了多少钱？","Renderer 固定为 4.2 后通过验证。")["allowed"])
        self.assertFalse(field_support("What was the GPU purchase price in USD?","CUDA runtime version 12.4.")["allowed"])

    def test_currency_duration_and_rate_quotes_are_real_source_spans(self):
        for query,body in (("月度预算多少元？","任务预算为一百二十元。"),
                           ("How much did this cost?","Approved cost: $50."),
                           ("访问记录保留多久？","访问审计日志保留14天。"),
                           ("What percentage was measured?","Observed retry rate: 0.8%.")):
            result = field_support(query,body)
            self.assertTrue(result["allowed"],result)
            for check in result["checks"]:
                self.assertEqual(body[check["start"]:check["end"]],check["quote"])

    def test_related_but_missing_rate_hash_actor_and_year_are_rejected(self):
        for query,body in (("实际重复扣款率是多少？","通过订单幂等键避免重复扣款。"),
                           ("实际SHA256校验值是什么？","下载后核对SHA256。"),
                           ("是谁批准这笔预算？","预算为120元。"),
                           ("2025年重试率是多少？","2026年实测重试率0.8%。")):
            self.assertFalse(field_support(query,body)["allowed"],query)

    def test_planned_value_does_not_prove_an_actual_measurement(self):
        self.assertFalse(field_support("实际重试率是多少？","计划把重试率降至0.8%。")["allowed"])
        self.assertTrue(field_support("实际重试率是多少？","实测重试率为0.8%。")["allowed"])

    def test_secret_lookup_and_unknown_field_status(self):
        self.assertFalse(field_support("管理员密码是什么？","构建使用Node20。")["allowed"])
        result = field_support("以前怎么处理登录回跳？","Cookie使用SameSite=Lax。")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"],"relevance_only_unverified")

    def test_relevance_gate_ignores_unsupported_candidates_and_rejects_nan(self):
        support = {"a":{"allowed":False},"b":{"allowed":True}}
        self.assertEqual(select_verified([("a",.99),("b",.9)],support,.8,0),["b"])
        self.assertFalse(select_verified([("a",.99),("b",.7)],support,.8,0))
        with self.assertRaises(ValueError):
            select_verified([("b",float("nan"))],support,.8,0)

    def test_price_cache_lifetime_is_a_duration_not_a_purchase_price(self):
        result = field_support("What is the latest lifetime for stored product prices?","产品价格缓存保留300秒。")
        self.assertTrue(result["allowed"])
        self.assertEqual([item["field"] for item in result["checks"]],["duration"])
        self.assertEqual(field_support("多少钱？","版本4.2。")["status"],"requested_fields_missing")

    def test_existing_supported_candidate_survives_low_pair_score(self):
        support = {"old":{"allowed":True},"new":{"allowed":True}}
        self.assertEqual(choose_with_preservation(["old"],[("new",.9),("old",.1)],support,.8,0),["old"])
        self.assertEqual(choose_with_preservation(["old","new"],[("new",.9),("old",.1)],support,.8,0),["old","new"])
        support["old"]["allowed"] = False
        self.assertEqual(choose_with_preservation(["old"],[("new",.9),("old",.1)],support,.8,0),["new"])
        self.assertFalse(choose_with_preservation(["old"],[("new",.2),("old",.1)],support,.8,0))
        support["old"]["allowed"] = True
        with self.assertRaises(ValueError):
            choose_with_preservation(["old"],[("new",.2)],support,float("nan"),0)


if __name__ == "__main__":
    unittest.main()
