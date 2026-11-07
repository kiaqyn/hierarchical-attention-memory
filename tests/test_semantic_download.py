"""No real download: fail closed on wrong existing/downloaded bytes."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import hfa_download_model as downloader


class DownloadChecks(unittest.TestCase):
    def test_wrong_existing_file_is_preserved_without_network(self):
        with tempfile.TemporaryDirectory(prefix='hfa-download-existing-') as temp:
            root=Path(temp)
            target=root/'model.onnx'
            target.write_bytes(b'existing-owner-file')
            with patch.object(downloader.urllib.request,'urlopen',side_effect=AssertionError('unexpected network')):
                with self.assertRaisesRegex(ValueError,'Existing model file differs'):
                    downloader.download(root)
            self.assertEqual(target.read_bytes(),b'existing-owner-file')
            self.assertFalse((root/'manifest.json').exists())

    def test_checksum_failure_never_publishes_partial_model_or_manifest(self):
        with tempfile.TemporaryDirectory(prefix='hfa-download-bad-') as temp:
            root=Path(temp)
            with patch.object(downloader.urllib.request,'urlopen',return_value=io.BytesIO(b'wrong-public-bytes')):
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):downloader.download(root)
            self.assertEqual(list(root.iterdir()),[])


if __name__=='__main__':unittest.main()
