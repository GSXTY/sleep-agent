"""Agent 管线的异步模型桥接与取消。"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")

from sleepagent import nightdata, pipeline


class AsyncPipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.night = nightdata.load_night()
        pipeline.clear_public_cache()

    def tearDown(self):
        pipeline.clear_public_cache()

    async def test_deterministic_answer_async(self):
        result = await pipeline.answer_async(
            pipeline.UserQuery("什么是 AHI", use_llm=False),
            self.night,
        )
        self.assertIn("指标解释", result.mode)
        self.assertIn("AHI", result.text)

    async def test_pipeline_uses_async_model_client(self):
        async def fake_chat_async(messages, **kwargs):
            return {
                "status": "success",
                "provider": "fake",
                "model": "fake",
                "detail": "",
                "content": "空气滤芯建议在 30 天内至少更换一次，滤芯不可清洗。",
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 20,
                    "total_tokens": 100,
                },
            }

        with mock.patch.object(
            pipeline.llm, "chat_async", side_effect=fake_chat_async
        ) as chat:
            result = await pipeline.answer_async(
                pipeline.UserQuery("滤芯多久换一次", use_llm=True),
                self.night,
            )
        self.assertEqual(chat.await_count, 1)
        self.assertIn("产品文档", result.mode)
        self.assertIn("30 天", result.text)

    async def test_cancel_stops_waiting_async_model_request(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_chat(messages, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with mock.patch.object(
            pipeline.llm, "chat_async", side_effect=blocking_chat
        ):
            task = asyncio.create_task(pipeline.answer_async(
                pipeline.UserQuery("滤芯多久换一次", use_llm=True),
                self.night,
            ))
            await asyncio.wait_for(started.wait(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(cancelled.wait(), timeout=3)


if __name__ == "__main__":
    unittest.main()
