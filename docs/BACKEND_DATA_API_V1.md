# 后端数据对接文档 V1

面向对象：云端后端、App 团队、Agent 开发。
适用代码：`<PROJECT_DIR>`（V1.0 基础闭环版）。

这份文档定义 **Agent 向云端要哪些数据、字段长什么样、缺了怎么办**。
接口由后端提供，Agent 只读，不写任何治疗数据。

---

## 0. 一页速览

| 项 | 结论 |
|---|---|
| 需要几个接口 | 3 个（列表、单晚、图表序列）；图表可二期 |
| 数据粒度 | **一个用户一晚一条聚合记录**，不是一次上传一条 |
| 单位 | **压力接口用 Pa（已确认）**，Agent ÷100 转 hPa 展示；时长分钟；容量 mL；流量 L/min |
| 缺失约定 | **没有值就传 `null`，绝不传 0** |
| 必填 | `user_id`、`record_date`、`treat_time_minutes` |
| 时区 | 全部按北京时间（UTC+8），时间字段带偏移量 |
| 调用方式 | Agent 服务账号 + Bearer Token，只读 |
| 超时预算 | 单接口 3 秒，重试 1 次；总回答预算 8 秒 |
| 上线前 | 必须跑「同晚对账」，见第 12 节 |

后端如果只能先做一个接口，就做 **最近 N 晚列表（4.1）并返回完整字段**——
一次调用同时覆盖单晚问答和多晚趋势；4.2 的单晚详情只是按需优化，不是必需。

---

## 1. 目标与边界

### 目标

1. Agent 能按 `user_id` 取到最近若干晚的治疗数据，用于问答和单晚报告。
2. 字段含义、单位、缺失语义与现有确定性事实层一致，不产生"两个版本的数字"。
3. 后端改字段时，只改适配层，不动 `pipeline / report / safety`。

### 不在本期范围

- 不做写接口（不接受 Agent 改设备参数、改治疗方案）。
- 不做实时波形（蓝牙实时数据仍由 App 直连设备）。
- 不做跨用户统计、运营看板（后续单独设计）。
- Agent 不生成图表；图表仍由后台渲染，Agent 只负责引用或转述。

---

## 2. 现状：数据现在从哪来

当前版本有两条数据入口，都收敛到同一个内部结构 `Night.raw`：

| 入口 | 文件 | 说明 |
|---|---|---|
| 样板数据 | `data/sample_nights.json` | 7 晚模拟数据，字段最全，用于开发与测试 |
| 报告导出 CSV | `import_report_export.py` | 把后台「N 天报告导出」映射成内部字段，压力值需除以 100 |

`nightdata.load_dataset(user_id)` 读取数据并逐晚生成事实（`build_facts`），
下游的问答、报告、校验都只依赖 `Night.raw` 的字段名。

**接入后端时，只需要把 `load_dataset` 换成"调接口 + 映射"，其余代码不动。**

---

## 3. 目标架构

```text
设备 --蓝牙--> App --上传--> 云端后台 --聚合/统计--> 数据接口
                                                        |
                                                        v
                                        BackendNightSource（适配层，新增）
                                                        |
                                                        v
                                   Night(user_id, raw, history)
                                                        |
                              pipeline -> 事实层 -> 报告 / 问答 / 校验
```

适配层要做四件事：

1. 调接口，拿原始 JSON。
2. 字段映射与单位换算（后端字段名 → 内部字段名）。
3. 缺失值处理：缺字段就不写这个键，**不补 0、不补默认值**。
4. 按 `user_id` 组装 `Night.history`（当前晚之前的夜晚，升序）。

参考实现位置：与 `nightdata.load_dataset()` 同级新增
`load_dataset_from_api(user_id, client)`，返回结构保持 `list[Night]`。

---

## 4. 接口清单

