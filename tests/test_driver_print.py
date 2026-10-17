import unittest
from bwi_monitor import driver_print
from tests import test_driver_score_http as http_tests


class DriverPrintTests(unittest.TestCase):
    def test_escapes_names_and_keeps_missing_arrival_unknown(self):
        task = {'in_scope': True, 'route': 'RIC01', 'name': '<script>alert(1)</script>',
                'task_no': 'MT-TEST', 'op_date': '2026-10-17', 'phone': 'DO-NOT-PRINT-PHONE'}
        html = driver_print.render(task, {'taskName': 'BWI.H-RIC01-ORF01-BWI.H'}).decode()
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('DO-NOT-PRINT-PHONE', html)
        self.assertNotIn('<script>alert(1)', html)
        self.assertIn('1103 Oliver Hl Wy', html)
        self.assertIn('Not recorded', html)
        self.assertIn('CONFIRM WITH DISPATCH', html)
        self.assertIn('size:Letter', html)

    def test_print_uses_saved_actual_and_confirmed_appointment(self):
        task = {'in_scope': True, 'route': 'ORF01', 'actual_arrival_et': '2026-10-17T22:05',
                'scheduled_arrival_et': '2026-10-17T22:00'}
        html = driver_print.render(task, {}).decode()
        self.assertIn('2026-10-17 22:05', html)
        self.assertIn('2026-10-17 22:00', html)
        self.assertIn('30 Aberdeen Rd', html)
        with self.assertRaises(ValueError): driver_print.render({**task, 'in_scope': False}, {})
        with self.assertRaises(ValueError): driver_print.render({**task, 'route': 'JFK.H'}, {})


class DriverPrintHttpTests(unittest.TestCase):
    setUp = http_tests.DriverScoreHttpTests.setUp
    get = http_tests.DriverScoreHttpTests.get

    def test_print_scope_admin_host_and_no_writes(self):
        before = self.store.db.total_changes
        response = self.get('/api/driver-checkin/1.html')
        self.assertEqual(response._send.call_args.args[0], 200)
        self.assertIn('测试司机', response._send.call_args.args[1].decode())
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(self.get('/api/driver-checkin/2.html')._send.call_args.args[0], 404)
        self.assertEqual(self.get('/api/driver-checkin/999.html')._send.call_args.args[0], 404)
        self.assertEqual(self.get('/api/driver-checkin/1.html', token='admin')._send.call_args.args[0], 403)
        self.assertEqual(self.get('/api/driver-checkin/1.html', token='admin', supplied_token='admin')._send.call_args.args[0], 200)
        self.assertEqual(self.get('/api/driver-checkin/1.html', host='evil.test:8768')._send.call_args.args[0], 403)
        for path in ['/api/driver-checkin/bad.html', '/api/driver-checkin/1.html?arrival=made-up']:
            self.assertEqual(self.get(path)._send.call_args.args[0], 400)
