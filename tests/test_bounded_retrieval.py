"""Independent controls for query-only recovery, noise, scope and read integrity."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from validate_hfa import hfa,invoke

class BoundedRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="hfa-bounded-retrieval-")
        self.mem=Path(self.temp.name)/"memory";hfa.cmd_init(self.mem,quiet=True,profile="example")
    def tearDown(self):self.temp.cleanup()
    def add(self,title,body,project=None):
        capture=__import__('io').StringIO()
        with __import__('contextlib').redirect_stdout(capture):
            hfa.cmd_add_topic(self.mem,title,"opaque-ticket",body,project=project,as_json=True)
        return json.loads(capture.getvalue())["id"]
    def route(self,query,unlock=None):
        rc,out=invoke(hfa.cmd_route,self.mem,query,None,unlock)
        self.assertEqual(rc,0,out);return json.loads(out)
    def test_compact_unicode_lookup_without_extra_tags(self):
        for title,body,query in [
            ("现场甲","阈值设为23。","阈值"),
            ("现场乙","항목을갱신했습니다","항목"),
            ("现场丙","บันทึกคำสั่ง","คำสั่ง"),
            ("现场丁","المخزن مغلق","المخزن"),
            ("现场戊","хранилище доступно","хранилище"),
        ]:
            key=self.add(title,body)
            self.assertIn(key,[r["id"] for r in self.route(query)["selected"]],query)
    def test_literal_does_not_match_across_unicode_word_boundaries(self):
        self.add("Accent note","complété is not the requested standalone word.")
        self.assertFalse(self.route("été")["selected"])
        self.add("Product note","Only products are documented.")
        self.assertFalse(self.route("prod")["selected"])
    def test_common_short_literal_abstains_instead_of_flooding(self):
        for i in range(4):self.add("现场"+str(i),"阈值记录："+str(i))
        self.assertFalse(self.route("阈值")["selected"])
    def test_plural_forms_recover_new_spelling_without_changing_truth(self):
        first=self.add("Policy note","A policy limits the batch.")
        second=self.add("Migration note","Migrations run before startup.")
        self.assertIn(first,[r["id"] for r in self.route("policies")["selected"]])
        self.assertIn(second,[r["id"] for r in self.route("migration")["selected"]])
        self.assertEqual(hfa._query_spelling_variant("request_ids parser.py",{"request_id","parser"}),"request_ids parser.py")
    def test_compound_query_can_find_separate_evidence(self):
        a=self.add("Ledger note","Retention lasts nine days.")
        b=self.add("Cipher note","Cipher is ChaCha20.")
        self.add("UI note","The layout choice is orange.")
        self.add("Detail note","Details about a theatre audition.")
        p=self.mem/"config.json";v=json.loads(p.read_text(encoding="utf8"));v["max_recall_topics"]=5;p.write_text(json.dumps(v),encoding="utf8");invoke(hfa.cmd_reindex,self.mem)
        result=self.route("retention details and cipher choice")
        self.assertEqual({r["id"] for r in result["selected"]},{a,b})
        self.assertLessEqual(result["context_tokens"]["count"],result["context_token_budget"])
    def test_one_related_clause_and_repeated_clues_do_not_widen(self):
        self.add("Ledger note","Retention lasts nine days.")
        self.add("Backup note","Daily database backup is enabled.")
        for query in ("retention details and unrelated theatre", "backup and singer audition",
                      "加载 加载 loading singer audition", "不要查阈值"):
            self.assertFalse(self.route(query)["selected"],query)
    def test_literal_stays_inside_scope_and_ignores_superseded_fact(self):
        key=self.add("分支记录","항목을갱신했습니다",project="S1")
        self.assertFalse(self.route("항목")["selected"])
        self.assertIn(key,[r["id"] for r in self.route("항목","explicitly inspect this branch")["selected"]])
        old=self.add("旧数值","阈值是23。")
        invoke(hfa.cmd_add_topic,self.mem,"新数值","fresh-ticket","现行限制为11。",None,None,"evidence:new",[old],"new-version",True)
        self.assertNotIn(old,[r["id"] for r in self.route("阈值")["selected"]])
    def test_fts_fallback_agrees_and_query_does_not_migrate_indexes(self):
        self.add("Ledger note","Retention lasts nine days.")
        self.add("Cipher note","Cipher is ChaCha20.")
        protected=[self.mem/"config.json",self.mem/"directory.md",self.mem/"summary.md",*self.mem.glob("chunks/*"),*self.mem.glob("index/*")]
        before={p:p.read_bytes() for p in protected if p.is_file()}
        normal=self.route("retention details and cipher choice")
        with patch.object(hfa,"_fts_search",return_value=(None,None)):
            fallback=self.route("retention details and cipher choice")
        self.assertEqual(normal["selected"],fallback["selected"])
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertEqual(invoke(hfa.cmd_status,self.mem,True)[0],0)

if __name__=="__main__":unittest.main()
