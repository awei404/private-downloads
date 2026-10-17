import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_builder', ROOT/'deploy/build_windows_package.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class WindowsPackageTests(unittest.TestCase):
    def test_code_allowlist_no_credentials_data_or_runtime_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = builder.build(folder)
            with ZipFile(archive) as z:
                self.assertIsNone(z.testzip())
                names = {n.split('/',1)[1] for n in z.namelist()}
                self.assertTrue({'install.ps1','bwi_monitor/driver_print.py','bwi_monitor/windows_service.py','VERSION.txt'}.issubset(names))
                for n in names:
                    self.assertFalse(n.startswith(('secrets/','data/','dev/','trial/')), n)
                    self.assertFalse(n.endswith(('.db','.sqlite','.csv','.log','.pyc')), n)
                    self.assertNotEqual(n,'.env')
                sums = z.read('BWI-Monitor-Windows/PACKAGE-SHA256SUMS.txt').decode().splitlines()
                self.assertEqual(len(sums), len(names)-1)
                for row in sums:
                    digest,name = row.split('  ',1)
                    self.assertEqual(digest, hashlib.sha256(z.read('BWI-Monitor-Windows/'+name)).hexdigest())
                self.assertIn(b'\r\n',z.read('BWI-Monitor-Windows/Install-BWI-Monitor.bat'))
            expected = (Path(folder)/'SHA256SUMS.txt').read_text().split()[0]
            self.assertEqual(expected,hashlib.sha256(archive.read_bytes()).hexdigest())
