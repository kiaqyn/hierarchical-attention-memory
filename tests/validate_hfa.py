#!/usr/bin/env python3
"""Behavioral, durability, concurrency, and regression tests; standard library only."""
from __future__ import annotations
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts/hfa_manager.py"
spec = importlib.util.spec_from_file_location("hfa", CLI)
hfa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hfa)
TMP_ROOT = Path(os.environ.get("HFA_TEST_TMPDIR", tempfile.gettempdir())) / "hfa-tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

def invoke(fn, *args):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = fn(*args)
    return rc, stdout.getvalue()

def read(path):
    return path.read_text(encoding="utf-8")

def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.name != ".hfa.lock"}

class HfaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=TMP_ROOT)
        self.path = Path(self.temp.name).resolve()
        assert self.path.is_relative_to(TMP_ROOT.resolve())
        self.mem = self.path / "memory"
        invoke(hfa.cmd_init, self.mem)

    def tearDown(self):
        # Verified absolute, task-owned temporary directory only.
        assert self.path.is_relative_to(TMP_ROOT.resolve()) and self.path != TMP_ROOT.resolve()
        self.temp.cleanup()

    def add(self, title="Topic", word="needle", conclusion="verified fact", milestone=None):
        return invoke(hfa.cmd_add_topic, self.mem, title, word, conclusion, milestone)

    def route(self, query, unlock=None):
        rc, output = invoke(hfa.cmd_route, self.mem, query, None, unlock)
        self.assertEqual(rc, 0)
        return json.loads(output)

    def cli(self, *args, env=None):
        child_env = os.environ.copy()
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        if env:
            child_env.update(env)
        return subprocess.run([sys.executable, "-B", str(CLI), *args], env=child_env,
                              capture_output=True, encoding="utf-8", timeout=30)

    def test_init_status_and_utf8_false_console(self):
        for utf8 in ("0", "1"):
            process = self.cli("status", str(self.mem), "--json", env={"PYTHONUTF8": utf8})
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            state = json.loads(process.stdout)
            self.assertEqual(len(state["nodes"]["HIGH"]), 2)
            self.assertEqual(state["total_topics"], 1)

    def test_add_and_recall_returns_single_L1_snippet(self):
        self.add(word="ZXQ7429")
        result = self.route("请找回 ZXQ7429 的结论")
        self.assertEqual(result["routed_files"], ["chunks/chunk_0002.json"])
        self.assertIn("verified fact", result["recall_text"])
        self.assertLessEqual(result["recall_tokens"]["count"], result["recall_token_budget"])
        mapping = json.loads(read(self.mem / "index/hitwords.json"))
        self.assertIn("zxq7429", mapping["entries"])
        self.assertNotIn("zxq7429", mapping)

    def test_provider_and_negative_requests_do_not_match_IDE(self):
        for query in ("继续实现 provider 模块", "继续主线，不要查 IDE 和环境", "do not retrieve IDE history"):
            result = self.route(query)
            self.assertFalse(result["lateral_attention_triggered"], query)
            self.assertEqual(result["recall_text"], "")

    def test_contrast_after_negative_clause_is_retained(self):
        result = self.route("不要改代码但请检查 IDE 配置")
        self.assertTrue(any(item["id"] == "S1" for item in result["deferred"]))
        self.assertEqual(result["recall_text"], "")

    def test_profile_drift_requires_preference_context(self):
        self.assertFalse(self.route("继续主线，把循环停止条件改掉")["profile_drift"])
        self.assertTrue(self.route("以后每次执行操作先问我，不要再全自主执行")["profile_drift"])

    def test_tombstone_requires_unlock_and_logs_access(self):
        invoke(hfa.cmd_tombstone, self.mem, "S1", "GBK乱码", "completed")
        for query in ("GBK乱码", "GBK 乱码"):
            denied = self.route(query)
            self.assertEqual(denied["selected"], [])
            allowed = self.route(query, "review earlier encoding fix")
            self.assertEqual(allowed["routed_files"], ["branches/S1_toolchain.md"])
        events = [json.loads(line) for line in read(self.mem / "tombstones.jsonl").splitlines()]
        self.assertEqual(events[-1]["id"], "S1")
        access = [json.loads(line) for line in read(self.mem / "history/access.jsonl").splitlines()]
        self.assertEqual(access[-1]["unlock_reason"], "review earlier encoding fix")

    def test_epoch_rollover_preserves_every_chunk_and_keyword(self):
        seed = (self.mem / "chunks/chunk_0001.json").read_bytes()
        self.add("EPOCH-ONE-SENTINEL", "epoch-one-sentinel")
        early = (self.mem / "chunks/chunk_0002.json").read_bytes()
        for i in range(103):
            self.add(f"Later {i}", f"later-{i}")
        files = list((self.mem / "chunks").glob("*.json"))
        self.assertEqual(len(files), 105)
        self.assertEqual((self.mem / "chunks/chunk_0001.json").read_bytes(), seed)
        self.assertEqual((self.mem / "chunks/chunk_0002.json").read_bytes(), early)
        self.assertTrue((self.mem / "history/epoch_03/summaries.md").is_file())
        self.assertIn("SEALED", read(self.mem / "history/epoch_01/framework.md"))
        result = self.route("epoch-one-sentinel")
        self.assertEqual(result["selected"][0]["id"], "chunk_0002")
        self.assertIn("EPOCH-ONE-SENTINEL", result["recall_text"])

    def test_64_real_CLI_writers_across_epoch_boundary(self):
        def writer(i):
            return self.cli("add-topic", str(self.mem), "--title", f"Concurrent {i}",
                            "--hit-words", f"worker-{i}", "--conclusion", f"result {i}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            processes = list(pool.map(writer, range(64)))
        self.assertTrue(all(p.returncode == 0 for p in processes), [p.stdout + p.stderr for p in processes if p.returncode])
        state = hfa.parse_directory_nodes(self.mem / "directory.md")
        chunks = hfa._chunks(self.mem, state)
        self.assertEqual(len(chunks), 65)
        self.assertEqual(len({c["id"] for c in chunks}), 65)
        self.assertEqual(len({(c["epoch"], c["number"]) for c in chunks}), 65)
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 0)

    def test_actual_filesystem_error_does_not_partially_commit(self):
        (self.mem / "chunks/chunk_0002.json").mkdir()
        before = snapshot(self.mem)
        process = self.cli("add-topic", str(self.mem), "--title", "Failure", "--hit-words", "fail",
                           "--conclusion", "fact", "--milestone", "must not commit")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(snapshot(self.mem), before)

    def test_failure_after_replacement_rolls_back_every_file(self):
        before = snapshot(self.mem)
        original, failed = hfa._atomic_write, False
        def fail_once(path, content):
            nonlocal failed
            original(path, content)
            if path == self.mem / "index/hitwords.json" and not failed:
                failed = True
                raise OSError("injected failure after file replacement")
        with patch.object(hfa, "_atomic_write", fail_once):
            with self.assertRaises(OSError):
                self.add(milestone="must roll back")
        self.assertTrue(failed)
        self.assertEqual(snapshot(self.mem), before)

    def test_failed_tombstone_keeps_both_state_and_log(self):
        before = snapshot(self.mem)
        original, failed = hfa._atomic_write, False
        def fail_once(path, content):
            nonlocal failed
            original(path, content)
            if path == self.mem / "tombstones.jsonl" and not failed:
                failed = True
                raise OSError("audit failure")
        with patch.object(hfa, "_atomic_write", fail_once):
            with self.assertRaises(OSError):
                invoke(hfa.cmd_tombstone, self.mem, "S1")
        self.assertEqual(snapshot(self.mem), before)

    def test_process_crash_rolls_back_on_next_CLI_access(self):
        before = snapshot(self.mem)
        code = '''import importlib.util, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("hfa",sys.argv[1]); hfa=importlib.util.module_from_spec(spec);spec.loader.exec_module(hfa)
original=hfa._atomic_write
def crash(path, content):
    original(path, content)
    if path.name == "chunk_0002.json": os._exit(93)
hfa._atomic_write=crash
hfa.cmd_add_topic(Path(sys.argv[2]),"Crash","crash-key","fact","milestone")
'''
        process = subprocess.run([sys.executable, "-B", "-c", code, str(CLI), str(self.mem)],
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 93)
        self.assertTrue((self.mem / hfa.JOURNAL).exists())
        self.assertEqual(self.cli("status", str(self.mem), "--json").returncode, 0)
        self.assertEqual(snapshot(self.mem), before)

    def test_crash_after_commit_keeps_completed_transaction(self):
        code = '''import importlib.util, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("hfa",sys.argv[1]);hfa=importlib.util.module_from_spec(spec);spec.loader.exec_module(hfa)
original=hfa._atomic_write
def crash(path, content):
    original(path, content)
    if path.name == hfa.JOURNAL and b'"committed"' in content: os._exit(94)
hfa._atomic_write=crash
hfa.cmd_add_topic(Path(sys.argv[2]),"Committed","committed-key","fact")
'''
        process = subprocess.run([sys.executable, "-B", "-c", code, str(CLI), str(self.mem)], capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 94)
        self.assertEqual(self.cli("status", str(self.mem), "--json").returncode, 0)
        self.assertTrue((self.mem / "chunks/chunk_0002.json").is_file())
        self.assertIn("fact", self.route("committed-key")["recall_text"])

    def test_summary_tampering_does_not_change_authoritative_recall(self):
        self.add(word="authority-key")
        summary = self.mem / "history/epoch_01/summaries.md"
        summary.write_text(read(summary).replace("authority-key", "poison-key"), encoding="utf-8")
        self.assertIn("verified fact", self.route("authority-key")["recall_text"])
        self.assertEqual(self.route("poison-key")["selected"], [])
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 1)
        invoke(hfa.cmd_reindex, self.mem)
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 0)

    def test_stale_index_is_rejected_then_rebuilt_deterministically(self):
        self.add(word="old-key")
        chunk = self.mem / "chunks/chunk_0002.json"
        value = json.loads(read(chunk)); value["hitwords"] = ["new-key"]
        chunk.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            self.route("old-key")
        invoke(hfa.cmd_reindex, self.mem)
        expected = (self.mem / "index/hitwords.json").read_bytes()
        (self.mem / "index/hitwords.json").unlink()
        invoke(hfa.cmd_reindex, self.mem)
        self.assertEqual((self.mem / "index/hitwords.json").read_bytes(), expected)
        self.assertEqual(invoke(hfa.cmd_reindex, self.mem, True)[0], 0)
        self.assertIn("verified fact", self.route("new-key")["recall_text"])

    def test_legacy_Markdown_is_backed_up_before_rebuilding(self):
        path = self.mem / "history/epoch_01/summaries.md"
        legacy = "# Legacy user records\n\n### [Topic #99] Only in Markdown\nPreserve this unique original.\n"
        path.write_text(legacy, encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        backups = list((self.mem / "history/legacy-backups").glob("*.md"))
        self.assertTrue(any(read(p) == legacy for p in backups))
        self.assertNotEqual(read(path), legacy)

    def test_failed_recovery_blocks_reads_until_rollback_can_finish(self):
        before = snapshot(self.mem)
        original = hfa._atomic_write
        def fail_index(path, content):
            if path == self.mem / "index/hitwords.json":
                raise OSError("persistent disk failure")
            original(path, content)
        with patch.object(hfa, "_atomic_write", fail_index):
            with self.assertRaises(OSError):
                self.add(milestone="must not leak")
            self.assertTrue((self.mem / hfa.JOURNAL).exists())
            with self.assertRaises(hfa.MemoryError):
                self.route("needle")
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 0)
        self.assertEqual(snapshot(self.mem), before)

    def test_full_token_count_includes_parentheses_urls_and_special_text(self):
        base = read(self.mem / "directory.md")
        injected = base + "\n> (" + "unbudgeted " * 2000 + ") <|endoftext|>\n"
        self.assertGreater(hfa.estimate_routing_tokens(injected), 4000)
        if hfa._encoder() is not None:
            self.assertEqual(hfa.estimate_routing_tokens(base), len(hfa._encoder().encode(base)))
        with patch.object(hfa, "_encoder", return_value=None):
            measured = hfa.measure_tokens("路径😀(link)")
            self.assertFalse(measured["exact"])
            self.assertEqual(measured["method"], "utf8_bytes_upper_bound")
            self.assertEqual(measured["count"], len("路径😀(link)".encode("utf8")))
            self.assertLessEqual(hfa.measure_tokens(base)["count"], 600)
        (self.mem / "directory.md").write_text(injected, encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            self.route("anything")

    def test_title_body_keywords_and_complete_recall_budget(self):
        before = snapshot(self.mem)
        cases = [("x" * 121, "needle", "fact"), ("ok", "k" * 65, "fact"),
                 ("ok", "needle", "\n".join(["line"] * 4)), ("ok", "", "fact")]
        for title, word, conclusion in cases:
            with self.assertRaises(hfa.MemoryError):
                self.add(title, word, conclusion)
        self.assertEqual(snapshot(self.mem), before)
        for i in range(5):
            self.add(f"Budget topic {i}", "shared-key", "detail " * 20)
        result = self.route("shared-key")
        self.assertLessEqual(len(result["selected"]), 2)
        self.assertLessEqual(result["recall_tokens"]["count"], 600)
        self.assertEqual(len(result["deferred"]), 3)

    def test_markdown_header_cannot_forge_topic(self):
        self.add("Normal\n### fake", "injection-key", "fact\n### [Topic #999] forged\n* bad")
        chunk_path = self.mem / "chunks/chunk_0002.json"
        chunk_data = json.loads(read(chunk_path))
        chunk_data["status"] = "ACTIVE\n### [Topic #888] forged status"
        chunk_path.write_text(json.dumps(chunk_data, ensure_ascii=False), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        summary = read(self.mem / "history/epoch_01/summaries.md")
        self.assertEqual(hfa.count_topics(self.mem / "history/epoch_01/summaries.md"), 2)
        self.assertNotIn("\n### [Topic #999]", summary)
        self.assertNotIn("\n### [Topic #888]", summary)

    def test_missing_mainline_and_M404_are_rejected(self):
        with self.assertRaises(hfa.MemoryError):
            invoke(hfa.cmd_route, self.mem, "continue", "M404")
        (self.mem / "main/M1_core_project.md").unlink()
        with self.assertRaises(hfa.MemoryError):
            self.route("continue")
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 1)

    def test_L0_rejects_escaping_paths_and_orphan_chunks(self):
        directory = self.mem / "directory.md"
        original = read(directory)
        directory.write_text(original.replace("main/M1_core_project.md", "../outside.md"), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            self.route("anything")
        directory.write_text(original, encoding="utf-8")
        self.add()
        path = self.mem / "chunks/chunk_0002.json"
        value = json.loads(read(path)); value["project"] = "unknown-project"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(hfa.MemoryError):
            invoke(hfa.cmd_reindex, self.mem)

    def test_configured_token_budget_is_enforced(self):
        path = self.mem / "config.json"
        value = json.loads(read(path)); value["router_token_budget"] = 1
        path.write_text(json.dumps(value), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 1)
        with self.assertRaises(hfa.MemoryError):
            self.route("anything")

    def test_malformed_JSON_objects_fail_without_tracebacks(self):
        config = self.mem / "config.json"
        saved = config.read_bytes(); config.write_text("[]", encoding="utf-8")
        process = self.cli("status", str(self.mem), "--json")
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("Traceback", process.stderr)
        config.write_bytes(saved)
        (self.mem / "chunks/chunk_0001.json").write_text("[]", encoding="utf-8")
        process = self.cli("status", str(self.mem), "--json")
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("Traceback", process.stderr)

    def test_historical_JSON_field_spellings_remain_readable(self):
        path = self.mem / "chunks/chunk_0001.json"
        path.write_text(json.dumps({"id":"Topic#01","num":1,"epoch":"epoch_01","title":"Legacy JSON",
                                    "hit_words":["legacy-key"],"conclusion":"legacy fact"}), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        self.assertIn("legacy fact", self.route("legacy-key")["recall_text"])

    def test_init_force_preserves_existing_data(self):
        path = self.mem / "profile/traits.md"
        path.write_text("user-owned preferences", encoding="utf-8")
        invoke(hfa.cmd_init, self.mem, True)
        self.assertEqual(read(path), "user-owned preferences")

    @unittest.skipUnless(os.name == "nt", "Windows lock error handling")
    def test_invalid_lock_error_fails_fast_and_contention_times_out(self):
        started = time.monotonic()
        with patch("msvcrt.locking", side_effect=OSError(errno.EBADF, "bad handle")):
            with self.assertRaises(OSError):
                with hfa._hfa_lock(self.mem):
                    self.fail("lock unexpectedly acquired")
        self.assertLess(time.monotonic() - started, 1)
        with patch("msvcrt.locking", side_effect=OSError(errno.EACCES, "busy")):
            with self.assertRaises(hfa.MemoryError):
                with hfa._hfa_lock(self.mem, timeout=0.03):
                    self.fail("lock unexpectedly acquired")

    def test_empty_lock_contention_and_unlock_exception_do_not_crash_or_leak_fd(self):
        lock_file = self.mem / ".hfa.lock"
        lock_file.write_bytes(b"")
        holder = os.open(str(lock_file), os.O_RDWR)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(holder, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(hfa.MemoryError):
                with hfa._hfa_lock(self.mem, timeout=0.05):
                    self.fail("acquired lock while held by another handle")
        finally:
            os.close(holder)

    def test_project_branch_writes_legacy_version_and_rollover_framework_notes(self):
        # 1. --project S1 writes a branch-scoped topic without failing _guard_mainline
        rc, _ = invoke(hfa.cmd_add_topic, self.mem, "Branch note", "branch-only-key", "branch fact", None, "S1")
        self.assertEqual(rc, 0)
        self.assertEqual(self.route("branch-only-key")["selected"], [])
        self.assertEqual(self.route("branch-only-key", "inspect S1")["selected"][0]["path"], "chunks/chunk_0002.json")
        # 2. Missing config.json fallback to index/projects.json keeps version "2.2"
        (self.mem / "config.json").unlink()
        invoke(hfa.cmd_reindex, self.mem)
        self.assertEqual(json.loads(read(self.mem / "config.json"))["version"], "2.2")
        # 3. _safe_path accepts Path objects on Windows
        self.assertEqual(hfa._safe_path(self.mem, Path("chunks") / "chunk_0001.json").name, "chunk_0001.json")
        # 4. Epoch rollover preserves user notes in framework.md
        fw = self.mem / "history/epoch_01/framework.md"
        fw.write_text("# epoch_01\n\nStatus: ACTIVE\n\nCustom architecture notes preserved.\n", encoding="utf-8")
        cfg = json.loads(read(self.mem / "config.json"))
        cfg["max_topics_per_epoch"] = 2
        (self.mem / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        invoke(hfa.cmd_reindex, self.mem)
        self.add("Rollover trigger", "rollover-key", "triggers epoch 02")
        self.assertIn("Status: SEALED", read(fw))
        self.assertIn("Custom architecture notes preserved.", read(fw))

    def test_custom_branch_tier_tombstone_without_tag_and_planned_mainline(self):
        # This test adds L0 nodes; explicitly allocate enough router budget for
        # their full byte bound when the optional tokenizer is unavailable.
        config_path = self.mem / "config.json"
        cfg = json.loads(read(config_path)); cfg["router_token_budget"] = 1000
        config_path.write_text(json.dumps(cfg), encoding="utf8")
        (self.mem / "branches/DEV.md").write_text("# DEV branch\n", encoding="utf-8")
        (self.mem / "main/M2.md").write_text("# M2 planned mainline\n", encoding="utf-8")
        d = self.mem / "directory.md"
        d.write_text(
            read(d)
            + "* **[DEV]** [`DEV`](branches/DEV.md) ➔ `MEDIUM`\n"
            + "* **[M2]** [`M2`](main/M2.md) ➔ `PLANNED` | `[命中词: roadmap-m2]`\n",
            encoding="utf-8",
        )
        invoke(hfa.cmd_reindex, self.mem)
        state = hfa.parse_directory_nodes(d)
        self.assertIn("DEV", [n["id"] for n in state["nodes"]["MEDIUM"]])
        invoke(hfa.cmd_tombstone, self.mem, "DEV", "dev-archived", "done")
        state = hfa.parse_directory_nodes(d)
        self.assertIn("DEV", [n["id"] for n in state["nodes"]["TOMBSTONED"]])
        # Planned M2 is deferred without unlock and recalled with unlock
        locked = self.route("roadmap-m2")
        self.assertIn("M2", [x["id"] for x in locked["deferred"]])
        unlocked = self.route("roadmap-m2", "review M2 roadmap")
        self.assertEqual(unlocked["selected"][0]["id"], "M2")
        self.assertGreaterEqual(unlocked["selected"][0]["score"], 100)
        self.assertIn("match", unlocked["selected"][0])

    def test_sqlite_fts5_index_created_verified_and_rebuilt(self):
        import sqlite3
        self.add("SQLite FTS5 检索层", "fts5-engine", "使用内置 sqlite3 FTS5 加速混合检索与候选过滤。")
        fts_path = self.mem / "index/fts.sqlite"
        self.assertTrue(fts_path.is_file())
        conn = sqlite3.connect(str(fts_path))
        try:
            digest = dict(conn.execute("SELECT key, value FROM meta").fetchall())["source_digest"]
            self.assertEqual(digest, hfa._digest(self.mem))
            if hfa._fts5_supported():
                rows = conn.execute("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?", ('"fts5-engine"',)).fetchall()
                self.assertEqual(rows, [("chunk_0002",)])
        finally:
            conn.close()
        saved_bytes = fts_path.read_bytes()
        # Different SQLite C library minor versions at header offset 96..100 must not trigger false staleness
        diff_ver = bytearray(saved_bytes)
        diff_ver[96:100] = b"\x00\x2e\x76\xab"
        fts_path.write_bytes(bytes(diff_ver))
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 0)
        # Actual corruption or page tampering must be rejected and rebuilt
        fts_path.write_bytes(b"corrupted-sqlite")
        self.assertEqual(invoke(hfa.cmd_status, self.mem, True)[0], 1)
        with self.assertRaises(hfa.MemoryError):
            self.route("fts5-engine")
        invoke(hfa.cmd_reindex, self.mem)
        self.assertEqual(fts_path.read_bytes(), saved_bytes)
        self.assertEqual(invoke(hfa.cmd_reindex, self.mem, True)[0], 0)

    def test_zero_config_default_memory_dir_and_auto_init(self):
        auto_dir = self.path / "auto_memory"
        env = {"HFA_MEMORY_DIR": str(auto_dir)}
        # 1. --auto-init on status --json initializes and returns pure JSON
        proc = self.cli("status", "--json", "--auto-init", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        state = json.loads(proc.stdout)
        self.assertEqual(state["problems"], [])
        self.assertTrue((auto_dir / "directory.md").is_file())
        # 2. add-topic and recall without explicit memory_dir argument
        added = self.cli("add-topic", "--title", "零配置写入", "--hit-words", "zero-cfg-key",
                         "--conclusion", "省略 memory_dir 时默认解析 HFA_MEMORY_DIR 或 ./memory。", "--json", env=env)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual(json.loads(added.stdout)["id"], "chunk_0002")
        recalled = self.cli("recall", "--query", "zero-cfg-key", env=env)
        self.assertEqual(recalled.returncode, 0, recalled.stdout + recalled.stderr)
        self.assertIn("零配置写入", json.loads(recalled.stdout)["recall_text"])

    def test_pep517_stdlib_build_backend_wheel_and_editable(self):
        import tarfile
        import zipfile
        backend_spec = importlib.util.spec_from_file_location("build_backend", ROOT / "scripts/_build_backend.py")
        backend = importlib.util.module_from_spec(backend_spec)
        backend_spec.loader.exec_module(backend)
        wheel_dir = self.path / "dist"
        whl_name = backend.build_wheel(wheel_dir)
        edt_name = backend.build_editable(wheel_dir)
        sdist_name = backend.build_sdist(wheel_dir)
        with zipfile.ZipFile(wheel_dir / whl_name) as zf:
            names = set(zf.namelist())
            self.assertIn("scripts/hfa_manager.py", names)
            self.assertIn("scripts/templates/memory/directory.md", names)
            self.assertIn("scripts/templates/memory/index/fts.sqlite", names)
            ep = zf.read("hierarchical_attention_memory-2.2.0.dist-info/entry_points.txt").decode("utf-8")
            self.assertIn("hfa = scripts.hfa_manager:main", ep)
            self.assertIn("hfa-mcp = scripts.hfa_mcp:main", ep)
            self.assertIn("hfa-hook = scripts.hfa_hook:main", ep)
        with zipfile.ZipFile(wheel_dir / edt_name) as zf:
            self.assertIn("hierarchical_attention_memory.pth", set(zf.namelist()))
        with tarfile.open(wheel_dir / sdist_name, "r:gz") as tf:
            sdist_names = set(tf.getnames())
            self.assertIn("hierarchical_attention_memory-2.2.0/PKG-INFO", sdist_names)
            self.assertIn("hierarchical_attention_memory-2.2.0/pyproject.toml", sdist_names)

if __name__ == "__main__":
    unittest.main(verbosity=2)
