# FastAPI 异步入口

## 目标

在保留现有 `demo_server.py` 的前提下，新增独立 FastAPI 入口：

```text
现有入口：http://127.0.0.1:8100
FastAPI：http://127.0.0.1:8101
```

8100 不修改，作为完整回滚通道。8101 验收通过后再切换 App 地址。

## 安装

```powershell
python -m pip install -r requirements-async.txt
```

## 启动

```powershell
$env:SLEEPAGENT_DATA_SOURCE = "backend"
$env:SLEEPAGENT_USER_ID = "<测试用户ID>"
python -m uvicorn api_server:app --host 0.0.0.0 --port 8101
```

也可以使用 `run_async.cmd`。

## 接口

- `GET /api/health`
- `POST /api/chat`
- `POST /api/chat/stream`
- `POST /api/feedback`

`/api/chat` 和 `/api/chat/stream` 与现有 Demo 使用同样的请求与响应语义。

## 当前实现范围

第一阶段 FastAPI 入口已经具备：

- ASGI/Uvicorn
- 异步路由
- 每会话 `asyncio.Lock`
- 全局限流 `asyncio.Semaphore`
- SSE `StreamingResponse`
- 心跳事件
- 客户端断开后停止后续 SSE 推送
- 共享 `httpx.AsyncClient` 连接池
- 异步后台数据预取
- `llm.chat_async()` 异步模型客户端
- `pipeline.answer_async()` 异步模型桥接
- 客户端取消时停止等待中的模型请求

现有同步 Agent 管线暂时通过：

```python
await asyncio.to_thread(demo_server.chat, ...)
```

调用，因此不会阻塞 FastAPI 事件循环。

## 尚未完成

- 8100 Demo 和 CLI 仍使用同步模型路径，作为回滚兼容入口
- 正式 Token 鉴权尚未接入；默认仅为内部 Demo 模式
- 单进程内存会话和锁在多实例下仍需 Redis

异步网络层的实现和迁移边界见 [ASYNC_NETWORKING.md](ASYNC_NETWORKING.md)。

## 服务器部署

正式服务器需要安装 Python 依赖，并使用 Uvicorn 启动 ASGI 应用。

建议由进程管理器托管：

- Linux：systemd
- Docker：容器主进程
- Kubernetes：Deployment

不要把 Uvicorn 开发文件监听模式用于生产：

```text
错误：--reload
```

单实例内部试用：

```powershell
python -m uvicorn api_server:app --host 0.0.0.0 --port 8101
```

多实例时必须使用 Redis 或粘性会话，并确保同一用户会话路由到同一状态存储。

## 回滚

应用层切换只需把 App WebView 地址从 8101 改回 8100。

代码层回滚标签：

```text
checkpoint-2026-10-23-pre-fastapi
```
