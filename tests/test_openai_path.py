"""用本地模拟服务器验证 OpenAI 兼容接口这条路径。

之前只能用注入的假响应验证校验闸门，这个测试补上网络层：
起一个真的 HTTP 服务，检查请求格式、响应解析和错误提示是否都对。
"""

# 测试固定关闭向量：结果不能依赖开发机上是否装了 Ollama。
# 想验证真实向量效果请跑 eval_retrieval.py / tune_weights.py。
import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Handler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            if self.headers.get("Authorization") != "Bearer test-key":
                self._send(401, {"error": "bad key"})
                return
            self._send(200, {"data": [{"id": "deepseek-chat"}]})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        _Handler.received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(raw),
        })
        if self.headers.get("Authorization") != "Bearer test-key":
            self._send(401, {"error": {"message": "Authentication fails"}})
            return
        self._send(200, {
            "choices": [{"message": {"role": "assistant",
                                     "content": "你的平均治疗压力是 4.35 hPa，P95 是 5 hPa。"}}]
        })


class TestOpenAICompatiblePath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")}
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["LLM_BASE_URL"] = "http://127.0.0.1:" + str(self.port) + "/v1"
        os.environ["LLM_MODEL"] = "deepseek-chat"
        os.environ["LLM_API_KEY"] = "test-key"

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_request_is_well_formed(self):
        from sleepagent import llm
        _Handler.received.clear()
        result = llm.chat([{"role": "user", "content": "我的平均压力是多少"}])
        self.assertEqual(result["status"], "success", result)
        sent = _Handler.received[-1]
        self.assertTrue(sent["path"].endswith("/v1/chat/completions"), sent["path"])
        self.assertEqual(sent["auth"], "Bearer test-key")
        self.assertEqual(sent["body"]["model"], "deepseek-chat")
        self.assertIn("messages", sent["body"])
        self.assertFalse(sent["body"]["stream"])

    def test_response_content_parsed(self):
        from sleepagent import llm
        result = llm.chat([{"role": "user", "content": "问"}])
        self.assertIn("4.35", result["content"])

    def test_models_probe_succeeds(self):
        from sleepagent import llm
        self.assertTrue(llm.available())
        self.assertIn("连通正常", llm.test_connection())

    def test_bad_key_gives_readable_message(self):
        from sleepagent import llm
        os.environ["LLM_API_KEY"] = "wrong-key"
        result = llm.chat([{"role": "user", "content": "问"}])
        self.assertEqual(result["status"], "error")
        self.assertIn("鉴权失败", result["detail"])

    def test_missing_key_is_reported(self):
        from sleepagent import llm
        from unittest import mock
        os.environ["LLM_API_KEY"] = ""
        # 必须屏蔽 config.json：开发机上它可能配着真实密钥，
        # 否则这个用例会随环境时通时不通
        with mock.patch.object(llm, "_config", return_value={}):
            self.assertFalse(llm.available())
            self.assertIn("API Key", llm.test_connection())

    def test_device_mode_through_mock_model(self):
        import chat
        from sleepagent import nightdata
        result = chat.answer("我的平均压力是多少", nightdata.load_night(), [], use_llm=True)
        self.assertIn("设备数据", result["mode"])
        self.assertIn("已校验", result["mode"])

    def test_product_mode_uses_doc_consistent_answer(self):
        """产品模式是 grounded：回答里的数字必须在资料里出现过。"""
        import chat
        from sleepagent import nightdata
        from unittest import mock
        fake = {"status": "success", "provider": "openai", "model": "deepseek-chat",
                "content": "空气滤芯建议在 30 天内至少更换一次，滤芯不可清洗。",
                "detail": ""}
        with mock.patch.object(chat.llm, "chat", return_value=fake):
            result = chat.answer("滤芯多久换一次", nightdata.load_night(), [], use_llm=True)
        self.assertIn("产品文档", result["mode"])
        self.assertIn("已校验", result["mode"])

    def test_product_mode_rejects_numbers_not_in_docs(self):
        """模型给产品回答编了资料里没有的数字时，必须回退到资料原文。"""
        import chat
        from sleepagent import nightdata
        from unittest import mock
        fake = {"status": "success", "provider": "openai", "model": "deepseek-chat",
                "content": "空气滤芯建议每 888 天更换一次。", "detail": ""}
        with mock.patch.object(chat.llm, "chat", return_value=fake):
            result = chat.answer("滤芯多久换一次", nightdata.load_night(), [], use_llm=True)
        self.assertTrue(result["mode"].endswith("规则版）"), result["mode"])
        self.assertIn("材料中没有的数字", result["detail"])

    def test_general_mode_through_mock_model(self):
        # 通用问答也走模型，且不要求数字可追溯
        import chat
        from sleepagent import nightdata
        result = chat.answer("推荐一部电影", nightdata.load_night(), [], use_llm=True)
        self.assertIn("通用对话", result["mode"])
        self.assertIn("已校验", result["mode"])

    def test_mock_model_hallucination_is_caught(self):
        """材料模式下，模型编造的数字必须被拦下并回退。"""
        import chat
        from sleepagent import nightdata
        from unittest import mock
        fake = {"status": "success", "provider": "openai", "model": "deepseek-chat",
                "content": "你的平均压力是 4.35 hPa，比上周的 9.87 下降了 5.52。", "detail": ""}
        with mock.patch.object(chat.llm, "chat", return_value=fake):
            result = chat.answer("我的平均压力是多少", nightdata.load_night(), [], use_llm=True)
        self.assertTrue(result["mode"].endswith("规则版）"), result["mode"])
        self.assertIn("材料中没有的数字", result["detail"])

    def test_product_mode_hallucination_falls_back_to_doc_text(self):
        import chat
        from sleepagent import nightdata
        from unittest import mock
        fake = {"status": "success", "provider": "openai", "model": "deepseek-chat",
                "content": "滤芯建议每 999 天更换一次。", "detail": ""}
        with mock.patch.object(chat.llm, "chat", return_value=fake):
            result = chat.answer("滤芯多久换一次", nightdata.load_night(), [], use_llm=True)
        self.assertTrue(result["mode"].endswith("规则版）"), result["mode"])
        self.assertIn("我在产品资料里查到", result["text"])


if __name__ == "__main__":
    unittest.main()