| # | 方法 | 路径 | 用途 | 频率 |
|---|---|---|---|---|
| 4.1 | GET | `/v1/agent/users/{user_id}/nights` | 最近 N 晚摘要（选夜、趋势） | 每次会话首次 |
| 4.2 | GET | `/v1/agent/users/{user_id}/nights/{record_date}` | 单晚完整字段（报告） | 出报告、查细节 |
| 4.3 | GET | `/v1/agent/users/{user_id}/nights/{record_date}/series` | 图表序列（二期） | 用户看趋势图 |
| 4.4 | GET | `/v1/agent/health` | 健康检查 | 探活 |

统一响应信封：

```json
{"code": 0, "message": "ok", "data": {}}
```

`code != 0` 时 `data` 为 `null`，`message` 为可直接展示的中文原因。

### 4.1 最近 N 晚（推荐：返回完整字段）

```http
GET /v1/agent/users/{user_id}/nights?limit=7&before=2026-10-16
Authorization: Bearer <service_token>
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `limit` | 否 | 默认 7，最大 90（30 天汇总最多 31 条） |
| `before` | 否 | 只取该日期（不含）之前的夜晚，用于翻页 |
| `from` / `to` | 否 | 按**日历日期区间**查询（含首尾），如 `from=2026-09-21&to=2026-10-20` |

`limit` 是"最近 N **条记录**"，`from/to` 是"某个**日期区间**"。
用户说"近 30 天"指的是后者：30 天里可能只有 20 晚有数据，
依从性要用 20/30 计算，不能用 20/20。所以周期汇总必须支持区间查询。

**默认返回每晚完整记录**（字段同 4.2）。7 晚 × 40 个字段大约 10 KB，30 晚约 40 KB，
一次取回就能同时支撑单晚问答与多晚趋势，省掉二次请求。

如果后端坚持只给摘要，摘要里必须有支撑趋势的这 5 个字段：
`ahi`、`air_leakage_p95`、`tidal_volume_avg_ml`、`respiratory_rate_avg`、
`minute_ventilation_avg_lpm`。少这几个，趋势模块会退化成"无法给出"。

下面是**摘要模式**的示例（完整字段时直接换成 4.2 的记录即可）：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "719697",
    "total": 7,
    "nights": [
      {
        "record_date": "2026-10-15",
        "treat_time_minutes": 402,
        "treat_score": 84,
        "ahi": 5.7,
        "air_leakage_p95": 18.1,
        "tidal_volume_avg_ml": 452,
        "respiratory_rate_avg": 15.2,
        "minute_ventilation_avg_lpm": 6.9,
        "has_detail": true
      }
    ]
  }
}
```

排序：按 `record_date` **升序**返回；`total` 是符合条件的总晚数（不含分页）。

### 4.2 单晚详情（可选优化）

如果 4.1 已经返回完整字段，这个接口不是必需；它的价值是单晚刷新、
以及历史很深（例如 90 天）时避免一次拉太多。

```http
GET /v1/agent/users/{user_id}/nights/2026-10-15
```

