import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from bwi_monitor import config, windows_service
from bwi_monitor.web import Handler


class WindowsServiceTests(unittest.TestCase):
    def test_wraps_original_monitor_once_and_stops_driver_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'data' / 'monitor.db')
            sync = Mock()
            with patch.object(config, 'DB_PATH', path), patch.dict(os.environ, {'BWI_DASHBOARD_READONLY':'0', 'GOFO_AUTH_FILE':'local-token-file'}), \
                    patch('bwi_monitor.__main__.main', return_value=17) as monitor, \
                    patch('bwi_monitor.driver_sync.DriverSync', return_value=sync) as constructor:
                self.assertEqual(windows_service.main(), 17)
                constructor.assert_called_once_with(path, auth_file='local-token-file')
                monitor.assert_called_once_with([])
                sync.start_periodic.assert_called_once_with()
                sync.stop.assert_called_once_with()
                self.assertIsNone(Handler.driver_sync)

    def test_readonly_does_not_start_driver_sync_and_monitor_failure_propagates(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(config, 'DB_PATH', str(Path(temp)/'test.db')), patch.dict(os.environ, {'BWI_DASHBOARD_READONLY':'1'}), \
                    patch('bwi_monitor.__main__.main', side_effect=RuntimeError('test failure')), \
                    patch('bwi_monitor.driver_sync.DriverSync') as constructor:
                with self.assertRaisesRegex(RuntimeError, 'test failure'): windows_service.main()
                constructor.assert_not_called()
                self.assertIsNone(Handler.driver_sync)
