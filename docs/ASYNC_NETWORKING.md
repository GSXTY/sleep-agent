# 异步网络层

## 已实现

### 1. 共享 AsyncClient

`sleepagent/async_http.py` 提供进程级共享连接池：

- 最大连接数 50
- Keep-alive 连接数 20
- 默认超时 60 秒
- FastAPI lifespan 退出时自动关闭

默认不读取系统代理环境变量，避免失效代理导致模型连接失败。

需要公司代理时可设置：

```text
SLEEPAGENT_HTTP_TRUST_ENV=1
```

### 2. 异步聊天客户端

`sleepagent.llm.chat_async()` 支持：

- OpenAI 兼容接口
- Ollama
- 异步并发上限
- 等待超时
- API 错误和返回格式处理
- token usage 解析

同步 `llm.chat()` 继续保留，命令行和现有 8100 Demo 不受影响。

### 3. 异步后台数据

`backend_ya70.fetch_rows_async()` 使用共享 AsyncClient 请求 YA70 接口，
包含一次连接重试、HTTP 错误检查和 `recode/result` 校验。

`data_source.load_nights_async()` 在 backend 模式下完成：

```text
异步请求后台
→ 在线程池执行字段映射
→ 返回 Night 列表
```

### 4. FastAPI 数据预取

FastAPI 在进入同步 Agent 管线前先执行：

```text
await data_source.load_nights_async(...)
```

数据加载完成后写入现有数据集缓存，再由 `asyncio.to_thread()` 执行同步
报告和问答管线。

这样后台 HTTP 请求已经离开同步 `urllib`，不会阻塞事件循环。

### 5. 异步模型桥接

`pipeline.answer_async()` 在事件循环中建立异步桥接，然后在工作线程执行现有
同步路由、检索和报告逻辑。

工作线程中的 `llm.chat()` 会自动桥接回事件循环，通过
`llm.chat_async()` 和共享 AsyncClient 发送 DeepSeek 请求。

FastAPI 客户端断开或外层任务被取消时，会同时取消正在等待的异步模型请求。

同步 `pipeline.answer()` 继续供 CLI 和 8100 Demo 使用，两者互不影响。

## 测试

```powershell
python -m unittest tests.test_async_networking tests.test_api_server -v
```

覆盖：

- 共享 AsyncClient
- OpenAI 兼容响应解析
- token usage
- 异步 YA70 请求
- 异步数据源加载
- FastAPI JSON 和 SSE
- 管线异步模型桥接
- 客户端取消同步 Agent 工作线程中的模型请求

## 环境变量

```text
SLEEPAGENT_HTTP_TRUST_ENV=0
LLM_MAX_CONCURRENCY=8
LLM_QUEUE_TIMEOUT_SECONDS=15
API_MAX_CONCURRENCY=16
```