返回第 5 节的完整字段。示例：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "719697",
    "record_date": "2026-10-15",
    "timezone": "Asia/Shanghai",
    "device_id": "A20AUK26090200016",
    "device_model": "YA70",
    "device_type": "BIPAP",
    "firmware_version": "1.3.0",
    "hardware_version": "V1.2",
    "start_time": "2026-10-14T22:41:00+08:00",
    "end_time": "2026-10-15T05:23:00+08:00",
    "treat_time_minutes": 402,
    "treat_score": 84,
    "treat_duration_score": 59,
    "mask_score": 17,
    "resp_score": 8,
    "therapy_mode": "AutoS",
    "mask_type": "nasal",
    "pressure_unit": "Pa",
    "pressure_setting": {"mode": "auto", "min": 400, "max": 800},
    "treat_pressure_avg": 435,
    "treat_pressure_p95": 500,
    "treat_pressure_max": 520,
    "ahi": 5.7,
    "apnea_index": 2.1,
    "hypopnea_index": 3.6,
    "central_apnea_index": 0.7,
    "apnea_count_total": 14,
    "hypopnea_count_total": 24,
    "ca_count_total": 5,
    "apnea_duration_total_sec": 168,
    "hypopnea_duration_total_sec": 204,
    "longest_event_free_minutes": 84,
    "tidal_volume_avg_ml": 452,
    "tidal_volume_p95_ml": 560,
    "respiratory_rate_avg": 15.2,
    "minute_ventilation_avg_lpm": 6.9,
    "minute_ventilation_p95_lpm": 8.4,
    "inspiratory_time_avg_sec": 1.65,
    "inspiratory_ratio_percent": 42,
    "expiratory_ratio_percent": 58,
    "air_leakage_avg": 12.4,
    "air_leakage_p95": 18.1,
    "air_leakage_max": 24,
    "large_leak_minutes": 6,
    "large_leak_percent": 1.5,
    "minute_detail_coverage": "missing",
    "event_detail_coverage": "missing",
    "data_source": "云端聚合"
  }
}
```

### 4.3 图表序列（二期）

后台已经有压力、呼吸事件时长、呼吸频率、漏气量四张趋势图。
Agent **不重画**，只用两种方式之一：

- 后端返回序列，Agent 转述关键拐点；
- 后端直接返回图片 URL，Agent 随回答带回给 App 渲染。

```http
GET /v1/agent/users/{user_id}/nights/2026-10-15/series?metric=pressure&interval=1m
```

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "metric": "pressure",
    "unit": "Pa",
    "interval": "1m",
    "points": [{"t": "2026-10-14T22:41:00+08:00", "v": 420}],
    "quality": {"coverage": 0.98, "gaps": 2},
    "image_url": "https://.../report/2026-10-15/pressure.png"
  }
}
```

`metric` 取值：`pressure`、`leak`、`resp_rate`、`event_duration`。
压力序列同样是 Pa，Agent 侧换算后再转述。

---

## 5. 字段契约

命名一律 **snake_case**。后端若已有 camelCase，由适配层转换，接口层不必改名。

### 5.1 标识与时间

| 字段 | 类型 | 单位 | 级别 | Agent 用途 | 缺失影响 |
|---|---|---|---|---|---|
| `user_id` | string | — | P0 | 用户隔离、审计 | 拒绝处理 |
| `record_date` | string `YYYY-MM-DD` | — | P0 | 选夜、趋势排序、报告标题 | 拒绝处理 |
| `start_time` / `end_time` | string ISO8601 带偏移 | — | P2 | 说明治疗时段 | 只报日期 |
| `device_id` | string | — | P2 | 售后排查 | 不展示 |
| `device_model` | string | — | P2 | 型号说明 | 不展示 |
| `device_type` | string | — | P2 | CPAP/BiPAP 说明 | 不展示 |
| `firmware_version` / `hardware_version` | string | — | P2 | 故障排查 | 不展示 |
| `timezone` | string | — | P1 | 日期口径 | 默认 `Asia/Shanghai` |

### 5.2 使用与评分

| 字段 | 类型 | 单位 | 级别 | 用途 |
|---|---|---|---|---|
| `treat_time_minutes` | number | 分钟 | P0 | 使用时长；<240 分钟标记为碎片记录 |
| `treat_score` | number | 分（百分制） | P1 | 设备总评分 |
| `treat_duration_score` / `mask_score` / `resp_score` | number | 分 | P2 | 评分拆分 |

### 5.3 治疗模式与压力

| 字段 | 类型 | 单位 | 级别 | 用途 |
|---|---|---|---|---|
| `therapy_mode` | string | — | P1 | CPAP / AutoCPAP / S / AutoS；**不能用 device_model 推断** |
| `pressure_setting` | object | **Pa** | P1 | `{mode, min, max}`；判断压力落在区间何处，适配层 ÷100 |
| `treat_pressure_avg` | number | **Pa** | P1 | 平均治疗压力，适配层 ÷100 转 hPa |
| `treat_pressure_p95` | number | **Pa** | P1 | P95 压力（95% 记录点不高于该值，不是最大值），适配层 ÷100 |
| `treat_pressure_max` | number | **Pa** | P2 | 最高压力，适配层 ÷100 |
| `pressure_unit` | string | — | P2 | 固定 `Pa`（已确认）；缺失时按 Pa 处理 |

