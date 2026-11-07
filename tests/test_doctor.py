"""Operational readiness must distinguish core, optional SDK and real host use."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from validate_hfa import hfa, invoke, snapshot, ROOT

spec = importlib.util.spec_from_file_location('doctor_tests', ROOT/'scripts/hfa_doctor.py')
doctor = importlib.util.module_from_spec(spec);spec.loader.exec_module(doctor)


class DoctorChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hfa-doctor-')
        self.root = Path(self.tmp.name)/'memory'
        invoke(hfa.cmd_init, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_healthy_report_omits_memory_body_and_does_not_claim_host_use(self):
        invoke(hfa.cmd_add_topic, self.root, 'Secret fixture', 'secret-fixture', 'DO_NOT_EXPOSE_731')
        before = snapshot(self.root)
        report = doctor.inspect_memory(self.root, hfa.cmd_status)
        self.assertTrue(report['ready_cli'])
        self.assertNotIn('DO_NOT_EXPOSE_731', json.dumps(report))
        self.assertIsNone(report['host']['model_used'])
        self.assertFalse(report['host']['registration_checked'])
        self.assertEqual(before, snapshot(self.root))

    def test_missing_sdk_is_optional_until_required(self):
        with patch.object(doctor, '_mcp_capability', return_value={'available':False,'compatible':False}):
            report = doctor.inspect_memory(self.root, hfa.cmd_status)
            self.assertTrue(report['ready'])
            self.assertFalse(report['ready_mcp'])
            required = doctor.inspect_memory(self.root, hfa.cmd_status, require_mcp=True)
            self.assertFalse(required['ready'])

    def test_missing_memory_is_reported_without_creating_it(self):
        missing = Path(self.tmp.name)/'missing'
        report = doctor.inspect_memory(missing, hfa.cmd_status)
        self.assertFalse(report['ready'])
        self.assertIn('memory_not_initialized', report['problems'])
        self.assertFalse(missing.exists())

    def test_damaged_index_is_reported_without_silent_repair(self):
        (self.root/'index/hitwords.json').write_text('{}', encoding='utf8')
        before = snapshot(self.root)
        report = doctor.inspect_memory(self.root, hfa.cmd_status)
        self.assertFalse(report['ready'])
        self.assertIn('index/hitwords.json', report['memory']['problems'])
        self.assertEqual(before, snapshot(self.root))

    def test_malformed_source_errors_do_not_disclose_record_contents(self):
        secret = 'private text should not appear'
        with patch.object(hfa, 'cmd_status', side_effect=ValueError(secret)):
            report = doctor.inspect_memory(self.root, hfa.cmd_status)
        self.assertFalse(report['ready'])
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report['memory']['error_type'], 'ValueError')

    def test_status_structured_return_is_the_same_health_report(self):
        rc,text = invoke(hfa.cmd_status, self.root, True)
        self.assertEqual(rc, 0)
        full = hfa.cmd_status(self.root, return_result=True)
        public = json.loads(text)
        self.assertNotIn('summary_text', public)
        self.assertNotIn(full['summary_text'], text)
        self.assertEqual(public, {k: v for k, v in full.items() if k != 'summary_text'})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hfa.cmd_status(self.root, True, include_text=True)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())['summary_text'], full['summary_text'])

    def test_real_cli_version_doctor_and_exit_codes(self):
        def cli(*args):
            return subprocess.run([sys.executable, '-B', str(ROOT/'scripts/hfa_manager.py'), *map(str,args)],
                                  capture_output=True, encoding='utf8', timeout=30)
        version = cli('--version')
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), 'HFA ' + doctor.package_version())
        good = cli('doctor', self.root, '--json')
        self.assertEqual(good.returncode, 0, good.stdout+good.stderr)
        self.assertTrue(json.loads(good.stdout)['ready_cli'])
        missing = cli('doctor', Path(self.tmp.name)/'absent', '--json')
        self.assertEqual(missing.returncode, 1)
        self.assertIn('memory_not_initialized', json.loads(missing.stdout)['problems'])

    def test_package_versions_match_build_metadata(self):
        spec = importlib.util.spec_from_file_location('doctor_build_version', ROOT/'scripts/_build_backend.py')
        backend = importlib.util.module_from_spec(spec);spec.loader.exec_module(backend)
        self.assertEqual(doctor.package_version(), backend.VERSION)
        self.assertIn('version = "' + doctor.package_version() + '"', (ROOT/'pyproject.toml').read_text(encoding='utf8'))


if __name__ == '__main__':
    unittest.main()
