# 后端数据对接 

---

## 一、

Agent 需要按 `user_id` 读取用户最近几晚的治疗记录，用来回答"昨晚怎么样"、
生成单晚报告。**只读，不写，不改设备参数。**

---

## 二、需要一个接口（MVP）

```http
GET /v1/agent/users/{user_id}/nights?limit=7
Authorization: Bearer <service_token>
```

| 项 | 约定 |
|---|---|
| 返回 | 最近 7 晚，按 `record_date` 升序，**一个日期一条（已确认）** |
| 字段 | 每晚完整字段（见第三节） |
| 体积 | 7 晚 × 40 字段 ≈ 10 KB，不分页 |
| 鉴权 | 服务账号 Token，只授"读治疗数据" |
| 调用频率 | 每次会话首次拉一次，本地缓存 5 分钟 |

一次调用同时覆盖单晚问答和多晚趋势，所以**先做这一个就够**。
单晚详情、图表序列都是后续优化，不阻塞联调。

> **为"近 30 天汇总"预留一个参数**：请支持 `from` / `to` 日期区间查询。
> `limit=30` 是"最近 30 条记录"，用户说的"近 30 天"是日历区间——
> 有缺勤时两者差很多（30 天里可能只有 20 晚有数据），依从性要用 20/30 算。

---

## 三、字段清单

### P0：必须

| 字段 | 类型 | 说明 |
|---|---|---|
| `user_id` | string | 用户标识 |
| `record_date` | string `YYYY-MM-DD` | 治疗夜归属日期 |
| `treat_time_minutes` | number | 治疗时长（分钟） |

### P1：报告核心模块依赖

| 字段 | 单位 | 用在哪儿 |
|---|---|---|
| `treat_score` | 分（百分制） | 总体评分 |
| `therapy_mode` | — | 治疗模式（CPAP/AutoCPAP/S/AutoS） |
| `pressure_setting` | **Pa** | `{mode, min, max}`，判断压力区间 |
| `treat_pressure_avg` / `treat_pressure_p95` | **Pa** | 压力解读 |
| `ahi` | 次/小时 | 呼吸事件核心指标 |
| `tidal_volume_avg_ml` | mL | 通气概况 |
| `respiratory_rate_avg` | 次/分 | 通气概况 |
| `minute_ventilation_avg_lpm` | L/min | 通气概况 |
| `air_leakage_avg` / `air_leakage_p95` | L/min | 漏气与面罩 |

> 多晚趋势依赖每晚的这 5 个：`ahi`、`air_leakage_p95`、
> `tidal_volume_avg_ml`、`respiratory_rate_avg`、`minute_ventilation_avg_lpm`。

### P2：增强说明，缺了只是少一段话

```
start_time / end_time / device_id / device_model / device_type /
firmware_version / hardware_version / mask_type / timezone
treat_duration_score / mask_score / resp_score
treat_pressure_max / pressure_unit
apnea_index / hypopnea_index / central_apnea_index
apnea_count_total / hypopnea_count_total / ca_count_total
apnea_duration_total_sec / hypopnea_duration_total_sec / longest_event_free_minutes
tidal_volume_p95_ml / minute_ventilation_p95_lpm
inspiratory_time_avg_sec / inspiratory_ratio_percent / expiratory_ratio_percent
air_leakage_max / large_leak_minutes / large_leak_percent
minute_detail_coverage / event_detail_coverage / data_source / sessions
```

P2 **不是必须**：缺了只是少一段说明，四个核心模块照常出结论。
下面三项建议优先争取，成本低、对回答质量影响明显：

| 字段 | 用来干什么 | 没有会怎样 |
|---|---|---|
| `large_leak_minutes` | 面罩密封结论："大漏气累计 6 分钟" | 只能说平均漏气和 P95，说不出有没有短时大漏气 |
| `minute_detail_coverage` | 标记有没有分钟级明细（`full`/`partial`/`missing`） | 不能谈"凌晨两点压力升高"这类时段结论 |
| `event_detail_coverage` | 标记有没有逐条事件明细 | 不能谈"事件集中在哪个时段" |

`large_leak_minutes` 现有导出里已经有（「漏气最大持续」，配套还有「漏气时间占比」
对应 `large_leak_percent`），直接带上即可；后两项是覆盖标记，就算没有分钟级数据，
返回 `"missing"` 即可，不需要新做统计。

字段命名用 snake_case；后端现有 camelCase 的话，我们适配层转，不用改库。

---

## 四、已确认

