# Agent 数据源接入

Agent 支持两种数据源：

- `sample`：默认样板数据，适合离线和单元测试；
- `backend`：YA70 日期区间统计接口，适合 App 联调。

数据源选择与分析引擎解耦，所有入口最终都得到同样的 `Night.history`。

## 1. 本地配置

创建 `.local/ya70_api.json`：

```json
{
  "data_source": "backend",
  "url": "http://127.0.0.1:8081/infc/respStatisticsYa70/listByDateRange",
  "user_id": 10001,
  "key": "<你的测试key>",
  "start_date": "2026-09-23",
  "end_date": "2026-10-22",
  "timeout": 20
}
```

`.local/` 已加入 `.gitignore`，不会上传 GitHub。

也可以不写 `start_date/end_date`，此时默认查询截至今天最近 30 天。

## 2. 环境变量

```powershell
$env:SLEEPAGENT_DATA_SOURCE = "backend"
$env:SLEEPAGENT_USER_ID = "10001"
$env:SLEEPAGENT_BACKEND_URL = "http://127.0.0.1:8081/infc/respStatisticsYa70/listByDateRange"
$env:SLEEPAGENT_BACKEND_KEY = "<你的测试key>"
$env:SLEEPAGENT_BACKEND_LOOKBACK_DAYS = "30"
```

环境变量优先级高于配置文件。

## 3. 命令行

样板数据：

```powershell
python chat.py --no-llm "最近一次使用情况"
```

真实后台：

```powershell
python chat.py --data-source backend --user-id 10001 --no-llm
```

也可以双击：

```text
chat_backend.cmd
```

单次提问：

```powershell
python chat.py `
  --data-source backend `
  --user-id 10001 `
  --no-llm `
  "我的评分有什么变化"
```

## 4. App Demo

```powershell
python demo_server.py `
  --host 0.0.0.0 `
  --data-source backend `
  --user-id 10001
```

也可以双击：

```text
demo_backend.cmd
```

Demo 的 `/api/chat` 也支持请求体传入：

```json
{
  "message": "最近一次使用情况",
  "user_id": "10001"
}
```

## 5. 正式 App 安全边界

Demo 允许直接传 `user_id`。正式版本不能信任客户端传入的任意 ID，正确流程是：

```text
App 登录 token
→ Agent 服务端校验 token
→ 服务端取得当前 userid
→ 使用服务端保存的后台 key 查询
```

后台 key 不能放进手机 App，也不能允许普通用户查询别人的 `userid`。
