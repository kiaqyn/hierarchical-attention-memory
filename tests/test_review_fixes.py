"""Fixes from the independent review of the recall and hook follow-ups."""
from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from validate_hfa import ROOT, hfa

spec = importlib.util.spec_from_file_location("hfa_hook_review", ROOT / "scripts/hfa_hook.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hfa-review-")
        self.root = Path(self.tmp.name) / "memory"

    def tearDown(self):
        self.tmp.cleanup()

    def test_readonly_uri_encodes_special_path_characters(self):
        names = ["space dir", "hash#dir", "pct%dir"]
        if os.name != "nt":
            names.append("q?mark")
        for name in names:
            root = Path(self.tmp.name) / name / "memory"
            hfa.cmd_init(root, quiet=True)
            db = root / "index/fts.sqlite"
            before = {p.name for p in db.parent.iterdir()}
            conn = hfa._open_readonly_sqlite(db)
            try:
                listed = conn.execute("PRAGMA database_list").fetchone()[2]
                self.assertIn(name, listed.replace("%23", "#").replace("%3F", "?").replace("%25", "%").replace("%20", " "))
                row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                self.assertEqual(row[0], "1")
                with self.assertRaises(sqlite3.Error):
                    conn.execute("UPDATE meta SET value='rw' WHERE key='version'")
            finally:
                conn.close()
            self.assertEqual(before, {p.name for p in db.parent.iterdir()})
        missing = self.root / "missing.sqlite"
        with self.assertRaises(hfa.MemoryError):
            hfa._open_readonly_sqlite(missing)
        self.assertFalse(missing.exists())

    def test_init_holds_the_lock_until_the_index_exists(self):
        events = []
        target = self.root

        def note(kind):
            events.append((kind, (target / "index/search.json").is_file()))

        if os.name == "nt":
            import msvcrt
            real = msvcrt.locking

            def spy(fd, mode, length):
                note("unlock" if mode == msvcrt.LK_UNLCK else "lock")
                return real(fd, mode, length)

            patcher = patch("msvcrt.locking", spy)
        else:
            import fcntl
            real = fcntl.flock

            def spy(fd, op):
                if op & fcntl.LOCK_UN:
                    note("unlock")
                elif op & fcntl.LOCK_EX:
                    note("lock")
                return real(fd, op)

            patcher = patch("fcntl.flock", spy)
        with patcher:
            hfa.cmd_init(target, quiet=True)
        unlocks = [ready for kind, ready in events if kind == "unlock"]
        self.assertTrue(unlocks, events)
        self.assertTrue(all(unlocks), events)
        self.assertEqual(sum(kind == "lock" for kind, _ready in events), 1)
        recalled = hfa.cmd_route(target, "主线", return_result=True)
        self.assertIn("context_text", recalled)

    def test_auto_phase_ignores_leftover_response_fields(self):
        leftover = {"lastUserInput": "下一轮问题", "response": "上一轮已经回答过"}
        self.assertEqual(hook._detect_phase("auto", leftover), "pre")
        self.assertEqual(hook._detect_phase("auto", {"output": "still talking", "modelOutput": "partial"}), "pre")
        self.assertEqual(hook._detect_phase("auto", {"lastAssistantResponse": "本轮结束"}), "post")
        self.assertEqual(hook._detect_phase("auto", {"terminationReason": "turn_end"}), "stop")

    def test_fence_allows_info_token_and_same_line_closer(self):
        body = {"title": "Fence title", "conclusion": "Saved from a fence.", "hit_words": ["fence-key"]}
        payloads = [
            "```hfa-memory json\n" + json.dumps(body) + "\n```",
            "```hfa-memory\n" + json.dumps(body) + "```",
            "```hfa json\n" + json.dumps(body) + "\n```",
        ]
        for text in payloads:
            decision = hook._extract_turn_decision({"lastAssistantResponse": text}, "post")
            self.assertIsNotNone(decision, text)
            self.assertEqual(decision["title"], "Fence title")
            self.assertIn("fence-key", decision["hit_words"])

    def test_journal_copies_sqlite_aside_instead_of_base64(self):
        hfa.cmd_init(self.root, quiet=True)
        seen = []
        real = hfa._atomic_write

        def spy(path, data):
            if Path(path).name == hfa.JOURNAL and b'"prepared"' in data:
                seen.append(data)
            return real(path, data)

        with patch.object(hfa, "_atomic_write", spy):
            with contextlib.redirect_stdout(io.StringIO()):
                hfa.cmd_add_topic(self.root, "Journal note", "journal-key", "The sqlite file stays out of the journal.", as_json=True)
        self.assertTrue(seen)
        state = json.loads(seen[0])
        fts = next(entry for entry in state["entries"] if entry["path"].endswith("fts.sqlite"))
        self.assertIsNone(fts["before"])
        self.assertTrue(str(fts["backup"]).startswith(".hfa-prior/"))
        self.assertNotIn("U1FMaXRl", seen[0].decode("utf-8"))
        self.assertFalse((self.root / ".hfa-prior").exists())

    def test_fts_build_uses_executemany(self):
        source = inspect.getsource(hfa._build_fts_cached)
        self.assertGreaterEqual(source.count("executemany"), 4)
        self.assertNotIn('conn.execute("INSERT INTO term_df', source)
        self.assertNotIn('conn.execute("INSERT INTO hitword', source)
        self.assertNotIn('conn.execute("INSERT INTO doc_term', source)
        self.assertNotIn('conn.execute("INSERT INTO chunk_meta', source)
        self.assertNotIn('conn.execute(\n                    "INSERT INTO chunks_fts', source)
        hfa.cmd_init(self.root, quiet=True)
        with contextlib.redirect_stdout(io.StringIO()):
            hfa.cmd_add_topic(self.root, "Bulk note", "bulk-key", "Rows are inserted together.", as_json=True)
        recalled = hfa.cmd_route(self.root, "bulk-key", return_result=True)
        self.assertEqual(recalled["selected"][0]["id"], "chunk_0001")

    def test_hook_timeout_is_configurable(self):
        self.assertEqual(hook._hook_timeout(), 120.0)
        with patch.dict(os.environ, {"HFA_HOOK_TIMEOUT": "33"}):
            self.assertEqual(hook._hook_timeout(), 33.0)
            fake = type("Proc", (), {"returncode": 0, "stdout": '{"id":"chunk_0001"}', "stderr": ""})()
            decision = {"title": "T", "hit_words": ["k"], "conclusion": "c", "source": "s", "request_id": "r"}
            with patch.object(hook.subprocess, "run", return_value=fake) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    hook._handle_post_or_stop(self.root, decision, phase="post", payload={})
            self.assertEqual(run.call_args.kwargs["timeout"], 33.0)
        with patch.dict(os.environ, {"HFA_HOOK_TIMEOUT": "0"}):
            self.assertEqual(hook._hook_timeout(), 1.0)

    def test_status_skips_rebuild_until_mismatch_or_full_verify(self):
        hfa.cmd_init(self.root, quiet=True)
        with patch.object(hfa, "_derived", side_effect=AssertionError("rebuilt")):
            state = hfa.cmd_status(self.root, return_result=True)
        self.assertEqual(state["stale_indexes"], [])
        with patch.object(hfa, "_derived", wraps=hfa._derived) as derived:
            hfa.cmd_status(self.root, return_result=True, full_verify=True)
        self.assertGreaterEqual(derived.call_count, 1)

    def test_fast_digest_rejects_sqlite_meta_mismatch(self):
        hfa.cmd_init(self.root, quiet=True)
        with contextlib.redirect_stdout(io.StringIO()):
            hfa.cmd_add_topic(self.root, "Digest note", "digest-key", "The stored digest must match the manifest.", as_json=True)
        db = self.root / "index/fts.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE meta SET value='deadbeef' WHERE key='source_digest'")
        conn.commit()
        conn.close()
        manifest_path = self.root / "index/source-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stat = db.stat()
        manifest["derived"]["index/fts.sqlite"]["size"] = stat.st_size
        manifest["derived"]["index/fts.sqlite"]["mtime_ns"] = stat.st_mtime_ns
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIsNone(hfa._fast_source_digest(self.root))
        with patch.object(hfa, "_derived", wraps=hfa._derived) as derived:
            state = hfa.cmd_status(self.root, return_result=True)
        self.assertGreaterEqual(derived.call_count, 1)
        self.assertTrue(state["stale_indexes"])

    def test_missing_fts_uses_term_df_and_fails_closed_without_it(self):
        hfa.cmd_init(self.root, quiet=True)
        with contextlib.redirect_stdout(io.StringIO()):
            hfa.cmd_add_topic(self.root, "Ledger note", "ledger-ticket", "Retention lasts nine days.", as_json=True)
        with patch.object(hfa, "_fts5_supported", return_value=False):
            hfa.cmd_reindex(self.root, quiet=True)
            with patch.object(hfa._SqlDocs, "items", side_effect=AssertionError("full scan")):
                result = hfa.cmd_route(self.root, "retention lasts nine days", return_result=True, index_source="sqlite")
        self.assertEqual(result["selected"][0]["id"], "chunk_0001")
        db = self.root / "index/fts.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE term_df")
        conn.execute("DROP TABLE doc_term")
        conn.commit()
        conn.close()
        manifest_path = self.root / "index/source-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stat = db.stat()
        manifest["derived"]["index/fts.sqlite"]["size"] = stat.st_size
        manifest["derived"]["index/fts.sqlite"]["mtime_ns"] = stat.st_mtime_ns
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(hfa._SqlDocs, "items", side_effect=AssertionError("full scan")):
            with self.assertRaises(hfa.MemoryError) as caught:
                hfa.cmd_route(self.root, "retention lasts nine days", return_result=True, index_source="sqlite")
        self.assertIn("term_df", str(caught.exception))

    def test_templates_do_not_follow_a_site_packages_shadow(self):
        parent = Path(self.tmp.name) / "site-packages"
        package = parent / "scripts"
        stray = parent / "templates" / "minimal"
        stray.mkdir(parents=True)
        (stray / "config.json").write_text("stray", encoding="utf-8")
        packaged = package / "templates" / "minimal"
        packaged.mkdir(parents=True)
        (packaged / "config.json").write_text("packaged", encoding="utf-8")
        self.assertEqual(hfa._resolve_templates(package, parent), package / "templates")
        empty = parent / "otherpkg"
        empty.mkdir()
        resolved = hfa._resolve_templates(empty, parent)
        self.assertEqual(resolved, empty / "templates")
        self.assertFalse((parent / "templates") == resolved)
        checkout = Path(self.tmp.name) / "checkout"
        (checkout / "templates" / "minimal").mkdir(parents=True)
        scripts = checkout / "scripts"
        scripts.mkdir()
        self.assertEqual(hfa._resolve_templates(scripts, checkout), checkout / "templates")


if __name__ == "__main__":
    unittest.main()