### 5.4 呼吸事件

| 字段 | 类型 | 单位 | 级别 | 用途 |
|---|---|---|---|---|
| `ahi` | number | 次/小时 | P1 | 核心指标，多晚趋势必用 |
| `apnea_index` / `hypopnea_index` / `central_apnea_index` | number | 次/小时 | P2 | 事件结构 |
| `apnea_count_total` / `hypopnea_count_total` / `ca_count_total` | number | 次 | P2 | 整晚发生次数 |
| `apnea_duration_total_sec` / `hypopnea_duration_total_sec` | number | 秒 | P2 | 累计时长 |
| `longest_event_free_minutes` | number | 分钟 | P2 | 最长无事件时段 |

**次数与指数必须分开**：次数是整晚发生几次，指数是平均每小时频率。
现有导出里次数填充率只有 11%~28%，指数是 100%——缺次数就传 `null`，不要用指数反推。

### 5.5 通气

| 字段 | 类型 | 单位 | 级别 | 用途 |
|---|---|---|---|---|
| `tidal_volume_avg_ml` | number | mL | P1 | 潮气量，趋势必用 |
| `tidal_volume_p95_ml` | number | mL | P2 | 波动上沿 |
| `respiratory_rate_avg` | number | 次/分 | P1 | 呼吸频率，趋势必用 |
| `minute_ventilation_avg_lpm` | number | L/min | P1 | 分钟通气量，趋势必用 |
| `minute_ventilation_p95_lpm` | number | L/min | P2 | 波动上沿 |
| `inspiratory_time_avg_sec` | number | 秒 | P2 | 吸气时间 |
| `inspiratory_ratio_percent` / `expiratory_ratio_percent` | number | % | P2 | 吸呼比，两项相加应≈100 |

### 5.6 漏气

| 字段 | 类型 | 单位 | 级别 | 用途 |
|---|---|---|---|---|
| `air_leakage_avg` | number | L/min | P1 | 平均漏气 |
| `air_leakage_p95` | number | L/min | P1 | P95 漏气，趋势必用 |
| `air_leakage_max` | number | L/min | P2 | 最高漏气 |
| `large_leak_minutes` | number | 分钟 | P2 | 大漏气累计时长 |
| `large_leak_percent` | number | % | P2 | 大漏气占整晚比例 |

### 5.7 数据质量与元信息

| 字段 | 类型 | 取值 | 级别 | 用途 |
|---|---|---|---|---|
| `minute_detail_coverage` | string | `full` / `partial` / `missing` | P2 | 有无分钟级明细；决定能否谈"某时段" |
| `event_detail_coverage` | string | 同上 | P2 | 有无逐条事件明细；决定能否谈事件聚集 |
| `mask_type` | string | `nasal` / `full` / `pillow` | P2 | 面罩类型说明 |
| `data_source` | string | 自由文本 | P2 | 展示"数据来源"，如"云端聚合" |
| `sessions` | array | 见 5.8 | P2 | 一晚多次记录时的明细 |

P2 不是必须，缺了只是少一段说明。其中三项建议优先争取（成本低、影响明显）：
`large_leak_minutes`（面罩密封结论）、`minute_detail_coverage`、
`event_detail_coverage`（后两项只是标记，没有分钟级数据也只需返回 `missing`）。

### 5.8 一晚多次治疗（重要）

现有结构是"一晚一条"。如果后台一晚可能有多段记录（中途停机再开、午睡），
请在聚合记录里给出：

