"""App Demo 服务：页面、聊天、多轮确认与反馈。"""
import os

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

import json
import shutil
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import demo_server


class DemoServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = demo_server.make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        demo_server._sessions.clear()
        demo_server._session_nights.clear()

    def _url(self, path):
        return "http://127.0.0.1:" + str(self.port) + path

    def _post(self, path, payload):
        request = urllib.request.Request(
            self._url(path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, path):
        with urllib.request.urlopen(self._url(path), timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def test_index_page(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("睡眠呼吸助手", body)
        self.assertIn("renderMarkdown", body)
        self.assertIn("polishReportText", body)
        self.assertIn("stripInternalValidation", body)
        self.assertIn("stripInlineSources", body)
        self.assertIn("isNearBottomElement", body)
        self.assertIn("scrollMessageToStart", body)
        self.assertIn("YA70工作原理是什么？", body)
        self.assertIn("有什么工作模式？", body)
        self.assertIn("帮我生成一份报告", body)
        self.assertNotIn("帮我生成一份报告？", body)
        self.assertIn("帮我分析一下这一周的数据", body)
        self.assertIn("data-table", body)
        self.assertIn("table-wrap", body)
        self.assertIn("data-label", body)
        self.assertIn("@media (max-width: 520px)", body)

    def test_health(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertIn('"ok"', body)

    def test_internal_model_error_is_not_exposed(self):
        result = SimpleNamespace(
            detail="模型调用失败（error）：connection refused",
            risk_flags=["llm_error"],
        )
        self.assertEqual(demo_server.public_detail(result), "")

    def test_chat_returns_answer(self):
        payload = self._post("/api/chat", {
            "message": "什么是 AHI", "use_llm": False,
            "session_id": "test-" + uuid.uuid4().hex[:8],
        })
        self.assertEqual(payload["code"], 0)
        self.assertIn("AHI", payload["data"]["text"])
        self.assertTrue(payload["data"]["session_id"])

    def test_multi_turn_confirmation(self):
        session = "turn-" + uuid.uuid4().hex[:8]
        first = self._post("/api/chat", {
            "message": "我昨晚的ahi是多少", "use_llm": False, "session_id": session})
        self.assertIn("时间确认", first["data"]["mode"])
        second = self._post("/api/chat", {
            "message": "是的", "use_llm": False, "session_id": session})
        self.assertIn("设备数据", second["data"]["mode"])
        self.assertIn("5.7", second["data"]["text"])

    def test_feedback_is_saved(self):
        directory = Path(demo_server.FEEDBACK_FILE).parent / ("test-" + uuid.uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "feedback.jsonl"
        try:
            with mock.patch.object(demo_server, "FEEDBACK_FILE", target):
                payload = self._post("/api/feedback", {
                    "request_id": "req-1", "rating": "up", "session_id": "s-1",
                    "question": "什么是 AHI", "answer": "AHI 是……",
                    "mode": "产品文档"})
            self.assertEqual(payload["code"], 0)
            record = json.loads(target.read_text(encoding="utf-8").strip())
            self.assertEqual(record["rating"], "up")
            self.assertEqual(record["question"], "什么是 AHI")
            self.assertEqual(record["mode"], "产品文档")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
