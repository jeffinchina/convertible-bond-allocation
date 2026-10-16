import unittest
from pathlib import Path

from service import normalize_query, report_document


class ServiceTest(unittest.TestCase):
    def test_normalize_name(self):
        self.assertEqual(normalize_query("四方科技"), "四方科技")

    def test_normalize_code(self):
        self.assertEqual(normalize_query("603339"), "603339")

    def test_normalize_fixed_phrase(self):
        self.assertEqual(normalize_query("帮我分析四方科技的配债数据。"), "四方科技")

    def test_empty_query(self):
        with self.assertRaises(ValueError):
            normalize_query("帮我分析配债数据")

    def test_incomplete_report_has_gate_message(self):
        value = {
            "status": "incomplete",
            "security": {"company": "测试公司", "code": "000001"},
            "generatedAt": "2026-08-28T16:00:00+08:00",
            "calculation": {"missingFields": ["registeredAmount"], "warnings": []},
        }
        body = report_document(value)
        self.assertIn("完整性闸门", body)
        self.assertIn("registeredAmount", body)

    def test_complete_report_keeps_conversion_ratio_analysis(self):
        value = {
            "status": "complete",
            "security": {"company": "测试公司", "code": "000001"},
            "generatedAt": "2026-09-02T12:00:00+08:00",
            "calculation": {
                "html": "<table><tr><td>行业｜主营业务</td><td>专业工程<br>洁净室工程</td></tr><tr><td>正股现价/转股价：103%</td></tr></table>",
                "warnings": [],
            },
        }
        body = report_document(value)
        self.assertIn("正股现价/转股价：103%", body)
        self.assertIn("行业｜主营业务", body)
        self.assertIn("专业工程<br>洁净室工程", body)

    def test_windows_launcher_is_portable_and_contains_no_user_profile(self):
        launcher = Path(__file__).with_name("配债数据分析.cmd").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("%~dp0", launcher)
        self.assertNotIn("C:\\Users\\", launcher)


if __name__ == "__main__":
    unittest.main()
