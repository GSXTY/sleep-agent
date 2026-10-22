# 语义查询规划层

## 1. 目标

不同用户会用不同说法表达同一需求，例如：

- 我这一周的总分变化
- 最近七天睡眠评分怎么样
- 这周设备分和之前比呢
- 为什么我的分数掉了

查询规划层把这些说法归一成同一份结构化计划，再交给确定性代码执行。

## 2. 分层

```text
安全边界
→ 范围/日期确认
→ 规则快速规划
→ LLM 受限语义规划（仅低置信度）
→ 计划校验
→ 数据查询 / RAG / 报告
→ 确定性计算
→ LLM 仅组织语言
```

## 3. 查询计划

```json
{
  "intent": "analyze_change",
  "metrics": ["treatment_score"],
  "window": "week",
  "window_days": 7,
  "confidence": 0.95,
  "source": "llm"
}
```

允许的 `intent`：

- `explain_metric`
- `device_value`
- `analyze_change`
- `analyze_trend`
- `compare_periods`
- `compare_nights`
- `report`
- `app_report_insight`
- `product_query`
- `general`
- `clarify`
- `out_of_scope`

允许的指标包括设备总分、三个评分分项、AHI、使用时长、压力、漏气、
潮气量、呼吸频率、分钟通气量和 `overall`。

允许的时间窗口为 `day / week / month / context / unspecified`。

## 4. 规则与 LLM 的边界

规则快速路径负责：

- 安全红线；
- 明确的产品操作和故障问题；
- 明确的指标定义；
- 明确日期和高频固定问法；
- LLM 不可用时的完整降级。

LLM 规划器负责：

- 同义表达和新问法；
- 从一句话中识别动作、指标和时间窗口；
- 在规则置信度不足时给出候选结构化计划。

LLM 不负责：

- 生成或补全治疗数据；
- 计算平均值、趋势和分项差值；
- 判断疾病、严重程度或是否调压；
- 绕过安全边界。

## 5. 校验和降级

- LLM 只能返回固定 JSON Schema；
- 未知 intent、metric、window 会被拒绝；
- 置信度低于 0.60 会被拒绝；
- LLM 失败时回退规则计划；
- 计划仍不明确时询问用户，不直接猜。

## 6. 典型路由

| 问法 | 计划 |
|---|---|
| 时长得分是什么 | `explain_metric / duration_score` |
| 我这一周的总分变化 | `analyze_change / treatment_score / week` |
| 最近30天 AHI 趋势 | `analyze_trend / ahi / month` |
| 上一次报告和前一次相比 | `compare_nights` |
| 生成报告 | `report`，然后确认单晚/7天/30天 |
| App 报告里的漏气 | `app_report_insight / leak` |