```json
{
  "record_date": "2026-10-15",
  "treat_time_minutes": 402,
  "sessions": [
    {"start_time": "2026-10-14T22:41:00+08:00", "minutes": 300,
     "ahi": 5.2, "air_leakage_p95": 17.4},
    {"start_time": "2026-10-15T04:10:00+08:00", "minutes": 102,
     "ahi": 7.1, "air_leakage_p95": 20.3}
  ]
}
```

`treat_time_minutes` 等顶层字段必须是**整晚聚合值**；不要把多段拆成多条"夜晚"返回，
否则趋势会被重复计数。

### 5.9 级别定义

| 级别 | 含义 | 缺失后果 |
|---|---|---|
| P0 | 没有就无法处理 | 该晚跳过，接口返回 400/404 |
| P1 | 报告核心模块依赖 | 对应模块标注"未提供"，其余照常 |
| P2 | 增强说明 | 仅少一段解释，不影响结论 |

---

## 6. 数据语义硬规则

1. **归属日期**：`record_date` 是治疗夜的归属日，不是上传日。建议按治疗**开始时间**所在自然日（北京时间）归属；跨零点的那一段算前一天。若后台已有既定口径，请在联调前书面确认一次。
2. **时区**：所有时间按 UTC+8 输出并带 `+08:00` 偏移；不要输出本地时间却不带偏移。
3. **单位**：接口压力用 **Pa**（后端已确认），Agent 在适配层 ÷100 转成 hPa 后展示；
   时长分钟、容量 mL、流量 L/min、百分比 0~100。换算只做一次，下游一律 hPa。
   现有导出 CSV 里 435 就是 435 Pa（= 4.35 hPa），不是"缩放值"。
4. **缺失 = null**：字段没有值就传 JSON 的 `null`，或直接不返回该键；
   **不要传 0，也不要传空字符串 `""`**。0 会被当成真实测量值参与计算，
   空字符串会在数值解析时被当成异常值。
5. **次数 ≠ 指数**：不要用 `ahi` 反推次数，也不要拿次数除以时长反推指数。
6. **P95 语义**：约 95% 的有效记录点不高于该值，不是最大值。前端文案也不要写成"最高"。
7. **碎片记录**：`treat_time_minutes < 240` 视为碎片记录，Agent 会在结论里提示"仅供参考"。是否过滤由产品决定，但不要静默丢弃。
8. **一晚一条**：同一 `user_id + record_date` 只能有一条聚合记录；多次治疗放进 `sessions`。
9. **治疗模式**：`therapy_mode` 缺失时传 `null`，不要用设备型号推断成 CPAP/S。
10. **明细覆盖率**：没有分钟级/逐条事件数据时，把 `minute_detail_coverage` / `event_detail_coverage` 设为 `missing`，Agent 就不会去描述"几点钟发生了事件"。
11. **幂等**：同一晚重复请求必须返回同一份数据（含四舍五入后的值），否则对账和缓存都会出问题。
12. **不要下发 PII**：接口里不要放姓名、手机号、地址、身份证；Agent 只需要 `user_id`。

---

## 7. 错误与降级

| 场景 | HTTP / code | Agent 行为 | 用户看到 |
|---|---|---|---|
| 用户无数据 | 200 + 空数组 | 正常，走"暂无记录"话术 | "还没有可用的治疗记录" |
| 日期不存在 | 404 | 不回退到其他夜晚 | "数据里没有 2026-10-19 的记录，最近一次是 2026-10-15" |
| 未授权 / token 失效 | 401 | 不重试，记录告警 | "数据服务暂时不可用，请稍后再试" |
| 越权访问他人 | 403 | 不重试，安全告警 | 同上，且不透露数据是否存在 |
| 限流 | 429 | 退避 1 次 | 同上 |
| 服务异常 | 5xx | 重试 1 次后降级 | 产品问答仍可用，数据类问题说明暂不可用 |
| 超时 | — | 3 秒超时，重试 1 次 | 同上 |
| 字段缺失 | 200 | 按 P1/P2 规则局部降级 | 对应模块写"本次未提供" |

