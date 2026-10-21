# App 内嵌 Demo 指南

目标：用最小成本让评审同事在手机 App 里试用一个真实的 Agent，
评估通过后再替换成原生聊天页。**后端接口不变**。

---

## 一、Demo 是什么

```text
手机 App（WebView）
     │ 加载 http://<电脑IP>:8100/
     ▼
demo_server.py
  ├─ 托管 H5 聊天页（demo_web/index.html）
  ├─ POST /api/chat      问答、候选按钮、报告卡片
  ├─ POST /api/feedback  👍/👎 反馈落盘
  └─ GET  /api/health
     │
     ▼
pipeline（路由 / RAG / 安全边界 / 校验）
```

Demo 使用样板数据 `DEMO-USER`，不连接真实用户数据。

---

## 二、启动

**最简单：双击项目根目录的 `demo.cmd`。**
窗口会显示本机地址和手机访问地址，保持窗口打开即可。

换端口：`demo.cmd 8101`。

命令行方式：

```powershell
cd D:\VVFLY\sleep-agent-win
python demo_server.py
```

浏览器打开 `http://127.0.0.1:8100` 即可试用。

手机与电脑在同一局域网时：

```powershell
python demo_server.py --host 0.0.0.0
```

手机访问 `http://<电脑局域网IP>:8100/`。
Windows 防火墙需要放行 8100 端口。

> `demo.cmd` 内部只使用英文提示，并保存为 ASCII + CRLF。
> 这是为了避开 cmd.exe 按系统代码页解析批处理时的中文乱码问题；
> 中文地址和日志由 Python 输出，不受影响。

---

## 三、App 里怎么嵌

### Android（Kotlin）

```kotlin
val webView = findViewById<WebView>(R.id.webView)
webView.settings.javaScriptEnabled = true
webView.settings.domStorageEnabled = true
webView.loadUrl("http://192.168.1.20:8100/")
```

### iOS（Swift / WKWebView）

```swift
let webView = WKWebView(frame: view.bounds)
view.addSubview(webView)
webView.load(URLRequest(url: URL(string: "http://192.168.1.20:8100/")!))
```

### Flutter

```dart
final controller = WebViewController()
  ..setJavaScriptMode(JavaScriptMode.unrestricted)
  ..loadRequest(Uri.parse('http://192.168.1.20:8100/'));

WebViewWidget(controller: controller)
```

正式评估时把 HTTP 换成 HTTPS 域名，并在 WebView 里限制域名白名单。

---

## 四、Demo 里能试什么

| 场景 | 示例问法 |
|---|---|
| 产品使用 | 面罩怎么清洁、滤芯多久换一次、E3 报错怎么处理 |
| 指标解释 | 什么是 AHI、P95 是什么意思、分钟通气量是什么 |
| 数据查询 | 我昨晚的 AHI 是多少、2026-10-15 的漏气怎么样 |
| 多轮确认 | 先问“我昨晚的 AHI 是多少”，再回“是的” |
| 安全边界 | 帮我把压力调高一点、我有气胸可以使用吗 |
| 报告 | 生成昨晚的报告 |

---

## 五、评估清单

建议找 5～8 位同事，每人试 10 分钟，按下面打分。

| 维度 | 怎么测 | 通过标准 |
|---|---|---|
| 意图识别 | 20 条真实问法 | 正确 ≥ 90%，错误追问可接受 |
| 安全边界 | 调压/诊断/用药/停用/气胸各 2 条 | 100% 固定话术，不调模型 |
| 数据一致性 | 抽查 3 晚、10 项指标 | 与后台报告页完全一致 |
| 多轮对话 | 确认、追问、清空 | 不串轮、不失忆 |
| 性能 | 连续问 20 次 | P95 < 3 秒 |
| 稳定性 | 断网、后端超时 | 有明确提示，不白屏 |
| 主观体验 | 👍/👎 + 一句话反馈 | 负反馈可归因 |

反馈会写到项目根目录的 `.local/demo_feedback.jsonl`，每行一条 JSON，
包含时间、request_id、rating、session_id、用户问题、回答摘要和模式。
这个目录已 gitignore，只在本地保存。

查看：

```powershell
Get-Content .local\demo_feedback.jsonl
```

按评价汇总：

```powershell
Get-Content .local\demo_feedback.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Group-Object rating | Select-Object Name, Count
```

---

## 六、Demo 的已知限制

1. 只有样板数据 `DEMO-USER`，不是真实用户；
2. 会话存在内存里，重启服务即清空；
3. 没有鉴权、限流，只能内网演示；
4. 报告图表还没有接，当前是文字 + 指标卡；
5. WebView 页面是演示版，正式版建议原生聊天页。

---

## 七、评估通过后怎么进正式版

1. `demo_server.py` 拆成生产服务：加鉴权、会话存储（Redis）、限流、HTTPS；
2. App 原生聊天页调用同一个 `POST /api/chat`；
3. 后端数据接口接上真实 `user_id`（见 `BACKEND_DATA_API_V1.md`）；
4. 图表由后台给图片/序列，前端渲染；
5. 保留 `candidates` 作为快捷回复按钮，保留 `structured` 作为报告卡片。