| # | 口径 | 状态 | 我们这边怎么处理 |
|---|---|---|---|
| 1 | 一晚一条聚合记录 | **已确认** | 直接按 `record_date` 映射，不做二次聚合；`sessions` 可选 |
| 2 | 缺失 = 治疗时间短、后台没算出来，就空着 | **已确认** | 空值用 JSON 的 `null`（不是 `""`、不是 0）；报告写"未计算/未提供"，不推测 |
| 3 | 压力单位是 **Pa** | **已确认** | 适配层 ÷100 转成 hPa 再进报告（435 Pa → 4.35 hPa），`pressure_setting` 同理 |

第 3 条要知道的后果：如果直接把 Pa 当 hPa 用，报告会写成"平均压力 435 hPa"，
比真实值大 100 倍。换算只做一次，在适配层做，下游一律 hPa。

---

## 五、返回示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "719697",
    "total": 7,
    "nights": [
      {
        "user_id": "719697",
        "record_date": "2026-10-15",
        "timezone": "Asia/Shanghai",
        "treat_time_minutes": 402,
        "treat_score": 84,
        "therapy_mode": "AutoS",
        "pressure_unit": "Pa",
        "pressure_setting": {"mode": "auto", "min": 400, "max": 800},
        "treat_pressure_avg": 435,
        "treat_pressure_p95": 500,
        "treat_pressure_max": 520,
        "ahi": 5.7,
        "tidal_volume_avg_ml": 452,
        "respiratory_rate_avg": 15.2,
        "minute_ventilation_avg_lpm": 6.9,
        "air_leakage_avg": 12.4,
        "air_leakage_p95": 18.1,
        "minute_detail_coverage": "missing",
        "event_detail_coverage": "missing",
        "data_source": "云端聚合"
      }
    ]
  }
}
```

- `code = 0` 表示成功；非 0 时 `data` 为 `null`，`message` 给中文原因。
- 某字段没有数据就写 JSON 的 `null`，或者干脆不返回这个键；
  **不要用空字符串 `""`**，空字符串会被当成"有值但异常"。
- 空数组（`nights: []`）表示这个用户还没有记录，不是错误。

---

## 六、口径确认表

已确认的不用再讨论；剩下的每条都给了我们的建议，同意就回"按建议"。

| # | 问题 | 状态 / 建议 | 影响 |
|---|---|---|---|
| 1 | 一晚一条 | ✅ 已确认 | — |
| 2 | 缺失留空 | ✅ 已确认 | 我们按 `null` 处理，不填 0、不用空字符串 |
| 3 | 压力单位 Pa | ✅ 已确认 | 适配层 ÷100 转 hPa |
| 4 | `record_date` 按开始还是结束归属？ | 待确认；应该是开始吧 | 决定"昨晚"指哪一晚 |
| 5 | 短于多少分钟就不计算 AHI、漏气这些指标？**这些短时记录还会不会返回？** | 待确认；建议照常返回（有时长、其余为 null），并给阈值或 `insufficient_data: true` | 整条被过滤的话，用户问"昨晚用了多久"就查不到 |
| 6 | 有没有 `therapy_mode`？枚举值是什么？ | 待确认；有则按 CPAP/AutoCPAP/S/AutoS 输出 | 没有就只能回答"未提供" |
| 7 | P95 算法与后台图表是否同源？采样间隔多少？ | 待确认；建议同源 | 否则报告和页面数字对不上 |
| 8 | 有没有分钟级曲线、逐条事件明细？ | 待确认；有就给序列或图片 URL，没有把 coverage 标 `missing` | 决定能不能谈"几点发生" |
| 9 | 单个用户最多能取多少晚？ | 待确认 | 影响趋势窗口 |
| 10 | Token 怎么发、怎么轮换？ | 待确认；建议独立服务账号、只读、双 token 并存 24 小时 | 影响灰度与上线 |
| 11 | 能否按日期区间查（`from` / `to`）？ | 待确认 | 不加的话，"近 30 天"只能退化成"最近 30 条记录" |

---

## 七、确认

- 只读接口，Agent 不写回任何治疗数据。
- 按 `user_id` 隔离，越权请求由我们和你们两侧共同拦截。
- 日志不落治疗数据原文，只记请求 ID、模式、耗时。
- 数据服务不可用时，产品资料问答仍可用，数据类问题明确提示"暂不可用"。
- 单接口 3 秒超时、失败重试 1 次，不放大后端压力。

---

## 八、联调需要的四样东西

| # | 交付物 | 用途 |
|---|---|---|
| 1 | 测试环境 `base_url` + 服务 Token | 我们接适配层 |
| 2 | 一个真实 `user_id`（有 7 晚数据） | 跑通报告与趋势 |
| 3 | Mock（样板 JSON 包一层信封即可） | 后端没做完时我们先联调 |
| 4 | 一晚的后台页面截图/数值 | 用来对账（10 项关键指标） |

---