**降级原则**：数据拿不到时，安全边界和产品资料问答继续可用，不整站不可用。

---

## 8. 性能、超时与缓存

| 项 | 建议值 |
|---|---|
| 列表接口 | p95 < 300 ms |
| 单晚接口 | p95 < 500 ms |
| 序列接口 | p95 < 800 ms |
| Agent 侧超时 | 3 s，GET 重试 1 次 |
| 总回答预算 | 8 s（含大模型） |
| 缓存 | key = `user_id + record_date`，TTL 5 分钟；当晚治疗未结束时不要缓存 |
| 缓存失效 | 设备再次上传同一晚时主动失效 |

如果单晚字段较多，宁可一次返回全量，也不要让 Agent 串行调 4 个接口拼数据。

---

## 9. 鉴权与隔离

- Agent 使用**独立服务账号**，只授"读治疗数据"权限，不继承 App 用户 token。
- 请求头：`Authorization: Bearer <service_token>`；可选 `X-Request-Id` 透传链路。
- 后端必须校验 `path.user_id` 与 token 授权范围一致；越权返回 403。
- Agent 侧已做一层校验：`pipeline.answer()` 会拒绝"请求用户与数据用户不一致"的调用。
- Token 轮换：支持双 token 并存至少 24 小时，避免灰度期间中断。

---

## 10. 日志与合规

Agent 侧现状（已实现）：

- 日志只记 `request_id / mode / 意图 / 召回数 / 校验结果 / 耗时`，**不记治疗数据原文**。
- 校验失败时，默认不落模型原文；需要排查时用 `SLEEPAGENT_DEBUG_LLM=1` 写到本机 `.local/debug/`。

后端侧建议：

- 访问日志记 `request_id / user_id 哈希 / 接口 / 状态码 / 耗时`，不记完整响应体。
- 数据保留与删除遵循现有用户协议；Agent 不做二次存储。
- 所有面向用户的结论保留免责声明："仅供设备使用参考，不构成医疗诊断"。

---

## 11. 联调步骤

| 步骤 | 内容 | 完成标志 |
|---|---|---|
| 1 | 后端按第 4、5 节提供 Mock | Agent 能用 Mock 跑通 7 晚列表 + 单晚报告 |
| 2 | 单用户真实联调 | 用真实 `user_id` 取一晚，字段逐个对账 |
| 3 | 对账脚本 | 同一晚 10 项关键指标与后台页面一致（见第 12 节） |
| 4 | 异常演练 | 401/403/404/429/500/超时各演练一次，话术符合第 7 节 |
| 5 | 灰度 | 先内部账号，再小比例用户；观察降级率与 P95 时延 |

建议的 Mock：把 `data/sample_nights.json` 直接包一层信封返回，
后端可以先不实现聚合逻辑，先把字段名对齐。

---

## 12. 对账清单（上线前必须全绿）

| 检查项 | 方法 | 通过标准 |
|---|---|---|
| 字段完整率 | 抽 20 晚，统计 P0/P1 非空率 | P0 = 100%，P1 ≥ 95% |
| 数值一致性 | 同晚对比后台页面 10 项指标 | 完全一致；百分比允许 ±0.1 |
| 压力换算 | 同晚对比后台页面的平均压力 | Pa ÷ 100 后与页面 hPa 一致（435 Pa → 4.35 hPa） |
| 缺失语义 | 找一晚缺 `treat_pressure_max` | 返回 `null`，Agent 不显示 0 |
| 次数/指数 | 找一晚只有指数没有次数 | 不出现"事件 0 次"这类误读 |
| 一晚一条 | 造一晚两段治疗 | 只返回一条记录（一晚一条已确认），`sessions` 可选 |
| 归属日期 | 跨零点治疗 | 归到开始日；与后台报告一致 |
| 越权 | A 的 token 查 B | 403，且不泄露存在性 |
| 幂等 | 同晚连查 3 次 | 三次结果一致 |
| 时延 | 100 次压测 | p95 满足第 8 节 |
| 降级 | 断开数据服务 | 产品问答仍可用，数据问答给出明确提示 |

