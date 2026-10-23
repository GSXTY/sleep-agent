# 并发控制与 SSE

## 当前定位

当前 Demo 使用 `ThreadingHTTPServer`，定位为内部评审版本，没有直接迁移到 FastAPI。

本阶段先解决三个最影响稳定性的问题：

1. 同一会话的请求必须按顺序执行。
2. 模型调用不能无限并发。
3. 长请求需要及时给前端返回状态。

## 同一会话串行

每个 `(user_id, session_id)` 使用独立锁：

- 同一会话的多条消息串行处理，避免历史覆盖和回答乱序。
- 不同用户、不同会话可以并行，不会因为一个全局锁被整体串行化。
- 用户治疗数据按 `user_id` 加独立加载锁，避免重复并发请求后台。

## 模型并发上限

环境变量：

```text
LLM_MAX_CONCURRENCY=8
LLM_QUEUE_TIMEOUT_SECONDS=15
```

当模型并发槽已满时，请求最多等待指定时间，然后返回可降级状态，不会无限排队。

该上限是单进程限制。多实例部署时应使用 Redis、模型网关或队列统一限流。

## SSE 接口

入口：

```text
POST /api/chat/stream
```

请求体与 `/api/chat` 相同：

```json
{
  "message": "帮我分析这一周的数据",
  "session_id": "session-id",
  "use_llm": true
}
```

事件顺序：

```text
event: status
data: {"stage":"processing","text":"正在检索资料并分析数据…"}

event: message
data: {AgentResponse}

event: done
data: {"request_id":"..."}
```

如果处理失败：

```text
event: error
data: {"message":"服务暂时不可用，请稍后再试。"}
```

连接空闲时每 15 秒发送一次 SSE heartbeat。

## 为什么不直接推送模型 token

产品、设备数据和报告回答需要经过：

- 数字白名单
- 日期校验
- 禁止语校验
- 医疗安全边界

如果模型 token 先展示，后发现数字或医疗表述不合规，无法撤回用户已经看到的内容。

因此当前策略是：

```text
推送处理状态
→ 完整生成并校验
→ 推送最终安全答案
```

报告和分析本来就不调用大模型，后续可以进一步按章节分段推送。

## 前端行为

H5 使用 `fetch` 读取 `ReadableStream`，不直接使用只方便 GET 的 `EventSource`。

如果没有流式接口或浏览器不支持流式读取，会自动回退到普通 `/api/chat`。

## 尚未完成

- 客户端断开后，当前 Python 工作线程不能强制终止同步模型请求。
- 会话、数据集、缓存和锁仍在单进程内存中。
- 还没有 Redis 分布式锁、跨实例限流和多节点会话同步。
- 还没有接入真正的模型 token 流式接口。

## 生产建议

正式版本建议按以下顺序升级：

```text
FastAPI / Uvicorn
→ async httpx
→ Redis 会话、缓存和分布式锁
→ 模型并发队列与熔断
→ 安全 SSE
→ 压测和成本监控
```
