"""Optional real tokenizer checks; counts refer to the encoding, not host billing."""
import importlib.util
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from validate_hfa import hfa,invoke

AVAILABLE=importlib.util.find_spec('tiktoken') is not None


@unittest.skipUnless(AVAILABLE,'optional tiktoken dependency unavailable')
class TokenizerBudgetChecks(unittest.TestCase):
    def test_prepared_encoding_works_offline_and_matches_explicit_o200k_count(self):
        import tiktoken
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network not allowed')):
            encoder=tiktoken.get_encoding('o200k_base')
            with patch.object(hfa,'_encoder',return_value=encoder):
                text='中文计量 <|endoftext|> and evidence pointers.'
                measured=hfa.measure_tokens(text)
                self.assertEqual(measured['count'],len(encoder.encode(text,disallowed_special=())))
                self.assertEqual(measured['method'],'o200k_base')
                self.assertTrue(measured['exact'])

    def test_same_budget_can_fit_two_records_and_single_fact_limit_retains_only_top(self):
        import tiktoken
        encoder=tiktoken.get_encoding('o200k_base')
        with tempfile.TemporaryDirectory(prefix='hfa-tokenizer-budget-') as temp:
            root=Path(temp)/'memory';invoke(hfa.cmd_init,root)
            body='中文显示方框时安装缺失的字体包，核对依赖再重启。'*4
            invoke(hfa.cmd_add_topic,root,'first','meter-fixture','FACT_A '+body)
            invoke(hfa.cmd_add_topic,root,'second','meter-fixture','FACT_B '+body)
            def recall(**kw):return hfa.cmd_route(root,'meter-fixture',return_result=True,**kw)
            with patch.object(hfa,'_encoder',return_value=None):byte_based=recall()
            with patch.object(hfa,'_encoder',return_value=encoder):
                token_based=recall();narrow=recall(max_topics=1)
            self.assertEqual(len(byte_based['selected']),1)
            self.assertEqual(len(token_based['selected']),2)
            self.assertEqual(len(narrow['selected']),1)
            self.assertEqual(narrow['selected'][0]['id'],token_based['selected'][0]['id'])
            self.assertEqual(byte_based['recall_token_budget'],token_based['recall_token_budget'])
            self.assertEqual(token_based['summary_text'],narrow['summary_text'])
            self.assertLessEqual(token_based['recall_tokens']['count'],token_based['recall_token_budget'])


if __name__=='__main__':unittest.main()