对账用一个脚本跑：`scripts/reconcile_backend.py`（待建），
输入 `user_id + record_date`，输出逐字段 diff。

---

## 13. 待确认问题（联调前逐条拍板）

已确认（2026-10-20）：

- **一晚一条聚合记录**：不需要 Agent 再按日期合并。
- **缺失 = 治疗时间短、后台未计算**：字段留空（`null`），不填 0。
- **压力单位 Pa**：适配层 ÷100 转 hPa。

仍待确认：

1. **归属日期口径**：按开始时间还是结束时间？跨零点怎么算？（直接影响"昨晚"的语义）
2. **短时阈值与短时记录的处理**：治疗时长低于多少分钟就不计算 AHI/P95 等指标？
   这些短时记录**是否照常返回**（`treat_time_minutes` 有值、其余为 null），
   还是整条过滤？能否带 `insufficient_data: true` 或 `missing_reason`，
   让用户知道"是没算，不是没测到"？
3. **P95 算法**：后台图的 P95 与导出字段是否同一套算法？采样间隔多少？
4. **治疗模式**：`therapy_mode` 是否已经入库？枚举值是什么（CPAP/AutoCPAP/S/AutoS/其他）？
5. **事件明细**：是否有逐条事件（时间、类型、时长）？有的话能否提供，用于"事件分布"分析。
6. **分钟级数据**：压力/漏气/频率曲线是否可按分钟返回？还是只能给图片？
7. **评分**：`treat_score` 及各分项是设备端算的还是云端算的？规则会不会调整？
8. **碎片记录**：少于 240 分钟的记录，产品上要不要展示？Agent 要不要提示？
9. **历史深度**：接口最多能取多少晚？超过 7 晚的趋势是否需要另一套接口？
10. **多设备**：同一用户换过机器时，是否要区分 `device_id`？趋势是否跨设备合并？
11. **删除与撤回**：用户删除数据后，缓存和 Agent 会话里的旧回答如何处理？
12. **内部资料权限**：客服能看到内部技术文档，普通用户不能；接口是否需要区分角色？

---

## 附录 A：现有导出列 → 内部字段

来自 `import_report_export.py` 的 `COLUMN_MAP`（前缀匹配，已处理 BOM）：

| 导出列名 | 内部字段 | 处理 |
|---|---|---|
| 用户ID | `user_id` | 原样 |
| 记录时间 | `record_date` | 取日期 |
| 设备ID | `device_id` | 原样 |
| PiCode | `pi_code` | 原样 |
| 设备类型 | `device_type` | 原样 |
| 固件版本 / 硬件版本 | `firmware_version` / `hardware_version` | 原样 |
| 开始时间 / 结束时间 | `start_time` / `end_time` | 原样 |
| 治疗时长 | `treat_time_minutes` | 数值 |
| 治疗评分 | `treat_score` | 数值 |
| 治疗时长得分 / 面罩得分 / 呼吸事件得分 | `treat_duration_score` / `mask_score` / `resp_score` | 数值 |
| AHI | `ahi` | 数值 |
| 睡眠呼吸暂停指数 AI | `apnea_index` | 数值 |
| 低通气指数 HI | `hypopnea_index` | 数值 |
| 中枢性睡眠呼吸暂停指数 CAI | `central_apnea_index` | 数值 |
| 呼吸暂停次数 / 低通气次数 / 中枢性次数 | `*_count_total` | 数值 |
| 呼吸暂停总持续时间 / 低通气总持续时间 | `*_duration_total_sec` | 数值 |
| 平均治疗压力 / 治疗压力P95 | `treat_pressure_avg` / `treat_pressure_p95` | **Pa ÷ 100 = hPa** |
| 平均潮气量 / 潮气量P95 | `tidal_volume_avg_ml` / `tidal_volume_p95_ml` | 数值 |
| 平均呼吸频率 | `respiratory_rate_avg` | 数值 |
| 平均分钟通气量 / 分钟通气P95 | `minute_ventilation_*` | 数值 |
| 平均吸气时间 | `inspiratory_time_avg_sec` | 数值 |
| 平均漏气 / 漏气P95 | `air_leakage_avg` / `air_leakage_p95` | 数值 |
| 漏气最大持续 / 漏气时间占比 | `large_leak_minutes` / `large_leak_percent` | 数值 |

