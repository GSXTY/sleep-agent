"""异步 HTTP、聊天客户端、后台查询和数据源加载。"""
import unittest
from unittest import mock

try:
    import httpx
    from sleepagent import async_http
except ImportError:
    httpx = None
    async_http = None

from sleepagent import backend_ya70, data_source, llm, nightdata


@unittest.skipUnless(httpx is not None, "async HTTP dependencies not installed")
class AsyncNetworkingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async_http.configure_transport(None)
        await async_http.close_client()

    async def asyncTearDown(self):
        await async_http.close_client()
        async_http.configure_transport(None)

    async def test_shared_client_requests(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        )
        async_http.configure_transport(transport)
        response = await async_http.request("GET", "https://example.test/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    async def test_chat_async_parses_openai_response(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "异步回答",
                    },
                }],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 8,
                    "total_tokens": 38,
                },
            })
        )
        async_http.configure_transport(transport)
        config = {
            "provider": "openai",
            "model": "deepseek-chat",
            "base_url": "https://example.test/v1",
            "api_key": "test-key",
            "timeout": 10,
        }
        with mock.patch.object(llm, "settings", return_value=config):
            result = await llm.chat_async([
                {"role": "user", "content": "测试"}
            ])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["content"], "异步回答")
        self.assertEqual(result["usage"]["input_tokens"], 30)
        self.assertEqual(result["usage"]["output_tokens"], 8)

    async def test_backend_fetch_rows_async(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "recode": 1,
                "result": [{"id": 1, "recordeDate": "2026-10-22"}],
            })
        )
        async_http.configure_transport(transport)
        rows = await backend_ya70.fetch_rows_async(
            10001, "2026-10-01", "2026-10-22", "test-key",
            "https://example.test/api", 10,
        )
        self.assertEqual(rows[0]["id"], 1)

    async def test_backend_data_source_loads_nights_async(self):
        cfg = {
            "source": "backend",
            "user_id": "10001",
            "url": "https://example.test/api",
            "key": "test-key",
            "start_date": "2026-10-01",
            "end_date": "2026-10-22",
            "timeout": 10,
        }
        nights = nightdata.load_dataset()
        with mock.patch.object(data_source, "settings", return_value=cfg), \
             mock.patch.object(
                 backend_ya70, "fetch_rows_async",
                 new=mock.AsyncMock(return_value=[{"id": 1}]),
             ) as fetch, \
             mock.patch.object(
                 backend_ya70, "rows_to_nights", return_value=nights
             ) as mapper:
            loaded = await data_source.load_nights_async()
        self.assertEqual(len(loaded), 7)
        fetch.assert_awaited_once()
        mapper.assert_called_once_with([{"id": 1}], 10001)

    async def test_sample_data_source_still_loads_async(self):
        cfg = {"source": "sample", "user_id": "DEMO-USER"}
        with mock.patch.object(data_source, "settings", return_value=cfg):
            loaded = await data_source.load_nights_async()
        self.assertTrue(loaded)


if __name__ == "__main__":
    unittest.main()
