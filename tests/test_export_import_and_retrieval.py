from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hfa_manager as hfa  # noqa: E402


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "hfa_manager.py"), *args],
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )


class ExportImportAndRetrievalTests(unittest.TestCase):
    def test_export_import_roundtrip_idempotence_and_tamper_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            w1 = base / "w1"
            w2 = base / "w2"
            bundle_path = base / "portable.json"

            self.assertEqual(hfa.cmd_init(w1, quiet=True, profile="minimal"), 0)
            self.assertEqual(hfa.cmd_init(w2, quiet=True, profile="minimal"), 0)

            # Create an active side branch S1 and a tombstoned branch S2 in w1.
            (w1 / "branches").mkdir(parents=True, exist_ok=True)
            (w1 / "branches/S1.md").write_text("# S1 toolchain branch\n", encoding="utf-8")
            (w1 / "branches/S2.md").write_text("# S2 retired branch\n", encoding="utf-8")
            dir_text = (w1 / "directory.md").read_text(encoding="utf-8")
            dir_text += (
                "\n* **[S1]** [`S1`](branches/S1.md) ➔ `MEDIUM` | 工具分支 `[命中词: toolchain]`\n"
                "* **[S2]** [`S2`](branches/S2.md) ➔ `MEDIUM` | 废弃分支 `[命中词: legacy-s2]`\n"
            )
            (w1 / "directory.md").write_text(dir_text, encoding="utf-8")
            self.assertEqual(hfa.cmd_reindex(w1, quiet=True), 0)

            r1 = _cli(
                "add-topic", str(w1),
                "--title", "Old pool config",
                "--hit-words", "db-pool,connection-pool",
                "--conclusion", "DB_POOL_SIZE=8",
                "--source", "doc:pool.md#L5",
                "--request-id", "exp-1",
                "--json",
            )
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            c1 = json.loads(r1.stdout)["id"]

            r2 = _cli(
                "add-topic", str(w1),
                "--title", "Updated pool config",
                "--hit-words", "db-pool,connection-pool",
                "--conclusion", "DB_POOL_SIZE=16",
                "--source", "doc:pool.md#L18",
                "--supersedes", c1,
                "--request-id", "exp-2",
                "--json",
            )
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

            r3 = _cli(
                "add-topic", str(w1),
                "--title", "Side toolchain note",
                "--hit-words", "toolchain",
                "--conclusion", "USE_SCCACHE=true",
                "--project", "S1",
                "--source", "doc:tools.md#L3",
                "--request-id", "exp-3",
                "--json",
            )
            self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)

            r4 = _cli(
                "add-topic", str(w1),
                "--title", "Retired branch secret",
                "--hit-words", "legacy-s2",
                "--conclusion", "OLD_S2_VALUE=deadbeef",
                "--project", "S2",
                "--request-id", "exp-4",
                "--json",
            )
            self.assertEqual(r4.returncode, 0, r4.stdout + r4.stderr)
            rtomb = _cli("tombstone", str(w1), "--id", "S2", "--hit-words", "legacy-s2", "--reason", "retired")
            self.assertEqual(rtomb.returncode, 0, rtomb.stdout + rtomb.stderr)

            # Export from w1 via CLI.
            rexp = _cli("export", str(w1), "--out", str(bundle_path), "--json")
            self.assertEqual(rexp.returncode, 0, rexp.stdout + rexp.stderr)
            exp_meta = json.loads(rexp.stdout)
            self.assertEqual(exp_meta["schema"], "hfa.export.v1")
            self.assertEqual(exp_meta["records"], 4)
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            self.assertIn("S2", bundle["tombstones"])

            # Import into fresh w2 via CLI.
            rimp1 = _cli("import", str(w2), "--bundle", str(bundle_path), "--json")
            self.assertEqual(rimp1.returncode, 0, rimp1.stdout + rimp1.stderr)
            imp1 = json.loads(rimp1.stdout)
            self.assertEqual(imp1["added"], 4)
            self.assertEqual(imp1["skipped_existing"], 0)
            self.assertEqual(hfa.cmd_reindex(w2, verify=True, quiet=True), 0)

            # Mainline recall returns superseding DB_POOL_SIZE=16 and preserves source link; S2 stays locked.
            route_pool = hfa.cmd_route(w2, "db-pool connection-pool", return_result=True)
            self.assertIn("DB_POOL_SIZE=16", route_pool["recall_text"])
            self.assertIn("doc:pool.md#L18", route_pool["recall_text"])
            self.assertNotIn("DB_POOL_SIZE=8", route_pool["recall_text"])

            route_s2 = hfa.cmd_route(w2, "legacy-s2", return_result=True)
            self.assertNotIn("OLD_S2_VALUE=deadbeef", route_s2["recall_text"])
            self.assertIn("S2", route_s2["skipped_tombstones"])

            route_s1 = hfa.cmd_route(w2, "toolchain USE_SCCACHE", unlock="verify branch import", return_result=True)
            self.assertIn("USE_SCCACHE=true", route_s1["recall_text"])

            # Idempotent second import on w2 writes 0 new chunks and skips all 4 cleanly.
            before_files = {p.name: p.read_bytes() for p in sorted((w2 / "chunks").glob("*.json"))}
            rimp2 = _cli("import", str(w2), "--bundle", str(bundle_path), "--json")
            self.assertEqual(rimp2.returncode, 0, rimp2.stdout + rimp2.stderr)
            imp2 = json.loads(rimp2.stdout)
            self.assertEqual(imp2["added"], 0)
            self.assertEqual(imp2["skipped_existing"], 4)
            after_files = {p.name: p.read_bytes() for p in sorted((w2 / "chunks").glob("*.json"))}
            self.assertEqual(before_files, after_files)

            # Future schema_version must fail closed BEFORE mutating w2 truth.
            future_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            future_bundle["schema_version"] = 999
            payload_no_digest = {k: v for k, v in future_bundle.items() if k != "digest"}
            future_bundle["digest"] = "sha256:" + hashlib.sha256(
                json.dumps(payload_no_digest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            future_path = base / "future.json"
            future_path.write_text(json.dumps(future_bundle, ensure_ascii=False), encoding="utf-8")
            rfut = _cli("import", str(w2), "--bundle", str(future_path), "--json")
            self.assertNotEqual(rfut.returncode, 0)
            self.assertIn("schema_version", rfut.stdout)
            self.assertEqual(
                {p.name: p.read_bytes() for p in sorted((w2 / "chunks").glob("*.json"))},
                after_files,
            )

            # Tampered bundle payload with stale digest must also fail closed.
            tampered_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            tampered_bundle["records"][0]["title"] = "Tampered title"
            tampered_path = base / "tampered.json"
            tampered_path.write_text(json.dumps(tampered_bundle, ensure_ascii=False), encoding="utf-8")
            rtamp = _cli("import", str(w2), "--bundle", str(tampered_path), "--json")
            self.assertNotEqual(rtamp.returncode, 0)
            self.assertIn("digest", rtamp.stdout)

    def test_lexical_trailing_punctuation_morphology_aliases_and_caution_clauses(self):
        # Cautionary standalone Chinese clause stays searchable while retrieval negations are stripped.
        caution_q = "不要把“没有发现问题”等同“已经证明安全”。"
        self.assertNotEqual(hfa._positive_query(caution_q), "")
        self.assertEqual(hfa._positive_query("不要查乱码和部署"), "")
        self.assertEqual(hfa._positive_query("不要查阈值"), "")
        self.assertEqual(hfa._positive_query("不要看历史，直接检查端口"), "直接检查端口")

        # Trailing dot/hyphen stripped when query=True, legacy tokenization kept when query=False.
        q_terms = hfa._search_terms("Check ttl. across the network.", {"aliases": []}, query=True)
        self.assertIn("ttl", q_terms)
        self.assertIn("network", q_terms)
        self.assertNotIn("ttl.", q_terms)
        self.assertNotIn("network.", q_terms)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mem"
            self.assertEqual(hfa.cmd_init(root, quiet=True, profile="minimal"), 0)

            cases = [
                ("TTL policy", "network-ttl", "Configure ttl across the network and verify health.", "r1"),
                ("Explain backoff", "backoff-explain", "Explain why retry backoff uses random jitter.", "r2"),
                ("DB migration", "db-migration", "Database migration window must stay under 10 minutes.", "r3"),
                ("Gateway timeout", "gw-timeout", "Upstream gateway request timeout returns HTTP 504.", "r4"),
                ("Security caution", "safety-rule", "核心审计原则：不要把“没有发现问题”等同“已经证明安全”。", "r5"),
            ]
            for title, hits, summary, req in cases:
                hfa.cmd_add_topic(root, title, hits, summary, None, None, "doc:spec", None, req, True)

            res_dot = hfa.cmd_route(root, "Verify ttl. across the network.", return_result=True)
            self.assertIn("Configure ttl across the network", res_dot["recall_text"])

            res_ing = hfa.cmd_route(root, "Start explaining jitter", return_result=True)
            self.assertIn("Explain why retry backoff", res_ing["recall_text"])

            res_morph = hfa.cmd_route(root, "Can we migrate the database right now?", return_result=True)
            self.assertIn("Database migration window", res_morph["recall_text"])

            res_alias = hfa.cmd_route(root, "Any timed-out gateway issues?", return_result=True)
            self.assertIn("HTTP 504", res_alias["recall_text"])

            res_caution = hfa.cmd_route(root, caution_q, return_result=True)
            self.assertIn("已经证明安全", res_caution["recall_text"])


if __name__ == "__main__":
    unittest.main()
