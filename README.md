# 睡眠呼吸产品助手（Windows 命令行版）

从原 Mac 项目 `sleep_ai_assistant` 重构而来，以 Windows 为基准环境。

**只用 Python 标准库**：不需要 pip install，不需要联网（接云端模型时除外），不需要网页。

默认行为是一个通用助手，遇到产品相关的问题会去查产品资料再回答。

---

## 运行

需要 Python 3.10 或以上。

```powershell
cd <本目录>
python chat.py
```

```
你 > 滤芯多久换一次
你 > 推荐一部电影
你 > E3 报错怎么处理
你 > 我的平均压力是多少
```

单次提问后退出：

```powershell
python chat.py "滤芯多久换一次"
python chat.py --no-llm "面罩怎么清洗"     # 强制规则版
python chat.py --report                     # 单晚治疗报告
```

跑自检：

```powershell
python -m unittest discover -s tests -v
```

当前状态：198 项测试全部通过。

交付文档：

- [版本说明](docs/VERSION_V1.md)
- [测试报告](docs/TEST_REPORT_V1.md)
- [接口契约](docs/INTERFACE_CONTRACT_V1.md)

---

## 四种模式

按下面的顺序判定，先命中的先执行：

| 顺序 | 模式 | 触发条件 | 回答方式 |
|---|---|---|---|
| 1 | **安全边界** | 红旗症状 / 要求调压 / 要求诊断 / 要求停用 | 固定话术，**不经过大模型** |
| 2 | **系统信息** | 问日期、星期、时间 | 由程序直接给出本机时间，**不经过大模型** |
| 3 | **报告生成** | 说「出个报告」「整体情况怎么样」 | 确定性报告，不经过大模型 |
| 4 | **产品文档** | 查到产品资料，且问题是「怎么做」型 | 依据资料回答并注明出处 |
| 5 | **设备数据** | 命中指标主题且事实层有数据 | 给出本晚真实数值 |
| 6 | **指标解释** | 只命中知识条目 | 指标含义 + 限制 + 建议 |
| 7 | **通用对话** | 其余问题 | 直接交给大模型 |

如果输入的是与已知型号只差一个字符的型号，例如 `Y70`、`Y70A`，系统会先进入
“型号确认”，询问是否指 `YA70` 或 `YA70A`，不会直接拿错误型号去检索产品资料。

判定顺序里有几个刻意的安排：

- **日期时间由程序回答，不问模型。** 大模型没有实时时钟，问它今天几号它会编一个日期。
  同时每次回答都会把当前日期注入提示词，让「最近」「上周」这类相对时间可用。
- **安全边界永远第一**。「你好，帮我把压力调高」仍然会被拦下，不会被问候逻辑带走。
- **产品「怎么做」型问题优先查资料**，否则「面罩多久换」会被设备数据抢走，答成漏气数值。
- **通用对话也走大模型**，但提示词里明确禁止编造产品细节（部件周期、错误码、保修政策等），
  资料没覆盖就说明未覆盖并建议联系客服。

---

## 放产品资料

`product_docs/` 分三类，重启即生效，输入 `/docs` 查看加载情况：

| 目录 | 用途 | 是否参与用户问答 |
|---|---|---|
| `product_docs/user/` | **对外资料**：官方说明书、从技术要求整理的规格 | 是 |
| `product_docs/internal/` | 内部资料：注册用技术要求原件 | 否 |
| `product_docs/examples/` | 早期示例文档（占位内容） | **否** |

`examples/` 里的内容是为演示编的（错误码、同步步骤、耗材周期都不是真实参数），
真实说明书到位后已移出检索范围，避免和真实资料竞争。

支持的格式：`.md` / `.txt` / `.docx`（docx 只用标准库解析，表格会还原成「单元格 \| 单元格」的行）。

### 当前语料与基线

对外 97 个切片（说明书 79 + 规格 18）。检索实测（22 条真实问题）：

| 方案 | R@1 | R@3 | MRR |
|---|---|---|---|
| 混合（BM25 0.3 + 向量 1.0，当前） | **95%** | **100%** | **0.977** |
| 纯关键词 | 68% | 91% | 0.780 |
| 纯向量 | 91% | 100% | 0.955 |

替换或新增文档后请重跑 `python eval_retrieval.py`。

---

## 检索方式：关键词 + 向量混合

默认使用 **Ollama + bge-m3 的混合检索**。未启动 Ollama、模型未下载或索引不可用时，
会自动退回纯关键词检索，对话服务不中断。

