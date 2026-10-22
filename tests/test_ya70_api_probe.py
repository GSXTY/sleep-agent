"""YA70 后端接口测试脚本的常量检查。"""
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "test_ya70_api.py"
SPEC = importlib.util.spec_from_file_location("test_ya70_api", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Ya70ApiProbeTest(unittest.TestCase):
    def test_request_body_matches_backend_contract(self):
        self.assertEqual(
            set(MODULE.REQUEST_BODY),
            {"userid", "startDate", "endDate"},
        )

    def test_url_and_auth_header_are_configured(self):
        self.assertTrue(MODULE.URL.endswith(
            "/infc/respStatisticsYa70/listByDateRange"
        ))
        self.assertTrue(MODULE.API_KEY)


if __name__ == "__main__":
    unittest.main()
