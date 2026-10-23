"""有资料可回退时使用短等待，并记录安全的失败分类。"""
import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import unittest
from unittest import mock

from sleepagent import embeddings, intent_fallback, llm, nightdata, pipeline, query_planner


CONFIG = {"provider": "openai", "model": "test", "base_url": "https://example.test",
          "api_key": "test-key", "timeout": 60.0}


class ModelTimeoutTest(unittest.TestCase):
    def test_classifiers_have_short_call_budgets_and_fall_back(self):
        with mock.patch.object(llm, "chat", return_value={"status": "error"}) as chat:
            self.assertIsNone(query_planner._classify_with_llm("最近表现如何"))
            self.assertEqual(chat.call_args.kwargs["timeout_seconds"], 4)
            self.assertIsNone(intent_fallback.classify("这个怎么弄"))
            self.assertEqual(chat.call_args.kwargs["timeout_seconds"], 4)

    def test_short_default_timeouts(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(llm, "_config", return_value={}), \
                mock.patch.object(embeddings, "_file_config", return_value={}):
            self.assertEqual(llm.settings()["timeout"], 15)
            self.assertEqual(embeddings.settings()["timeout"], 8)

    def test_call_budget_caps_transport_and_queue_without_mutating_config(self):
        with mock.patch.object(llm, "settings", return_value=CONFIG), \
                mock.patch.object(llm, "_chat_slots") as slots, \
                mock.patch.object(llm, "_post", return_value={
                    "ok": False, "detail": "timed out",
                }) as post:
            slots.acquire.return_value = True
            result = llm.chat([], timeout_seconds=8)
        self.assertEqual(result["status"], "error")
        self.assertEqual(post.call_args.args[-1], 8)
        self.assertLessEqual(slots.acquire.call_args.kwargs["timeout"], 0.5)
        slots.release.assert_called_once()
        self.assertEqual(CONFIG["timeout"], 60)

    def test_product_timeout_returns_document_and_logs_reason(self):
        pipeline.clear_public_cache()
        failure = {"status": "error", "content": "", "detail": "timed out",
                   "provider": "openai", "model": "test"}
        with mock.patch.object(llm, "chat", return_value=failure) as chat, \
                self.assertLogs("sleepagent.pipeline", level="WARNING") as logs:
            result = pipeline.answer("滤芯多久换一次", nightdata.load_night(), [], use_llm=True)
        self.assertIn("产品文档", result.mode)
        self.assertIn("滤芯", result.text)
        self.assertIn("llm_error", result.risk_flags)
        self.assertEqual(chat.call_args.kwargs["timeout_seconds"], 8)
        self.assertTrue(any("reason=timeout" in line and result.request_id in line
                            for line in logs.output))

    def test_failure_log_distinguishes_empty_from_auth_and_hides_detail(self):
        for status, detail, reason in (("empty", "private response", "empty_content"),
                                       ("error", "401 secret-key", "authentication")):
            with self.assertLogs("sleepagent.pipeline", level="WARNING") as logs:
                pipeline._log_model_failure("request-test", {"status": status, "detail": detail})
            self.assertIn("reason=" + reason, logs.output[0])
            self.assertNotIn(detail, logs.output[0])


class AsyncModelTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_timeout_returns_failure_and_releases_slot(self):
        from sleepagent import async_http
        slots = mock.Mock()
        slots.acquire = mock.AsyncMock(return_value=True)
        with mock.patch.object(llm, "settings", return_value=CONFIG), \
                mock.patch.object(llm, "_async_chat_slots", slots), \
                mock.patch.object(async_http, "request", new_callable=mock.AsyncMock,
                                  side_effect=TimeoutError()) as request:
            result = await llm.chat_async([], timeout_seconds=8)
        self.assertEqual(result["status"], "error")
        self.assertIn("超时", result["detail"])
        self.assertEqual(request.call_args.kwargs["timeout"], 8)
        slots.release.assert_called_once()