导出里**没有**、需要后端补的字段：
`treat_pressure_max`、`pressure_setting`、`mask_type`、
`longest_event_free_minutes`、`air_leakage_max`、
`minute_detail_coverage`、`event_detail_coverage`、`therapy_mode`。

---

## 附录 B：字段 → Agent 能力

| 能力 | 依赖字段 |
|---|---|
| 单晚使用情况 | `treat_time_minutes`、`treat_score` |
| 压力解读 | `treat_pressure_avg`、`treat_pressure_p95`、`pressure_setting` |
| 呼吸事件解读 | `ahi`、`*_index`、`*_count_total`、`*_duration_total_sec` |
| 通气概况 | `tidal_volume_avg_ml`、`respiratory_rate_avg`、`minute_ventilation_avg_lpm` |
| 漏气与面罩 | `air_leakage_avg`、`air_leakage_p95`、`large_leak_*` |
| 多晚趋势 | 每晚 `ahi`、`air_leakage_p95`、`tidal_volume_avg_ml`、`respiratory_rate_avg`、`minute_ventilation_avg_lpm` |
| 治疗模式回答 | `therapy_mode`（缺失时只答"未提供"） |
| 事件时段分布 | `event_detail_coverage = full` + 事件明细 |
| 图表引用 | `series.image_url` 或 `points` |

---

## 附录 C：内部对接点（给 Agent 开发）

| 代码位置 | 作用 | 接入时是否要改 |
|---|---|---|
| `sleepagent/nightdata.py::load_dataset` | 目前读本地 JSON | **改为调接口** |
| `sleepagent/nightdata.py::Night` | 内部数据结构 `raw/history` | 不改 |
| `sleepagent/nightdata.py::build_facts` | 生成确定性事实 | 不改 |
| `sleepagent/nightdata.py::resolve_night` | 时间解析（含系统日期口径） | 不改 |
| `sleepagent/pipeline.py::answer` | 路由 / 校验 / 报告 | 不改 |
| `probe_server.py` / `chat.py` | 入口，按 `user_id` 加载 | 只改数据源注入 |

推荐做法：新增 `sleepagent/backends/api_client.py`（纯标准库 `urllib`），
实现 `fetch_nights(user_id, limit)` 与 `fetch_night(user_id, date)`，
再在 `load_dataset` 里做一次开关（本地 JSON / 远程 API），保证离线测试仍可跑。

压力量换算只做一次，放在适配层，下游一律 hPa：

```python
PRESSURE_KEYS = ("treat_pressure_avg", "treat_pressure_p95", "treat_pressure_max")

def to_hpa(record):
    """接口给的是 Pa；÷100 得到 hPa。只转换一次，幂等。"""
    for key in PRESSURE_KEYS:
        value = record.get(key)
        if isinstance(value, (int, float)):
            record[key] = round(value / 100.0, 2)
    setting = record.get("pressure_setting") or {}
    for key in ("min", "max"):
        value = setting.get(key)
        if isinstance(value, (int, float)):
            setting[key] = round(value / 100.0, 2)
    record["pressure_setting"] = setting
    record["pressure_unit"] = "hPa"
    return record
```

如果把已经转换过的记录再转一次，会得到 0.04 hPa 这种值——所以转换点只能有一处。