配置向量模型后，`product_kb.retrieve()` 自动变成**混合检索**：
稀疏（关键词）+ 稠密（向量）两路各召回 10 条，用 RRF 按排名融合，取前 3。
向量不可用时静默退回纯关键词，行为与改造前完全一致。

```powershell
ollama pull bge-m3
```

`config.json` 加一段：

```json
"embedding": {
  "provider": "ollama",
  "model": "bge-m3",
  "base_url": "http://127.0.0.1:11434"
}
```

或用环境变量：

```powershell
$env:LLM_EMBEDDING_PROVIDER = "ollama"
$env:LLM_EMBEDDING_MODEL    = "bge-m3"
```

### 改完检索逻辑必须跑评估

```powershell
python eval_retrieval.py              # 四组对比
python eval_retrieval.py --verbose    # 附未命中清单
```

当前基线（无向量）：纯关键词 R@1 68%、R@3 91%、MRR 0.780。
**任何改动让这个数字下降都应当回退。**

`provider=fake` 是确定性哈希向量，只用于验证「索引→检索→融合→降级」链路，
没有语义能力，**不要用它评估效果**。

---

## 一键验证

```powershell
python run_checks.py              # 单元测试 + 检索评估
python run_checks.py --verbose    # 附检索未命中清单
```

当前：198 项测试通过，混合检索 R@1 95% / R@3 100%。
**任何改动后都要跑一次，并核对用例数量**——数量对不上说明有测试没被收集到。

---

## 代码结构

```
chat.py                 命令行入口（只做参数解析与展示）
sleepagent/pipeline.py  对话管线：路由 -> 事实/检索 -> 模型 -> 校验
sleepagent/
  safety.py             安全边界（红旗/调压/诊断/停用/用药）
  runtime_info.py       日期时间（程序直答，注入提示词）
  social.py             问候、能力询问、报告触发
  router.py             意图与主题识别
  product_kb.py         产品资料加载与混合检索入口
  bm25.py               BM25 稀疏检索
  embeddings.py         向量化客户端
  vector_index.py       向量索引
  knowledge.py          指标知识库
  nightdata.py          确定性事实层
  formatting.py         统一数值与缺失值格式化
  report.py             单晚报告（文本 + 结构化）
  acceptance.py         报告验收
  validation.py         输出校验（三档强度）
  llm.py                大模型客户端
  logging_config.py     终端 + logs/agent.log 轮转日志
```

入口层只做转发，所有业务判定都在 `pipeline.py` 与各模块里，
以后加 FastAPI 入口不会出现两套行为。

### 三档校验强度

| 模式 | 强度 | 检查内容 |
|---|---|---|
| 设备数据 / 报告生成 | strong | 数字白名单 + 医疗红线 + 诊断表述 + P95 语义 |
| 产品文档 / 指标解释 | medium | 数字白名单 + 医疗红线 |
| 通用对话 | weak | 仅医疗红线 |

### 可调项

| 文件 | 内容 |
|---|---|
| `synonyms.json` | 口语说法 → 文档用词，改完重启生效 |
| `sleepagent/retrieval_constants.py` | 检索参数（章节加成 / 覆盖闸门 / RRF 权重）。**改前必须跑 `eval_retrieval.py`**，默认值有测试锁定 |
| `config.json` | 大模型与向量模型配置 |
| `data/sample_nights.json` | 7 晚样板数据 |
| `.local/report_export.path` | 真实导出文件路径 |

### 日志

```powershell
python chat.py --verbose      # 输出路由、命中数、降级、校验不通过等日志
```

每次请求固定输出：

```text
[时间] [请求ID] [模式] [意图] 召回:N 模型:是/否 校验:通过/未通过 耗时:Nms
```

异常场景在请求ID后增加统一前缀：

```text
[SAFETY_BLOCK] 安全边界拦截
[VALIDATION_FAIL] 模型输出校验失败并回退
[DEGRADE] 向量服务或模型不可用，进入降级路径
```

日志只记模式、意图、命中数量与校验结果，不打印用户治疗数据原文。

日志会同时写入 `logs/agent.log`。文件默认记录 INFO 及以上的请求日志，
单个文件达到 5 MB 后自动轮转，保留 5 个历史文件。终端默认只显示 WARNING，
使用 `--verbose` 时显示完整 INFO 日志。

---

## 数据模型：多条治疗夜

