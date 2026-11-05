"""Actual Windows share handles plus fail-fast behavior for unrelated errors."""
import ctypes
import errno
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from validate_hfa import hfa


class AtomicReplaceChecks(unittest.TestCase):
    def test_unclassified_permission_error_is_not_retried(self):
        with patch.object(hfa.os,'replace',side_effect=PermissionError(errno.EACCES,'synthetic permission')) as replace:
            with patch.object(hfa.time,'sleep') as wait:
                with self.assertRaises(PermissionError):hfa._replace_atomic('source','target')
                replace.assert_called_once();wait.assert_not_called()

    def open_shared_reader(self,path):
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0,None)
        self.assertNotIn(handle,(None,ctypes.c_void_p(-1).value),ctypes.get_last_error())
        return kernel,handle

    @unittest.skipUnless(os.name=='nt','Windows sharing semantics required')
    def test_temporary_reader_conflict_recovers_without_permission_changes(self):
        with tempfile.TemporaryDirectory(prefix='hfa-replace-transient-') as temp:
            target=Path(temp)/'index.json';target.write_bytes(b'old')
            kernel,handle=self.open_shared_reader(target)
            real_replace=os.replace;attempts=[];timer=[]
            def replace(source,destination):
                attempts.append(1)
                try:return real_replace(source,destination)
                except OSError:
                    if not timer:
                        close=threading.Timer(.04,lambda:kernel.CloseHandle(handle))
                        timer.append(close);close.start()
                    raise
            try:
                with patch.object(hfa.os,'replace',side_effect=replace):hfa._atomic_write(target,b'new')
            finally:
                if timer:timer[0].join()
                else:kernel.CloseHandle(handle)
            self.assertGreaterEqual(len(attempts),2)
            self.assertLessEqual(len(attempts),4)
            self.assertEqual(target.read_bytes(),b'new')
            self.assertEqual(list(target.parent.glob('.hfa-stage-*')),[])

    @unittest.skipUnless(os.name=='nt','Windows sharing semantics required')
    def test_persistent_reader_conflict_is_bounded_and_preserves_old_bytes(self):
        with tempfile.TemporaryDirectory(prefix='hfa-replace-persistent-') as temp:
            target=Path(temp)/'index.json';target.write_bytes(b'old')
            kernel,handle=self.open_shared_reader(target)
            real_replace=os.replace
            try:
                with patch.object(hfa.os,'replace',wraps=real_replace) as replace:
                    with self.assertRaises(OSError) as result:hfa._atomic_write(target,b'new')
                    self.assertIn(result.exception.winerror,(5,32))
                    self.assertEqual(replace.call_count,4)
            finally:kernel.CloseHandle(handle)
            self.assertEqual(target.read_bytes(),b'old')
            self.assertEqual(list(target.parent.glob('.hfa-stage-*')),[])


if __name__=='__main__':unittest.main()
