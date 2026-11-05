"""Run emitted hook commands through cmd's native argv entry point on Windows."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "nt", "Antigravity Windows command-marshalling regression")
class WindowsHookLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hfa-launch-")
        self.workspace = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def generated(self, hook, memory):
        proc = subprocess.run(
            [sys.executable, "-B", str(hook), "--print-hooks", "--memory-dir", str(memory)],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["hfa-auto-recall"]

    def invoke(self, command, payload):
        # shell=True has different quoting and used to hide this host failure.
        proc = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, cwd=self.workspace, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))

    def test_all_generated_phases_launch_via_native_cmd_argv(self):
        config = self.generated(ROOT / "scripts/hfa_hook.py", self.workspace / "memory")
        for event in ("PreInvocation", "PostInvocation", "Stop"):
            with self.subTest(event=event):
                command = config[event][0]["command"]
                self.assertNotIn('"', command)
                result = self.invoke(command, {"invocationNum": 0})
                self.assertEqual(result, {"decision": "allow"} if event == "Stop" else {})

    def test_unicode_and_shell_metacharacter_paths_deliver_the_correct_summary(self):
        special = self.workspace / "space 中文 %TEMP% & (test) ' folder"
        special.mkdir()
        for name in ("hfa_hook.py", "hfa_manager.py"):
            shutil.copy2(ROOT / "scripts" / name, special / name)
        memory = special / "memory with spaces"
        proc = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/hfa_manager.py"), "init", str(memory)],
            capture_output=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = memory / "summary.md"
        summary.write_text(summary.read_text(encoding="utf-8") + "\nLAUNCH_BODY_中文_73a9\n", encoding="utf-8")
        config = self.generated(special / "hfa_hook.py", memory)
        command = config["PreInvocation"][0]["command"]
        self.assertNotIn('"', command)
        result = self.invoke(command, {
            "conversationId": "synthetic-launch-test", "turnId": "synthetic-launch-turn",
            "workspacePaths": [str(special)], "lastUserInput": "不要保存本轮内容。核对摘要。",
        })
        self.assertIn("LAUNCH_BODY_中文_73a9", json.dumps(result, ensure_ascii=False))
        self.assertFalse((self.workspace / "memory with spaces").exists())


if __name__ == "__main__":
    unittest.main()
