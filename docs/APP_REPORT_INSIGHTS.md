# App 报告解读接入说明

## 1. 定位

App 负责图表展示，Agent 负责读取同一套后端结构化数据并生成文字解读。
Agent 不识别报告截图，也不重复画图。

入口模式名：`App报告解读`

```text
解读一下我的App报告
App报告里的漏气怎么理解
为什么我的评分下降了
App报告里的AHI和呼吸事件说明什么
```

## 2. 板块

| `module` | App 板块 | 主要字段 |
|---|---|---|
| `score` | 睡眠评分 | `treat_score`、时长/面罩/呼吸事件得分 |
| `events` | 呼吸事件 | AHI、AI、HI、CAI、事件次数 |
| `breathing` | 呼吸概况 | 潮气量、呼吸频率、分钟通气量、吸呼占比 |
| `leak` | 漏气与面罩密封 | 平均/P95/最高漏气、大漏气时长 |
| `pressure` | 治疗压力 | 平均/P95/最高压力、压力设定范围 |

## 3. `AgentResponse.structured`

分析、趋势和 App 报告解读会返回同一类结构：

```json
{
  "schema_version": "1.0",
  "report_type": "app_report_insight",
  "record_date": "2026-10-15",
  "data_source": "样板数据（非真实病例）",
  "selected_module": "leak",
  "window": {
    "days": 30,
    "first_date": "2026-10-09",
    "last_date": "2026-10-15",
    "record_count": 7,
    "observed_days": 7
  },
  "summary": {
    "level": "attention",
    "conclusion": "需要回看的板块：漏气与面罩密封。"
  },
  "insights": [
    {
      "module": "leak",
      "title": "漏气与面罩密封",
      "definition": "漏气指标反映面罩与管路在治疗过程中的漏气记录。",
      "level": "attention",
      "conclusion": "本晚平均漏气 12.4 L/min，P95 漏气 18.1 L/min。",
      "evidence": [
        {
          "source_field": "air_leakage_p95",
          "label": "P95 漏气",
          "value": 18.1,
          "unit": "L/min",
          "display": "18.1 L/min",
          "provided": true
        }
      ],
      "suggestion": "可检查面罩贴合、头带松紧和管路接口。",
      "source_fields": ["air_leakage_p95"]
    }
  ],
  "data_quality": {
    "gap_count": 0,
    "missing_days": 0,
    "fragment_count": 0
  },
  "source_fields": ["air_leakage_p95"],
  "disclaimer": "不构成医疗诊断，也不建议自行调整治疗参数。"
}
```

## 4. 等级约定

`level` 只有三种：

| 值 | 含义 | App 展示建议 |
|---|---|---|
| `info` | 有数据，未触发需要回看的规则 | 普通文本 |
| `attention` | 记录变化、碎片夜、大漏气或压力达到设定上限，需要回看 | 中性强调色 |
| `data_gap` | 字段不足，不能完成该板块解读 | 灰色缺省/补数据提示 |

`attention` 不是疾病异常，不使用“正常/异常/严重”结论。

评分板块除了总分，还会返回 `breakdown`：

- `components`：时长、面罩、呼吸事件三个分项的上一晚/本晚分数、差值、
  同晚证据、含义解释和建议；
- `window_summary`：评分窗口平均值、范围、走势和波动；
- `period_comparison`：近期与此前阶段均值；
- `suggestions`：根据分项和对应指标生成的低风险建议。

## 5. App 接入方式

App 继续调用现有 `/api/chat`，发送：

```json
{"question": "App报告里的漏气怎么理解", "user_id": "USER-001"}
```

读取响应中的 `structured`。如果 `structured.report_type` 为
`app_report_insight`，直接根据 `insights[].module` 放置到对应报告板块。

App 不需要解析正文 Markdown；正文用于无障碍朗读、复制和降级展示。

## 6. 数据一致性要求

- 后端返回的指标值和单位就是 App 图表使用的值，Agent 不再做第二套口径；
- 压力如果后端使用 Pa，必须在入口适配层统一除以 100 转成 hPa；
- 缺失值必须保持 `null`，不能补 0；
- 同一用户只能读取自己的数据，鉴权必须在后端完成；
- 时间窗口由 Agent 请求参数决定，App 与 Agent 应使用相同的 `from/to`。