数据是**一组治疗夜**（不是单晚），每题按提问里的时间词自动挑：

| 你这么说 | 取哪一晚 |
|---|---|
| 昨晚使用情况 / 昨天用了多久 | 最近一晚 |
| 前天用了多久 | 往前第 2 晚 |
| 大前天 | 往前第 3 晚 |
| 2026-10-11 的情况 / 10月13日怎么样 | 指定日期 |
| 没写时间 | 默认最近一晚 |

生成报告时用**该晚 + 它之前的夜晚**（最多 7 晚）算趋势——不会用未来的数据。

数据文件是 `data/sample_nights.json`，结构：

```json
{ "data_source": "...", "nights": [ { 第 1 晚 }, { 第 2 晚 }, ... ] }
```

也兼容旧形态（单个夜晚对象、或带内部 history 的单晚文件）。回答的元信息里会写明用的是哪一晚。

---

## 命令

| 命令 | 作用 |
|---|---|
| `/docs` | 列出已加载的产品资料 |
| `/report` | 生成单晚治疗报告（样板数据） |
| `/facts` | 查看本晚确定性事实，每条带来源字段与计算方式 |
| `/knowledge` | 列出指标解释条目 |
| `/safety 问题` | 只做安全判定，看命中哪一档 |
| `/llm` | 查看大模型配置；`/llm on` `off` `test` |
| `/status` | 查看聊天模型、向量模型、索引和降级状态 |
| `/clear` `/help` `/quit` | 清空历史 / 帮助 / 退出 |

---

## Pipeline 内部契约

`sleepagent/pipeline.py` 现在用 `UserQuery` 和 `AgentResponse` 作为内部标准结构。
这是给 CLI、HTTP 探针和未来 App 共用的边界，不等同于最终后端接口：

- `UserQuery` 固定问题、上下文、是否启用模型和预留的 `user_id`。
- `AgentResponse` 固定正文、模式、请求 ID、来源、校验等级、校验结果、风险标记和结构化数据。
- `nightdata` 的数据加载、选夜、事实和趋势函数都接收 `user_id`，默认 `DEMO-USER`。
- 真实后台字段变化时，由入口适配层映射到 `UserQuery`，不把业务判断写回 CLI 或 HTTP 层。

---

## 结构化报告契约

单晚报告的稳定结构在 `contracts/structured_report_sample.json`：

- `schema_version`、`report_type` 固定报告契约版本。
- `rating` 是总体评级。
- `core_metric_cards` 是可直接渲染的四项指标卡，缺失值保持 `value=None`、`provided=false`。
- `modules` 按压力、呼吸事件、呼吸概况、漏气四个模块返回标题和正文。
- `summary`、`acceptance`、`missing_fields`、`disclaimer` 一并返回。

`tests/test_structured_report_contract.py` 会比对样例与运行时输出的字段名、层级和类型。

---

## 接入真实报告数据

如果拿到的是「N天报告导出」CSV（每行一个治疗夜），用转换层脚本生成单晚报文：

```powershell
python import_report_export.py --list                    # 列出所有夜晚
python import_report_export.py --date 2026-09-22         # 按治疗夜取
python import_report_export.py --latest                  # 最近一个可用夜晚
python import_report_export.py --index 12 --export-json ".local\real_night.json"
```

常用导出路径写到 `.local/report_export.path` 后，就不用每次传 `--file`。

**真实数据不进仓库**（沿用原项目约定）：原始 CSV 与派生契约都放 `.local/` 下，已被 `.gitignore` 排除。

转换层会做三件事：字段映射、**压力值 /100**（导出列名写 hPa，实际是原始缩放值）、
缺失字段显式标注。治疗时长低于 240 分钟的记录会给出碎片警告。

---

## 接入大模型

不接也能用：产品问题会直接给出资料原文，指标问题给出知识条目。接上之后回答会自然很多。

### DeepSeek（已预填）

本目录的 `config.json` 已按 DeepSeek 填好，只需要把 `api_key` 换成你的：

```json
{
  "provider": "openai",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key": "sk-你的key"
}
```

启动后先输入 `/llm test` 确认连通。密钥在界面上只显示掩码，不会明文打印。

### 其他 OpenAI 兼容接口

用环境变量（PowerShell）：

```powershell
$env:LLM_PROVIDER = "openai"
$env:LLM_BASE_URL = "https://api.deepseek.com/v1"
$env:LLM_MODEL    = "deepseek-chat"
$env:LLM_API_KEY  = "sk-你的key"
python chat.py
```

