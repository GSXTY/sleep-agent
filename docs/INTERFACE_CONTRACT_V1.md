# 睡眠呼吸产品助手 V1.0 接口契约

本文定义内部稳定契约和当前验证探针，不等同于最终生产 API。后端字段确定后，由入口适配层完成映射。

## 1. 内部输入：UserQuery

文件：`sleepagent/pipeline.py`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `question` | string | 是 | 用户问题，自动去除首尾空白 |
| `history` | list | 否 | 最近对话，默认空列表 |
| `use_llm` | boolean | 否 | 是否允许调用聊天模型，默认 `true` |
| `user_id` | string | 否 | 用户边界，默认 `DEMO-USER` |

当前数据源仍为单用户，真实多用户接入时在 `nightdata.load_dataset/load_night` 内部按 `user_id` 查询。

## 2. 内部输出：AgentResponse

| 字段 | 类型 | 说明 |
|---|---|---|
| `text` | string | 最终展示正文 |
| `mode` | string | 安全边界、型号确认、产品文档、设备数据、报告生成等 |
| `detail` | string | 模式补充说明 |
| `request_id` | string | 请求关联 ID |
| `sources` | string[] | 知识库来源或事实 ID |
| `tier` | string | `blocked` / `strong` / `medium` / `weak` / `none` |
| `validation` | object | `{ok: boolean|null, problems: string[]}` |
| `risk_flags` | string[] | 风险标记，如 `pressure`、`model_typo`、`validation_failed` |
| `structured` | object|null | 报告结构化数据；其他模式通常为 `null` |

`AgentResponse` 支持 `to_dict()`，HTTP 层应发送 `to_dict()`，不要把对象直接 JSON 序列化。

## 3. HTTP 探针

用途：验证 Pipeline 可以脱离命令行运行，不是生产网关。

### 请求

```http
POST /ask
Content-Type: application/json
```

```json
{
  "question": "滤芯多久换一次",
  "use_llm": false,
  "user_id": "DEMO-USER"
}
```

`user_id` 可选。`use_llm` 默认 `false`，避免探针误调用外部模型。

### 成功响应

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "text": "回答正文",
    "mode": "产品文档（规则版）",
    "detail": "未启用大模型",
    "request_id": "d6df0f46a88f",
    "sources": ["资料文件名 · 章节名"],
    "tier": "medium",
    "validation": {"ok": null, "problems": []},
    "risk_flags": ["llm_disabled"],
    "structured": null
  }
}
```

### 错误响应

| HTTP | code | 场景 |
|---:|---:|---|
| 400 | 400 | 请求体不是 JSON，或缺少 `question` |
| 404 | 404 | 路径不存在 |
| 500 | 500 | Pipeline 运行时异常 |

## 4. 结构化报告

样例文件：`contracts/structured_report_sample.json`

固定字段：

- `schema_version`：当前 `1.0.0`
- `report_type`：当前 `single_night`
- `record_date`
- `data_source`
- `rating`
- `core_metrics`
- `core_metric_cards`
- `modules`
- `summary`
- `acceptance`
- `missing_fields`
- `disclaimer`

指标卡字段：`key`、`source_field`、`label`、`value`、`unit`、`display`、`provided`。
缺失指标使用 `value=null`、`provided=false`、`display="未提供"`。

## 5. 日志契约

每次请求生成 `request_id`，日志格式：

```text
[时间] [请求ID] [模式] [意图] 召回:N 模型:是/否 校验:通过/未通过 耗时:Nms
```

异常前缀：

- `[SAFETY_BLOCK]`
- `[VALIDATION_FAIL]`
- `[DEGRADE]`

日志写入 `logs/agent.log`，单文件 5 MB 轮转，保留 5 个历史文件。

## 6. 后端接入原则

后端字段变化时，只修改入口适配层：

```text
后端响应 -> 字段映射/校验/缺失处理 -> UserQuery
AgentResponse -> 生产 API 信封 -> App
```

不要把数据库查询、字段名或鉴权逻辑写进 `pipeline.py`、`report.py` 或命令行层。
