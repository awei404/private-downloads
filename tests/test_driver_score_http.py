import io
import json
import os
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile

from bwi_monitor import config, driver_arrivals, web
from bwi_monitor.store import Store
from tests import test_driver_arrivals as arrival_tests

NOW, segment = arrival_tests.NOW, arrival_tests.segment

KEY = "tms:" + "a" * 64


class DriverScoreHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.db.close)
        self.store.upsert_segments([segment(), dict(segment(2, task='MT-OTHER'), sortNum=1, destination='JFK.H')], NOW)
        self.store.db.execute('INSERT INTO driver_task_profiles VALUES(?,?,?,?,?,?,?,?)',
            ('MT-DRIVER-TEST', KEY, '测试司机', '12345678901', '测试供应商', 'TEST', 'TMS司机资料', NOW.isoformat()))
        self.store.db.execute('INSERT INTO driver_appointments VALUES(?,?,?,?,?)',
            ('MT-DRIVER-TEST', '2026-10-17T20:00:00+00:00', '测试约定', '1', NOW.isoformat()))

    def review(self, **kw):
        return dict(route_ok=True, return_status='not_required', manual_deduction=0, manual_reason='', version=0, **kw)

    def post(self, body, path, **kwargs):
        return arrival_tests.ArrivalHttpTests.post(self, body=body, path=path, **kwargs)

    def get(self, path, token='', supplied_token='', host='localhost:8768'):
        handler = object.__new__(web.Handler)
        handler.path, handler.store = path, self.store
        handler.headers = Message()
        handler.headers['Host'] = host
        handler.headers['X-Admin-Token'] = supplied_token
        handler.server = SimpleNamespace(server_address=('127.0.0.1', 8768))
        handler.connection = SimpleNamespace(getsockname=lambda: ('127.0.0.1', 8768))
        handler.driver_sync = SimpleNamespace(status=lambda: {'running': False, 'error': None})
        handler._send, handler.send_response, handler.send_header, handler.end_headers = [mock.Mock() for _ in range(4)]
        handler.wfile = io.BytesIO()
        with mock.patch.object(web, 'utcnow', return_value=NOW), mock.patch.object(config, 'ADMIN_TOKEN', token):
            handler.do_GET()
        return handler

    def test_review_and_arrival_update_same_task_result_without_mutating_tms(self):
        before = list(self.store.db.execute('SELECT data FROM segments ORDER BY id'))
        code, pending = self.post(self.review(), '/api/driver-score/1')
        self.assertEqual(code, 200)
        self.assertIsNone(pending['driver_score']['score'])
        code, arrival = self.post({'actual_arrival_et': '2026-10-17T16:05', 'version': 0}, '/api/driver-arrival/1')
        self.assertEqual(arrival['driver_score']['score'], 55)
        payload = self.review()
        payload.update(return_status='failed', manual_deduction=5, manual_reason='测试扣分', version=1)
        code, updated = self.post(payload, '/api/driver-score/1')
        self.assertEqual((code, updated['driver_score']['score']), (200, 25))
        self.assertEqual(self.post(payload, '/api/driver-score/1')[0], 409)
        self.assertEqual(before, list(self.store.db.execute('SELECT data FROM segments ORDER BY id')))

    def test_scope_readonly_auth_origin_and_version_errors(self):
        self.assertEqual(self.post(self.review(), '/api/driver-score/2')[0], 400)
        self.assertEqual(self.post(self.review(), '/api/driver-score/1', readonly=True)[0], 403)
        self.assertEqual(self.post(self.review(), '/api/driver-score/1', token='test')[0], 403)
        self.assertEqual(self.post(self.review(), '/api/driver-score/1', headers={'Origin': 'http://other.test'})[0], 403)
        self.assertEqual(self.post(self.review(), '/api/driver-score/1', headers={'Host': 'evil.test', 'Origin': 'http://evil.test'})[0], 403)
        self.assertEqual(self.post({**self.review(), 'manual_deduction': 10}, '/api/driver-score/1')[0], 400)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM driver_score_reviews').fetchone()[0], 0)

    def test_total_override_and_restore_accept_encoded_identity(self):
        path = '/api/driver-total-score/' + KEY.replace(':', '%3A')
        code, value = self.post({'manual_score': 80, 'reason': '测试调整', 'version': 0}, path)
        self.assertEqual((code, value['override']['manual_score']), (200, 80))
        self.assertEqual(self.post({'manual_score': 70, 'reason': '过期版本', 'version': 0}, path)[0], 409)
        code, value = self.post({'manual_score': None, 'reason': '恢复自动', 'version': 1}, path)
        self.assertEqual(code, 200)
        self.assertIsNone(value['override']['manual_score'])

    def test_query_filter_scope_and_invalid_dates(self):
        value = self.get('/api/drivers')._send.call_args.args[1]
        self.assertEqual([t['task_no'] for t in value['tasks']], ['MT-DRIVER-TEST'])
        self.assertEqual(len(value['drivers']), 1)
        self.assertEqual(self.get('/api/drivers?route=RIC01')._send.call_args.args[1]['tasks'], [])
        for query in ('route=JFK.H', 'start=bad', 'start=2026-02-30', 'start=2026-10-18&end=2026-10-17', 'route=ORF01&route=RIC01', 'include_phone=true'):
            with self.subTest(query=query):
                self.assertEqual(self.get('/api/drivers?' + query)._send.call_args.args[0], 400)
        self.assertEqual(self.get('/api/drivers', host='evil.test:8768')._send.call_args.args[0], 403)
        for path in ('/api/state', '/api/segment/1'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, host='evil.test:8768')._send.call_args.args[0], 403)

    def test_export_has_attachment_headers_and_explicit_phone_option(self):
        denied = self.get('/api/driver-export.xlsx', token='admin')
        self.assertEqual(denied._send.call_args.args[0], 403)
        for include in (0, 1):
            response = self.get(f'/api/driver-export.xlsx?route=ORF01&include_phone={include}', token='admin', supplied_token='admin')
            response.send_response.assert_called_once_with(200)
            headers = dict(call.args for call in response.send_header.call_args_list)
            self.assertIn('attachment;', headers['Content-Disposition'])
            with ZipFile(io.BytesIO(response.wfile.getvalue())) as archive:
                text = b''.join(archive.read(n) for n in archive.namelist()).decode()
            self.assertEqual('12345678901' in text, bool(include))
            self.assertNotIn('MT-OTHER', text)


if __name__ == '__main__':
    unittest.main()
