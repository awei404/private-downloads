import io
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from bwi_monitor import driver_export


class DriverExportTests(unittest.TestCase):
    def fixture(self):
        return {
            "tasks": [{"task_no": "MT-TEST", "op_date": "2026-10-17", "route": "ORF", "score": 0,
                       "profile": {"driver_name": '=HYPERLINK("https://invalid.test")', "driver_phone": "12345678901", "source": "TMS司机资料"},
                       "review": {"route_ok": False, "return_status": "failed", "manual_deduction": 7, "manual_reason": "核对<情况>&原因\x00"},
                       "arrival": {"actual_arrival_et": "2026-10-17T17:00", "late_minutes": 2}, "pending_reasons": []},
                      {"task_no": "MT-PENDING", "op_date": "2026-10-17", "score": None, "pending_reasons": ["待登记到场"]}],
            "drivers": [{"name": "测试司机", "phone": "12345678901", "auto_score": 0, "manual_score": None,
                         "score": 0, "scored_task_count": 1, "pending_task_count": 1}],
            "window_start": "2026-09-18", "window_end": "2026-10-17",
            "filter": {"start": "2026-10-17", "end": "2026-10-17", "route": "ORF01"}, "sync": {"running": False}}

    def xml_parts(self, include_phone=False):
        payload = driver_export.export_scores(self.fixture(), include_phone=include_phone)
        with ZipFile(io.BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            parts = {name: archive.read(name) for name in archive.namelist()}
            for value in parts.values():
                ET.fromstring(value)
            return parts

    def test_valid_workbook_contains_scope_and_excludes_phones_by_default(self):
        parts = self.xml_parts()
        text = b"".join(parts.values()).decode()
        self.assertNotIn('12345678901', text)
        self.assertIn('四支线任务', text)
        self.assertIn('近30天自动平均分', text)
        self.assertIn('2026-09-18', text)
        self.assertIn('ORF01', text)
        self.assertIn('旧调度中按约定回填', text)

    def test_explicit_phone_option_includes_phone_as_text(self):
        parts = self.xml_parts(True)
        sheet = ET.fromstring(parts['xl/worksheets/sheet2.xml'])
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        cells = sheet.findall('.//s:c', ns)
        phone = next(c for c in cells if '12345678901' in ''.join(c.itertext()))
        self.assertEqual(phone.get('t'), 'inlineStr')

    def test_formula_looking_names_stay_text_and_pending_is_blank(self):
        sheet = ET.fromstring(self.xml_parts()['xl/worksheets/sheet2.xml'])
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        self.assertFalse(sheet.findall('.//s:f', ns))
        name = sheet.find('.//s:c[@r="D2"]', ns)
        self.assertEqual(name.get('t'), 'inlineStr')
        self.assertIn('=HYPERLINK', ''.join(name.itertext()))
        headers = {''.join(c.itertext()): c.get('r')[:-1] for c in sheet.findall('.//s:row[@r="1"]/s:c', ns)}
        score_col = headers['任务评分']
        self.assertEqual(''.join(sheet.find(f'.//s:c[@r="{score_col}2"]', ns).itertext()), '0')
        self.assertEqual(''.join(sheet.find(f'.//s:c[@r="{score_col}3"]', ns).itertext()), '')


if __name__ == '__main__':
    unittest.main()