注意：只要设置了 `LLM_PROVIDER`，程序就会忽略 `config.json`，
避免出现「provider 是 ollama 却用着 DeepSeek 地址」这种错配。

### 本地 Ollama

```powershell
# 安装 Ollama 后
ollama pull qwen3:1.7b
python chat.py
```

清除 `LLM_PROVIDER` 环境变量并删掉 config.json 即可回到本地模型。

---

## 校验闸门

模型输出不会直接给用户，必须先过 `validation.py`。**校验强度跟着模式走**：

| 检查项 | 产品文档 / 设备数据（grounded） | 通用对话（general） |
|---|---|---|
| 数字白名单 | 检查：输出里的数字必须在材料里出现过 | 不检查 |
| 诊断式表述 | 检查（你得了 / 属于重度 / 诊断为…） | 只检查「确诊为 / 诊断为」 |
| 调压建议 | 检查 | 检查 |
| P95 语义错误 | 检查 | 不检查 |
| 长度上限 | 1200 字 | 1200 字 |

为什么分模式：产品回答必须严格可追溯，编造部件周期是硬伤；
而「一加一等于 2」这种日常回答里的数字本来就无法逐一追溯，硬套白名单会把所有通用回答全部拦掉。

扫描数字前会先把 `P95` 替换成文字，否则里面的 95 会被当成凭空出现的数字。

回退不是静默的，回答下方会写明原因，例如：

```
[模式] 产品文档（规则版） | 模型输出未通过校验，已回退：出现材料中没有的数字：45
```

---

## 实现过程记录

结构梳理、关键设计决策、以及**实现过程中被真实数据暴露并修掉的 23 个问题**，
都写在 [实现过程记录.md](实现过程记录.md) 里。排查同类问题时可以先看那份。

---

## 目录结构

```
chat.py                     命令行入口与四模式管线
import_report_export.py     报告导出 CSV -> 内部契约 的转换层
实现过程记录.md              结构说明与问题记录
.local/                     真实数据与路径指针（已 gitignore）
product_docs/               产品资料（放 .md / .txt / .docx）
config.json                 大模型配置（含密钥，已加入 .gitignore）
sleepagent/
  safety.py                 安全边界：红旗 / 调压 / 诊断 / 停用
  social.py                 对话层：问候、能力询问、报告触发
  router.py                 意图与主题识别
  product_kb.py             产品文档加载与检索（含 docx 解析）
  knowledge.py              指标知识库检索（复用原项目 base.json）
  nightdata.py              确定性事实层
  report.py                 单晚报告生成
  acceptance.py             报告验收（复用原项目验收合同）
  validation.py             模型输出校验（分模式）
  runtime_info.py           当前日期时间：程序直答 + 注入提示词
  llm.py                    大模型客户端：Ollama + OpenAI 兼容
  logging_config.py         日志配置：终端 + logs/agent.log
knowledge/base.json         原项目的指标知识库，未改动
contracts/report_analysis_acceptance.json   原项目的报告验收合同，未改动
data/sample_night.json      样板数据（非真实病例）
tests/                      自检
```

---

## 已知限制

- **产品检索是启发式关键词匹配，不是向量检索。** 实测（含 YA70 产品规格）：
  产品问题 19 个命中 17 个，通用问题 7 个 0 误判。
  还有两个漏检（如「登录后别人能看到我的数据吗」），原因是问题词数与文档用词差异较大，
  重合覆盖率不够。这类问题需要靠维护 `SYNONYMS` 同义词表或换 embedding 解决。
- 中文按 bigram 切词会产生跨词边界的假 token（「今天天气怎么样」里的「气怎」
  会撞上「喘不上气怎么办」），已用停用词表 + 内容词重合覆盖率挡住，但不是万无一失。
- 通用对话回退时只给一句说明，没有兜底内容——因为通用问题本来就没有确定性答案。
- 样板设备数据是构造的，不是真实病例。
- 这仍然不是医疗器械功能：不下诊断、不判严重程度、不给调压建议。

---

## 启动脚本

本目录下有一个 `run.cmd`，双击即可启动。它会自动把代码页切到 UTF-8 并切到本目录，
避免中文乱码和路径问题。也可以命令行使用：

```
run.cmd
run.cmd "滤芯多久换一次"
```

如果不使用脚本而直接运行 `python chat.py` 出现中文乱码，先执行一次 `chcp 65001`。
