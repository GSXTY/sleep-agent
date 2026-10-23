# YA70 日期区间接口测试

打开 `tools\test_ya70_api.py`，修改文件顶部的三个请求字段：

```python
REQUEST_BODY = {
    "userid": 10002,
    "startDate": "2026-09-23",
    "endDate": "2026-10-22",
}
```

然后运行：

```powershell
python tools\test_ya70_api.py
```

当前联调地址已切换到叶添添提供的本地服务：

```text
http://127.0.0.1:8081/infc/respStatisticsYa70/listByDateRange
```

该地址已实测连通，返回 `recode=1`。当前 `2026-09-23` 到 `2026-10-22`
区间内返回两条记录，日期分别为 `2026-10-15` 和 `2026-10-18`。
返回压力字段为 `treatPressure/treatPressure95 = 400`，后续适配时需要按 Pa
转换为 `4.00 hPa`。

脚本会：

1. 向 `URL` 发送 JSON POST；
2. 在请求头增加 `key: <API_KEY>`；
3. 打印 HTTP 状态；
4. 按 Markdown 示例的 JSON 格式打印后端响应。

如果接口返回 404，并且响应中不再出现“用户key验证失败”，说明鉴权已经通过，
但当前域名没有部署对应路径。

测试 key 属于敏感信息，正式提交前应移除或替换为环境变量。

## 真实数据 Agent 测试

接口字段映射位于 `sleepagent/backend_ya70.py`。运行完整 Agent 测试：

```powershell
python tools\test_agent_with_backend.py
```

适配层会：

- 将 `recordeDate` 映射为内部 `record_date`；
- 将 `treatTime/treatScore` 和三个分项映射为内部字段；
- 将 `treatPressure/treatPressure95` 从 Pa 除以 100 转成 hPa；
- 映射 AHI、漏气、潮气量和呼吸频率；
- 将 `treatModels` 数字转换成 CPAP/AutoCPAP/S/AutoS 等名称；
- 按日期构建 `Night.history`。

当前真实数据只有 `2026-10-15` 和 `2026-10-18` 两晚，且两晚都短于 4 小时，
因此报告会明确提示样本不足和碎片记录。
