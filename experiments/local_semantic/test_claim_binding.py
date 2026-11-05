from datetime import date
import unittest
from claim_binding import binding_support


class ClaimBindingChecks(unittest.TestCase):
    def test_wrong_product_cannot_borrow_another_products_amount(self):
        self.assertFalse(binding_support('Mercury预算多少元？','Venus预算 60 元。')['allowed'])

    def test_wrong_rate_and_wrong_year_cannot_be_combined_across_clauses(self):
        body='2023年 Mercury 成功率 95%；2024年 Mercury 重试率 5%。'
        self.assertFalse(binding_support('2024年 Mercury 成功率是多少？',body)['allowed'])

    def test_exact_quote_comes_from_the_matching_clause_not_first_number(self):
        body='2023年 Mercury 成功率 95%；2024年 Mercury 成功率 98%。'
        r=binding_support('2024年 Mercury 成功率是多少？',body)
        self.assertTrue(r['allowed'])
        self.assertEqual(r['binding']['evidence'][0]['quote'],'98%')
        for check in r['checks']:
            if check['present']:
                self.assertEqual(body[check['start']:check['end']],check['quote'])

    def test_an_explicit_subject_without_number_is_not_enough(self):
        self.assertFalse(binding_support('Mercury预算多少元？','Mercury讨论预算。Venus预算60元。')['allowed'])

    def test_missing_names_and_cross_language_aliases_are_not_invented(self):
        self.assertFalse(binding_support('Mercury预算多少元？','水星预算60元。')['allowed'])

    def test_multiple_objects_or_metrics_with_ambiguous_values_are_rejected(self):
        self.assertFalse(binding_support('Mercury预算多少元？','Mercury and Venus budgets are 60元 and 80元 respectively.昨日报告。')['allowed'])
        self.assertFalse(binding_support('Mercury 成功率是多少？','Mercury success rate and retry rate are 95% and 5%.')['allowed'])

    def test_negated_and_target_clauses_cannot_prove_observations(self):
        self.assertFalse(binding_support('Mercury实际成功率是多少？','Mercury计划成功率99%；Venus实测成功率98%。')['allowed'])
        self.assertFalse(binding_support('Mercury成功率是多少？','Mercury 未记录成功率，Venus成功率98%。')['allowed'])

    def test_relative_time_requires_a_supplied_reference_date(self):
        body='2027年 Mercury 实测成功率98%。'
        self.assertFalse(binding_support('去年 Mercury 成功率是多少？',body)['allowed'])
        self.assertTrue(binding_support('去年 Mercury 成功率是多少？',body,date(2028,1,2))['allowed'])

    def test_month_and_year_must_belong_to_same_value_clause(self):
        body='2023年9月 Mercury 重试率5%；2024年8月 Mercury 重试率2%。'
        self.assertFalse(binding_support('2024年9月 Mercury 重试率是多少？',body)['allowed'])

    def test_decimal_values_are_not_split_at_the_decimal_point(self):
        r=binding_support('What percentage is the Mercury failure rate?','Mercury failure rate is 1.25%.')
        self.assertTrue(r['allowed'])
        self.assertEqual(r['binding']['evidence'][0]['quote'],'1.25%')

    def test_replaced_duration_cites_the_new_value(self):
        r=binding_support('Mercury缓存有效期多久？','Mercury缓存有效期从60秒改为300秒。')
        self.assertTrue(r['allowed'])
        self.assertEqual(r['binding']['evidence'][0]['quote'],'300秒')

    def test_non_numeric_requests_keep_existing_retrieval_behavior_without_claiming_binding(self):
        r=binding_support('以前怎么解决登录回跳？','Cookie设置SameSite=Lax。')
        self.assertTrue(r['allowed'])
        self.assertEqual(r['binding']['coverage'],'not_checked_non_numeric_request')

    def test_secrets_and_invalid_input_do_not_bypass_existing_gates(self):
        self.assertFalse(binding_support('管理员密码是什么？','密码未记录。')['allowed'])
        with self.assertRaises(TypeError):binding_support(None,'文本')

    def test_past_tense_metric_question_still_requires_a_numeric_claim(self):
        r=binding_support('What was the Mercury failure rate?','Mercury failure rate: 1.25%.')
        self.assertTrue(r['allowed'])
        self.assertEqual(r['binding']['evidence'][0]['quote'],'1.25%')
        self.assertFalse(binding_support('What was the Mercury failure rate?','Mercury success rate: 98.75%.')['allowed'])

    def test_sentence_opening_preposition_is_not_a_product_name(self):
        r=binding_support('Before how many days must the audit entries be removed?','审计日志保留10天。')
        self.assertTrue(r['allowed'])
        self.assertEqual(r['binding']['requested']['entities'],[])

    def test_cache_lifetime_can_answer_generic_retention_but_not_the_reverse(self):
        self.assertTrue(binding_support('Mercury副本保留多久？','Mercury缓存有效期10秒。')['allowed'])
        self.assertFalse(binding_support('Mercury缓存有效期多久？','Mercury日志保留10天。')['allowed'])

    def test_post_filter_can_only_remove_scoped_candidates(self):
        from claim_binding import filter_bound_candidates
        docs={'wrong':{'body':'Venus预算60元。'},'right':{'body':'Mercury预算80元。'}}
        r=filter_bound_candidates('Mercury预算多少元？',['wrong','right'],docs)
        self.assertEqual(r['selected'],['right'])
        with self.assertRaises(ValueError):filter_bound_candidates('问题',['wrong','wrong'],docs)
        with self.assertRaises(ValueError):filter_bound_candidates('问题',['a','b','c'],docs)

    def test_duration_about_a_price_cache_does_not_request_currency(self):
        r=binding_support('What is the Mercury price-cache lifetime?','Mercury缓存有效期10秒。')
        self.assertTrue(r['allowed'])
        self.assertEqual(list(r['binding']['requested']['metrics']),['duration'])

    def test_monthly_budget_cannot_borrow_an_annual_number(self):
        self.assertFalse(binding_support('Mercury每月预算多少元？','Mercury年度预算1200元。')['allowed'])
        self.assertTrue(binding_support('Mercury每月预算多少元？','Mercury月度预算100元。')['allowed'])

    def test_literal_names_are_case_insensitive_and_need_no_source_quotes(self):
        self.assertTrue(binding_support('Mercury预算多少元？','mercury预算100元。')['allowed'])
        self.assertTrue(binding_support('“水星”预算多少元？','水星预算100元。')['allowed'])
        with self.assertRaises(TypeError):binding_support('去年预算多少元？','预算100元。','2026-01-01')

    def test_explicitly_negated_number_is_not_positive_evidence(self):
        self.assertFalse(binding_support('Mercury成功率是多少？','Mercury成功率不是98%。')['allowed'])
        self.assertFalse(binding_support('What is the Mercury success rate?','Mercury success rate is not 98%.')['allowed'])

    def test_generic_order_proportion_is_not_assumed_to_mean_retries(self):
        from claim_binding import metric_names
        self.assertEqual(metric_names('订单处理完成的比例是多少？','percentage'),set())
        self.assertEqual(metric_names('请求中有多大比例需要再次提交？','percentage'),{'retry_rate'})


if __name__=='__main__':unittest.main()
