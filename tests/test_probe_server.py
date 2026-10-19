"""HTTP 探针测试：证明 pipeline 可以脱离命令行独立运行。

这是「能不能接前端」的第一个硬指标——如果 pipeline 里藏着命令行依赖
（读 sys.argv、要求 input()），这个测试会直接失败。
"""

# 测试固定关闭向量：结果不能依赖开发机上是否装了 Ollama。
# 想验证真实向量效果请跑 eval_retrieval.py / tune_weights.py。
import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import probe_server


class TestProbeServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = probe_server.make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, payload):
        request = urllib.request.Request(
            "http://127.0.0.1:" + str(self.port) + "/ask",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:" + str(self.port) + path, timeout=5) as response:
            return response.status, response.read().decode("utf-8")

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIn("ok", body)

    def test_ask_returns_envelope(self):
        status, payload = self._post({"question": "滤芯多久换一次", "use_llm": False})
        self.assertEqual(status, 200)
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["message"], "ok")
        self.assertIn("text", payload["data"])
        self.assertIn("mode", payload["data"])
        self.assertIn("tier", payload["data"])
        self.assertIn("validation", payload["data"])
        self.assertIn("risk_flags", payload["data"])
        self.assertIn("滤芯", payload["data"]["text"])

    def test_safety_boundary_over_http(self):
        status, payload = self._post({"question": "帮我把压力调高一点", "use_llm": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["mode"], "安全边界")

    def test_user_id_is_accepted_by_probe(self):
        status, payload = self._post(
            {"question": "今天几号", "use_llm": False, "user_id": "USER-A"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["mode"], "系统信息")

    def test_missing_question_is_rejected(self):
        request = urllib.request.Request(
            "http://127.0.0.1:" + str(self.port) + "/ask",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 400)

    def test_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._get("/nope")
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
