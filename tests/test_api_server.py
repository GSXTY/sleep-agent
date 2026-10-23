"""FastAPI 兼容入口测试；未安装异步依赖时明确跳过。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import unittest
from unittest import mock

from sleepagent import nightdata

try:
    from fastapi.testclient import TestClient
    import api_server
except ImportError:
    TestClient = None
    api_server = None


@unittest.skipUnless(TestClient is not None, "FastAPI dependencies not installed")
class ApiServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        async def fake_load_nights_async(user_id=None, source=None):
            return nightdata.load_dataset(str(user_id or "DEMO-USER"))

        cls.night_patch = mock.patch.object(
            api_server.data_source,
            "load_nights_async",
            new=mock.AsyncMock(side_effect=fake_load_nights_async),
        )
        cls.night_patch.start()
        cls.client = TestClient(api_server.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.night_patch.stop()

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["streaming"])

    def test_chat_json_contract(self):
        response = self.client.post("/api/chat", json={
            "message": "什么是 AHI",
            "session_id": "api-json-test",
            "use_llm": False,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertIn("AHI", payload["data"]["text"])

    def test_sse_contract(self):
        with self.client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "什么是 AHI",
                "session_id": "api-sse-test",
                "use_llm": False,
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())
        self.assertIn("event: status", body)
        self.assertIn("event: message", body)
        self.assertIn("event: done", body)


if __name__ == "__main__":
    unittest.main()
