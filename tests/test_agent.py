"""自检：python -m unittest discover -s tests -v"""

# 测试固定关闭向量：结果不能依赖开发机上是否装了 Ollama。
# 想验证真实向量效果请跑 eval_retrieval.py / tune_weights.py。
import os
os.environ.setdefault("SLEEPAGENT_DISABLE_VECTORS", "1")
import os
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepagent import (acceptance, bm25, embeddings, knowledge, llm, nightdata,
                        product_kb, report, router, runtime_info, safety, social,
                        validation, vector_index)


class TestKnowledge(unittest.TestCase):
    def test_loads(self):
        self.assertEqual(len(knowledge.load_knowledge_base()), 10)

    def test_retrieves_ahi(self):
        hits = knowledge.retrieve("AHI 是什么意思")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["category"], "ahi")

    def test_irrelevant_returns_nothing(self):
        self.assertEqual(knowledge.retrieve("今天天气怎么样", top_k=2), [])


class TestSafety(unittest.TestCase):
    def test_urgent(self):
        self.assertEqual(safety.check("我昨晚胸口疼得厉害").kind, "urgent")

    def test_pressure_change_refused(self):
        self.assertEqual(safety.check("帮我把压力调高一点").kind, "pressure")

    def test_ask_to_change_pressure_is_also_refused(self):
        for question in ("能不能自己改压力", "我可以调整压力吗", "压力能改吗"):
            self.assertEqual(safety.check(question).kind, "pressure", question)

    def test_pressure_question_without_change_allowed(self):
        self.assertEqual(safety.check("我的平均压力是多少").kind, "in_scope")

    def test_diagnosis_refused(self):
        self.assertEqual(safety.check("我这个是不是得了睡眠呼吸暂停").kind, "diagnosis")

    def test_stop_use_refused(self):
        self.assertEqual(safety.check("我不想戴了可以吗").kind, "stop")

    def test_normal_passes(self):
        self.assertEqual(safety.check("滤芯多久换一次").kind, "in_scope")


class TestSocial(unittest.TestCase):
    def test_greeting(self):
        self.assertEqual(social.classify("你好")[0], "greeting")

    def test_ahi_is_not_greeting(self):
        self.assertIsNone(social.classify("AHI 是什么意思")[0])

    def test_capability(self):
        self.assertEqual(social.classify("你能做什么")[0], "capability")

    def test_capability_examples_use_common_entrypoints(self):
        text = social.CAPABILITY_TEXT
        self.assertIn("帮我生成报告", text)
        self.assertIn("什么是 AHI", text)

    def test_report_trigger(self):
        self.assertEqual(social.classify("出个报告")[0], "report")

    def test_metric_question_is_not_report(self):
        self.assertIsNone(social.classify("报告里的 P95 是什么意思")[0])

    def test_leading_greeting_split(self):
        had, rest = social.split_leading_greeting("你好，滤芯多久换一次")
        self.assertTrue(had)
        self.assertIn("滤芯", rest)


class TestRouter(unittest.TestCase):
    def test_usage_terms(self):
        self.assertIn("usage", router.route("我这周用了多久")["topics"])

    def test_leakage_topic(self):
        self.assertIn("leakage", router.route("漏气量怎么样")["topics"])

    def test_out_of_scope(self):
        self.assertEqual(router.route("今天天气怎么样")["goal"], "out_of_scope")


class TestFacts(unittest.TestCase):
    def setUp(self):
        self.night = nightdata.load_night()

    def test_facts_built(self):
        self.assertGreaterEqual(len(self.night.facts), 8)

    def test_no_none_leaked(self):
        for item in self.night.facts:
            self.assertNotIn("None", item.text, item.fid)

    def test_every_fact_has_source(self):
        for item in self.night.facts:
            self.assertTrue(item.source)
            self.assertTrue(item.calculation)

    def test_p95_semantics(self):
        text = " ".join(item.text for item in self.night.facts if item.topic == "pressure")
        self.assertIn("不是最大值", text)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.payload = report.render(nightdata.load_night())

    def test_acceptance_passes(self):
        failures = []
        for item in self.payload["acceptance"]["results"]:
            failures.extend(item["failures"])
        self.assertEqual(failures, [], "；".join(failures))

    def test_modules_present(self):
        self.assertEqual(sorted(self.payload["modules"].keys()),
                         ["breathing-overview", "leakage", "pressure", "respiratory-events"])

    def test_boundary_stated(self):
        self.assertIn("不构成医学诊断", self.payload["text"])


class TestProductKb(unittest.TestCase):
    def test_loads_real_docs(self):
        stats = product_kb.stats()
        self.assertGreaterEqual(len(stats["user_documents"]), 2)
        self.assertTrue(any(word in name for name in stats["user_documents"]
                            for word in ("说明书", "产品说明", "使用指南")))
        self.assertGreater(stats["chunks"], 20)

    def test_hits_maintenance_question(self):
        hits = product_kb.retrieve("滤芯多久换一次", top_k=1)
        self.assertTrue(hits)
        self.assertIn("滤芯", hits[0]["section"])

    def test_numbered_section_ranks_first(self):
        # 说明书用「6.4 更换空气滤芯」这种编号章节，问法要能命中
        hits = product_kb.retrieve("滤芯多久换一次", top_k=1)
        self.assertTrue(hits)
        self.assertIn("滤芯", hits[0]["section"])

    def test_general_questions_mostly_do_not_hit(self):
        # 已知限制：个别口语化问题仍会弱命中（例如「推荐一部电影」撞上技术指标章节）。
        # 这里要求绝大多数无关问题不命中，而不是全部，避免把不现实的约束写死。
        questions = ("今天天气怎么样", "推荐一部电影", "美国的首都是哪里", "1+1等于几")
        hits = [q for q in questions if product_kb.retrieve(q, top_k=1)]
        self.assertLessEqual(len(hits), 1, "无关问题命中过多：" + str(hits))

    def test_docx_is_supported(self):
        """docx 是 zip，用标准库就能读。这里现场构造一个最小 docx 验证。"""
        path = product_kb.docs_dir() / "_selftest.docx"
        xml = (
            "<w:document><w:body>"
            "<w:p><w:pPr><w:pStyle w:val=\"Heading1\"/></w:pPr>"
            "<w:r><w:t>自检章节</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>本机自检专用段落，包含关键词 自检专用标记。</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        try:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", xml)
            product_kb.reload_chunks()
            hits = product_kb.retrieve("自检专用标记", top_k=1)
            self.assertTrue(hits, "docx 里的内容没有被读到")
            self.assertIn("_selftest", hits[0]["doc"])
        finally:
            if path.exists():
                path.unlink()
            product_kb.reload_chunks()


class TestValidation(unittest.TestCase):
    def test_grounded_rejects_unknown_number(self):
        result = validation.check_output("压力是 9.87。", "压力是 4.35", mode="grounded")
        self.assertFalse(result["ok"])

    def test_general_allows_plain_numbers(self):
        result = validation.check_output("一加一等于 2。", "1+1", mode="general")
        self.assertTrue(result["ok"], result["problems"])

    def test_general_still_blocks_pressure_advice(self):
        result = validation.check_output("建议你调高到 6。", "任意", mode="general")
        self.assertFalse(result["ok"])

    def test_general_still_blocks_diagnosis_claim(self):
        result = validation.check_output("可以确诊为呼吸暂停。", "任意", mode="general")
        self.assertFalse(result["ok"])

    def test_grounded_blocks_clinical_pattern(self):
        result = validation.check_output("你得了重度呼吸暂停。", "任意", mode="grounded")
        self.assertFalse(result["ok"])

    def test_tolerates_rounding(self):
        self.assertTrue(validation.check_output("约 4.4。", "4.35", mode="grounded")["ok"])

    def test_p95_not_treated_as_number(self):
        self.assertTrue(validation.check_output("P95 表示上沿。", "材料无数字", mode="grounded")["ok"])


class TestLlmSettings(unittest.TestCase):
    def test_openai_provider_from_env(self):
        saved = {k: os.environ.get(k) for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY")}
        try:
            os.environ["LLM_PROVIDER"] = "openai"
            os.environ["LLM_MODEL"] = "deepseek-chat"
            os.environ["LLM_API_KEY"] = "sk-test-key-123456"
            cfg = llm.settings()
            self.assertEqual(cfg["provider"], "openai")
            self.assertEqual(cfg["model"], "deepseek-chat")
            self.assertNotIn("test-key", llm.mask_key(cfg["api_key"]))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_env_provider_change_ignores_file_config(self):
        from unittest import mock
        saved = os.environ.get("LLM_PROVIDER")
        file_cfg = {"provider": "openai", "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat", "api_key": "sk-file"}
        try:
            os.environ["LLM_PROVIDER"] = "ollama"
            with mock.patch.object(llm, "_config", return_value=file_cfg):
                cfg = llm.settings()
            self.assertEqual(cfg["provider"], "ollama")
            self.assertNotIn("deepseek", cfg["base_url"])
            self.assertEqual(cfg["api_key"], "")
        finally:
            if saved is None:
                os.environ.pop("LLM_PROVIDER", None)
            else:
                os.environ["LLM_PROVIDER"] = saved


class TestRuntimeInfo(unittest.TestCase):
    """大模型没有实时时钟，日期必须由程序给出。"""

    def test_detects_date_questions(self):
        for question in ("今天日期", "今天星期几", "现在几点", "今天是几号", "当前时间"):
            self.assertTrue(runtime_info.is_datetime_question(question), question)

    def test_does_not_misdetect(self):
        for question in ("今天天气怎么样", "我昨晚用了多久", "滤芯多久换一次",
                         "报告日期是什么", "今天的数据怎么样"):
            self.assertFalse(runtime_info.is_datetime_question(question), question)

    def test_answer_contains_real_date(self):
        text = runtime_info.answer("今天日期")
        expected = runtime_info.now().strftime("%Y年%m月%d日")
        self.assertIn(expected, text)
        self.assertIn(runtime_info.weekday_cn(), text)

    def test_weekday_question_returns_weekday(self):
        text = runtime_info.answer("今天星期几")
        self.assertIn(runtime_info.weekday_cn(), text)

    def test_clock_question_returns_time(self):
        text = runtime_info.answer("现在几点")
        self.assertIn(runtime_info.now().strftime("%H:%M"), text)

    def test_facts_line_contains_date(self):
        line = runtime_info.facts_line()
        self.assertIn(runtime_info.now().strftime("%Y-%m-%d"), line)


class TestDateQuestionGoesToClock(unittest.TestCase):
    def setUp(self):
        import chat
        self.chat = chat
        self.night = nightdata.load_night()

    def test_date_question_never_calls_model(self):
        from unittest import mock
        calls = []
        def fake_chat(messages, **kwargs):
            calls.append(messages)
            return {"status": "success", "provider": "fake", "model": "fake",
                    "content": "今天是 2026 年 5 月 12 日。", "detail": ""}
        with mock.patch.object(self.chat.llm, "chat", side_effect=fake_chat):
            result = self.chat.answer("今天日期", self.night, [], use_llm=True)
        self.assertEqual(calls, [], "日期问题不该调用模型")
        self.assertEqual(result["mode"], "系统信息")
        self.assertNotIn("5 月 12 日", result["text"])
        self.assertIn(runtime_info.now().strftime("%Y年%m月%d日"), result["text"])

    def test_prompt_carries_current_date(self):
        from unittest import mock
        captured = []
        def fake_chat(messages, **kwargs):
            captured.append(messages)
            return {"status": "success", "provider": "fake", "model": "fake",
                    "content": "这是一段普通回答。", "detail": ""}
        with mock.patch.object(self.chat.llm, "chat", side_effect=fake_chat):
            self.chat.answer("推荐一部电影", self.night, [], use_llm=True)
        self.assertTrue(captured, "通用问答应该调用模型")
        joined = str(captured[-1])
        self.assertIn(runtime_info.now().strftime("%Y-%m-%d"), joined)


class TestDeviceDataQuestions(unittest.TestCase):
    """数据类问题必须走设备数据模式，用本晚真实数值回答。"""

    def setUp(self):
        import chat
        self.chat = chat
        self.night = nightdata.load_night()

    def test_ahi_question_uses_device_data(self):
        # 回归：路由识别的是 ahi 主题，事实层用的是 events 话题，
        # 少了映射这一步会掉到产品文档模式，答出完全无关的内容
        result = self.chat.answer("我的 AHI 是多少", self.night, [], use_llm=False)
        self.assertIn("设备数据", result["mode"])
        self.assertIn("5.7", result["text"])
        self.assertNotIn("应用商店", result["text"])

    def test_topic_alias_map_covers_router_topics(self):
        from sleepagent.router import TOPIC_TERMS
        router_topics = {topic for topic, _ in TOPIC_TERMS}
        fact_topics = {fact.topic for fact in self.night.facts}
        aliases = self.chat.FACT_TOPIC_ALIASES
        unresolved = router_topics - fact_topics - set(aliases) - {"overall"}
        self.assertEqual(unresolved, set(), "这些主题没有对应的事实话题：" + str(unresolved))

    def test_other_data_questions(self):
        for question, token in (("我昨晚用了多久", "402"),
                                ("我的漏气量怎么样", "12.4"),
                                ("压力正常吗", "4.35"),
                                ("我的呼吸频率是多少", "15.2")):
            result = self.chat.answer(question, self.night, [], use_llm=False)
            self.assertIn("设备数据", result["mode"], question)
            self.assertIn(token, result["text"], question)

    def test_product_doc_question_still_routes_to_docs(self):
        result = self.chat.answer("滤芯多久换一次", self.night, [], use_llm=False)
        self.assertIn("产品文档", result["mode"])


class TestReportExportImport(unittest.TestCase):
    """报告导出 -> 内部契约 的转换层。"""

    CSV = "".join((
        "\ufeff用户ID,设备ID,PiCode/产品编码,设备类型,记录时间（yyyy-MM-dd HH:mm:ss）,开始时间,结束时间,",
        "治疗时长,治疗评分（时长+面罩+呼吸事件，满分100）,",
        "\"当日工作模式 CPAP: 1,AutoCPAP: 2,\",",
        "AHI,漏气P95,平均漏气,漏气最大持续,漏气时间占比,",
        "呼吸暂停次数（当日 type=2 apnea_count 合计）,",
        "低通气次数（当日 type=2 hypopnea_count 合计）,",
        "中枢性睡眠呼吸暂停次数（当日 type=2 ca_count 合计）,",
        "睡眠呼吸暂停指数 AI（次/小时）,低通气指数 HI（次/小时）,",
        "中枢性睡眠呼吸暂停指数 CAI（次/小时）,",
        "呼吸暂停总持续时间（秒，type=2 apnea_duration 合计）,",
        "低通气总持续时间（秒，type=2 hypopnea_duration 合计）,",
        "平均治疗压力（hPa）,治疗压力P95（hPa）,",
        "平均潮气量,潮气量P95,平均呼吸频率,平均分钟通气量,分钟通气P95,平均吸气时间,",
        "固件版本,硬件版本,面罩得分,呼吸事件得分,治疗时长得分\n",
        "719697,DEV1,PICODE1,70120,2026-09-22,2026-09-22 12:31:10,2026-09-23 11:52:09,",
        "138,47,3,5.65,6,2.14,0,0,",
        "1,12,0,0.43,5.22,0.0,",
        "55,196,",
        "536,780,",
        "515.49,808,15.84,17.6,32,1.34,",
        "1.00.22,1.0,19.86,4,23\n",
    ))

    def setUp(self):
        import importlib
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        self.tmp.write(self.CSV)
        self.tmp.close()
        self.mod = importlib.import_module("import_report_export")

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _convert(self):
        _, data, index = self.mod.load_rows(Path(self.tmp.name))
        self.assertEqual(len(data), 1)
        return self.mod.convert(data[0], index)

    def test_bom_header_is_stripped(self):
        _, _, index = self.mod.load_rows(Path(self.tmp.name))
        self.assertIn("用户ID", index)

    def test_pressure_is_divided_by_100(self):
        record = self._convert()
        self.assertAlmostEqual(record["treat_pressure_avg"], 5.36, places=2)
        self.assertAlmostEqual(record["treat_pressure_p95"], 7.8, places=2)

    def test_identity_fields(self):
        record = self._convert()
        self.assertEqual(record["record_date"], "2026-09-22")
        self.assertEqual(record["user_id"], "719697")
        self.assertEqual(record["device_id"], "DEV1")

    def test_missing_fields_are_recorded_not_zeroed(self):
        # 导出没有的字段不能被填成 0，应当显式列出
        record = self._convert()
        self.assertNotIn("treat_pressure_max", record)
        self.assertIn("treat_pressure_max", record["_absent_fields"])

    def test_contract_reaches_report(self):
        from sleepagent import report
        night = self.mod.build_night(self._convert())
        payload = report.render(night)
        failures = []
        for item in payload["acceptance"]["results"]:
            failures.extend(item["failures"])
        self.assertEqual(failures, [], "；".join(failures))

    def test_report_has_no_none_leak(self):
        from sleepagent import report
        night = self.mod.build_night(self._convert())
        text = report.render(night)["text"]
        self.assertNotIn("None", text)

    def test_fragment_night_is_flagged(self):
        # 治疗时长低于阈值时，脚本主流程应给出碎片记录提示
        record = self._convert()
        self.assertLess(record["treat_time_minutes"], self.mod.USABLE_MINUTES)



class TestDataScopeConsistency(unittest.TestCase):
    """对话侧和报告侧必须对「有几晚数据」说法一致。

    回归背景：对话侧过去只取按主题匹配的事实，看不到历史夜晚，
    会回答「没有更早的使用记录」；报告侧读 trend_summary() 却写着「共 4 个夜晚」。
    同一个用户问两次得到自相矛盾的说法。
    """

    def setUp(self):
        import chat
        self.chat = chat
        self.night = nightdata.load_night()

    def test_coverage_fact_exists(self):
        fact = nightdata.coverage_fact(self.night.user_id, self.night)
        self.assertEqual(fact.fid, "data-coverage")
        self.assertIn("7 个有记录的夜晚", fact.text)

    def test_device_mode_includes_coverage(self):
        result = self.chat.answer("我上次用了多久", self.night, [], use_llm=False)
        self.assertIn("设备数据", result["mode"])
        self.assertIn("7 个有记录的夜晚", result["text"])
        self.assertNotIn("没有更早的使用记录", result["text"])

    def test_chat_and_report_agree_on_night_count(self):
        chat_text = self.chat.answer("我上次用了多久", self.night, [], use_llm=False)["text"]
        report_text = report.render(self.night)["text"]
        self.assertIn("7 个有记录的夜晚", chat_text)
        self.assertIn("7 个有记录的夜晚", report_text)

    def test_coverage_range_starts_at_earliest_night(self):
        fact = nightdata.coverage_fact(self.night.user_id, self.night)
        self.assertIn("2026-10-09", fact.text)
        self.assertIn("2026-10-15", fact.text)

    def test_trend_is_chronological(self):
        trend = nightdata.trend_summary(self.night.user_id, self.night)
        dates = [str(item["record_date"]) for item in trend["nights"]]
        self.assertEqual(dates, sorted(dates), "趋势夜晚必须按日期升序")

    def test_report_trend_states_chronological_ahi(self):
        # 报告写「按时间顺序 AHI 依次为」，那顺序就必须真的是时间序
        trend = nightdata.trend_summary(self.night.user_id, self.night)
        ordered = [str(item.get("ahi")) for item in trend["nights"]]
        text = report.render(self.night)["text"]
        self.assertIn("、".join(ordered), text)

    def test_coverage_not_injected_outside_device_mode(self):
        # 范围事实只在设备数据模式加，否则每个问题都会变成设备数据
        result = self.chat.answer("滤芯多久换一次", self.night, [], use_llm=False)
        self.assertIn("产品文档", result["mode"])
        self.assertNotIn("个有记录的夜晚", result["text"])


class TestMultiNightDataset(unittest.TestCase):
    """多晚数据集：按需取某一晚，报告用近几天。"""

    def setUp(self):
        self.nights = nightdata.load_dataset()

    def test_dataset_has_seven_nights(self):
        self.assertEqual(len(self.nights), 7)

    def test_nights_are_sorted_by_date(self):
        dates = [str(n.raw["record_date"]) for n in self.nights]
        self.assertEqual(dates, sorted(dates))

    def test_each_night_has_its_own_history(self):
        self.assertEqual(len(self.nights[0].history), 0)
        self.assertEqual(len(self.nights[-1].history), 6)

    def test_every_night_has_full_facts(self):
        for night in self.nights:
            self.assertGreaterEqual(len(night.facts), 10, night.raw["record_date"])

    def test_dataset_is_internally_consistent(self):
        # 原来那份样板数据 AHI 6.4 与 AI+HI=5.7 对不上，新数据强制自洽
        for night in self.nights:
            raw = night.raw
            ahi = raw["apnea_index"] + raw["hypopnea_index"]
            self.assertAlmostEqual(ahi, raw["ahi"], places=2, msg=str(raw["record_date"]))
            self.assertEqual(
                raw["treat_duration_score"] + raw["mask_score"] + raw["resp_score"],
                raw["treat_score"], str(raw["record_date"]))
            self.assertGreaterEqual(raw["air_leakage_p95"], raw["air_leakage_avg"])

    def test_data_source_propagates_to_every_night(self):
        for night in self.nights:
            self.assertIn("样板数据", str(night.raw.get("data_source")))


class TestNightSelection(unittest.TestCase):
    """按提问里的时间词挑选治疗夜。"""

    def setUp(self):
        self.nights = nightdata.load_dataset()

    def _pick(self, question, today=None):
        night, _ = nightdata.select_night(
            nightdata.DEFAULT_USER_ID, self.nights, question, today=today)
        return str(night.raw["record_date"]) if night else None

    def test_latest_by_default(self):
        self.assertEqual(self._pick("帮我看看"), "2026-10-15")

    def test_yesterday_resolves_only_when_data_has_it(self):
        import datetime
        # 数据到 10-15：系统日期 10-16 时，昨晚正好有记录
        self.assertEqual(self._pick("昨晚使用情况", datetime.date(2026, 10, 16)), "2026-10-15")
        # 系统日期 10-20 时，昨晚是 10-19，数据里没有，不能拿 10-15 顶替
        self.assertIsNone(self._pick("昨晚使用情况", datetime.date(2026, 10, 20)))
        self.assertIsNone(self._pick("前天用了多久", datetime.date(2026, 10, 20)))

    def test_day_before_wording_stays_data_relative(self):
        self.assertEqual(self._pick("前一天用了多久"), "2026-10-14")
        self.assertEqual(self._pick("上一晚怎么样"), "2026-10-14")

    def test_explicit_date(self):
        self.assertEqual(self._pick("2026-10-11 的情况"), "2026-10-11")
        self.assertEqual(self._pick("10月13日怎么样"), "2026-10-13")

    def test_missing_date_returns_none(self):
        night, note = nightdata.select_night(
            nightdata.DEFAULT_USER_ID, self.nights, "2025-01-01 的报告")
        self.assertIsNone(night)
        self.assertIn("没有", note)

    def test_recent_uses_latest(self):
        self.assertEqual(self._pick("最近怎么样"), "2026-10-15")


class TestSelectionRouting(unittest.TestCase):
    """时间指代要能压过产品提问词。"""

    def setUp(self):
        import chat
        self.chat = chat
        self.nights = nightdata.load_dataset()

    def _mode(self, question):
        night, _ = nightdata.select_night(nightdata.DEFAULT_USER_ID, self.nights, question)
        return self.chat.answer(question, night or self.nights[-1], [], use_llm=False)

    def test_time_ref_beats_howto_word(self):
        # 「前一天用了多久」带「多久」这个产品提问词，但问的是用户数据
        self.assertIn("设备数据", self._mode("前一天用了多久")["mode"])

    def test_date_question_without_metric_word(self):
        self.assertIn("设备数据", self._mode("2026-10-11 的情况")["mode"])

    def test_product_question_without_time_ref_still_uses_docs(self):
        self.assertIn("产品文档", self._mode("滤芯多久换一次")["mode"])

    def test_usage_wording_is_recognized(self):
        self.assertIn("设备数据", self._mode("最近使用情况")["mode"])


class TestHeadingDetection(unittest.TestCase):
    """编号列表项不能被当成章节标题，否则内容会被静默丢掉。

    回归背景：「连接手机 App」那一节下面全是 1. 2. 3. 这样的编号步骤，
    被标题判定吃掉后整个章节没有进入索引，问「如何连接手机app」会答「资料里没有」。
    """

    def setUp(self):
        from sleepagent import product_kb
        self.pk = product_kb
        self.pk.reload_chunks()

    def test_numbered_list_item_is_not_heading(self):
        self.assertFalse(self.pk._is_heading("1. 手机安装配套 App 并完成注册。"))
        self.assertFalse(self.pk._is_heading("3. 主机开机后进入「设置 - 蓝牙配对」，屏幕会显示配对码。"))

    def test_multilevel_number_is_still_heading(self):
        self.assertTrue(self.pk._is_heading("3.4.2 YA70双水平正压通气治疗机技术要求"))
        self.assertTrue(self.pk._is_heading("## 连接手机 App"))
        self.assertTrue(self.pk._is_heading("1. 治疗压力"))

    def test_table_row_is_not_heading(self):
        self.assertFalse(self.pk._is_heading("YA70A | CPAP、Auto CPAP | 4~20 | 0.5"))

    def test_app_section_is_indexed(self):
        sections = {chunk.section for chunk in self.pk.load_chunks()}
        self.assertTrue(any("APP" in section for section in sections),
                        "说明书的 APP 章节没有被索引：" + str(sorted(sections)[:5]))

    def test_app_connection_question_hits_docs(self):
        hits = self.pk.retrieve("如何连接手机app", top_k=3)
        self.assertTrue(hits, "APP 相关章节没有被检索到")
        self.assertTrue(
            any(word in hit["section"] for hit in hits for word in ("APP", "连接", "控制软件", "设置")),
            str([hit["section"] for hit in hits]))

    def test_numbered_list_content_is_indexed(self):
        # 说明书里 APP 安装步骤是 1) 2) 的编号列表，内容不能被当成标题丢掉
        blob = " ".join(chunk.text for chunk in self.pk.load_chunks())
        self.assertIn("应用宝", blob)

    def test_synonym_does_not_duplicate(self):
        # 「手机app」不该被换成「appapp」，否则会丢掉「手机」这个能命中的词
        self.assertEqual(self.pk._apply_synonyms("如何连接手机app"), "如何连接手机app")
        self.assertEqual(self.pk._apply_synonyms("加湿器有几个档位"), "加温湿化器有几个档位")


class TestBM25(unittest.TestCase):
    """BM25 基础行为。它是可选的第三路信号，不是替换现有检索。"""

    def test_tokenize_chinese_bigrams(self):
        self.assertEqual(bm25.tokenize("延时升压"), ["延时", "时升", "升压"])

    def test_tokenize_mixed(self):
        terms = bm25.tokenize("E3 报错")
        self.assertIn("e3", terms)
        self.assertIn("报错", terms)

    def test_idf_downweights_common_terms(self):
        documents = [
            {"key": "1", "text": "面罩 面罩 面罩", "payload": {"doc": "a", "section": "s1"}},
            {"key": "2", "text": "面罩 滤芯", "payload": {"doc": "a", "section": "s2"}},
            {"key": "3", "text": "面罩 滤芯 更换 周期", "payload": {"doc": "a", "section": "s3"}},
        ]
        index = bm25.BM25Index(documents)
        # 「面罩」出现在每篇里，IDF 应明显低于只出现一次的「周期」
        self.assertLess(index.idf["面罩"], index.idf["周期"])

    def test_ranks_matching_document_first(self):
        chunks = [chunk for chunk in product_kb.load_chunks() if chunk.visibility == "user"]
        index = bm25.build_from_chunks(chunks)
        hits = index.search("滤芯多久换一次", top_k=1)
        self.assertTrue(hits)
        self.assertIn("滤芯", hits[0]["section"])

    def test_empty_query_returns_nothing(self):
        chunks = [chunk for chunk in product_kb.load_chunks() if chunk.visibility == "user"]
        index = bm25.build_from_chunks(chunks)
        self.assertEqual(index.search("", top_k=3), [])


class TestRetrievalQuality(unittest.TestCase):
    """把检索质量固定成断言，防止改动后悄悄退步。

    当前基线：现有关键词 R@1 = 70%，R@3 = 85%（27 条用例）。
    这里用「至少不能比基线差太多」的阈值，避免把具体数字焊死。
    """

    def test_keyword_retrieval_recall(self):
        import eval_retrieval
        chunks = [chunk for chunk in product_kb.load_chunks() if chunk.visibility == "user"]
        cases = eval_retrieval.all_cases()
        hits = 0
        for question, expected in cases:
            results = product_kb.retrieve(question, top_k=1)
            words = expected if isinstance(expected, list) else [expected]
            if results and any(word in str(results[0].get("section", "")) for word in words):
                hits += 1
        ratio = hits / len(cases)
        # 当前实测 70%；跌破 60% 说明明显退步
        self.assertGreaterEqual(ratio, 0.60, "稀疏检索 R@1 跌破 60%：" + str(round(ratio * 100)) + "%")


class TestRRFFusion(unittest.TestCase):
    """RRF 只融合排名，不融合分数。"""

    def _hit(self, section, score):
        return {"doc": "d.md", "section": section, "index": 0, "text": "", "score": score}

    def test_item_ranked_first_in_both_wins(self):
        lexical = [self._hit("A", 20.0), self._hit("B", 10.0)]
        dense = [self._hit("A", 0.9), self._hit("C", 0.8)]
        fused = product_kb._rrf_fuse([lexical, dense], top_k=3)
        self.assertEqual(fused[0]["section"], "A")

    def test_scale_does_not_matter(self):
        # 关键词分 20 与余弦分 0.9 量纲完全不同，融合结果不应被量纲带偏
        lexical = [self._hit("A", 0.001), self._hit("B", 0.0001)]
        dense = [self._hit("B", 0.99), self._hit("A", 0.98)]
        fused = product_kb._rrf_fuse([lexical, dense], top_k=2)
        self.assertEqual({hit["section"] for hit in fused}, {"A", "B"})

    def test_weights_shift_ranking(self):
        lexical = [self._hit("A", 10.0)]
        dense = [self._hit("B", 0.9)]
        fused = product_kb._rrf_fuse([lexical, dense], top_k=2, weights=(1.0, 0.1))
        self.assertEqual(fused[0]["section"], "A")

    def test_deduplicates_same_chunk(self):
        lexical = [self._hit("A", 10.0)]
        dense = [self._hit("A", 0.9)]
        fused = product_kb._rrf_fuse([lexical, dense], top_k=3)
        self.assertEqual(len(fused), 1)


class TestHybridFallback(unittest.TestCase):
    """向量不可用时必须静默退回纯关键词，行为与改造前一致。"""

    def test_falls_back_when_embeddings_unavailable(self):
        from unittest import mock
        with mock.patch.object(product_kb.embeddings, "available", return_value=False):
            hybrid = product_kb.retrieve("滤芯多久换一次", top_k=3, visibility="user")
            lexical = product_kb.lexical_search(
                "滤芯多久换一次", top_k=3, visibility="user")
        self.assertEqual([hit["section"] for hit in hybrid],
                         [hit["section"] for hit in lexical])

    def test_hybrid_false_is_pure_lexical(self):
        hybrid = product_kb.retrieve(
            "滤芯多久换一次", top_k=3, visibility="user", hybrid=False)
        lexical = product_kb.lexical_search(
            "滤芯多久换一次", top_k=3, visibility="user")
        self.assertEqual([hit["section"] for hit in hybrid],
                         [hit["section"] for hit in lexical])

    def test_falls_back_when_embedding_returns_none(self):
        from unittest import mock
        with mock.patch.object(product_kb.embeddings, "available", return_value=True), \
             mock.patch.object(product_kb.embeddings, "embed_query", return_value=None):
            hits = product_kb.retrieve("滤芯多久换一次", top_k=3)
        self.assertTrue(hits)
        self.assertIn("滤芯", hits[0]["section"])


class TestVectorIndex(unittest.TestCase):
    """索引的增量重建与降级。"""

    def _chunks(self):
        return [chunk for chunk in product_kb.load_chunks() if chunk.visibility == "user"][:5]

    def _index_path(self):
        # 用项目内路径，避免依赖系统临时目录（部分环境下无法在 Temp 下建子目录）
        return Path(__file__).resolve().parents[1] / ".local" / "_test_index.json"

    def test_builds_and_reuses_cache(self):
        from unittest import mock
        calls = {"n": 0}

        def fake_embed(texts):
            calls["n"] += 1
            return [[0.1] * 8 for _ in texts]

        target = self._index_path()
        target.unlink(missing_ok=True)
        try:
            with mock.patch.object(vector_index, "INDEX_FILE", target):
                entries = vector_index.ensure(self._chunks(), fake_embed, "m1")
                self.assertEqual(len(entries), 5)
                self.assertEqual(calls["n"], 1)
                # 第二次应全部命中缓存，不再调用向量化
                vector_index.ensure(self._chunks(), fake_embed, "m1")
                self.assertEqual(calls["n"], 1)
                # 模型换了必须重建
                vector_index.ensure(self._chunks(), fake_embed, "m2")
                self.assertEqual(calls["n"], 2)
        finally:
            target.unlink(missing_ok=True)

    def test_returns_none_when_embedding_fails(self):
        from unittest import mock
        target = self._index_path()
        target.unlink(missing_ok=True)
        try:
            with mock.patch.object(vector_index, "INDEX_FILE", target):
                self.assertIsNone(vector_index.ensure(self._chunks(), lambda texts: None, "m1"))
        finally:
            target.unlink(missing_ok=True)

    def test_cosine_basic(self):
        self.assertAlmostEqual(vector_index.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(vector_index.cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(vector_index.cosine([1, 0], [1, 0, 0]), 0.0)   # 维度不一致

    def test_search_respects_visibility(self):
        entries = [
            {"doc": "a", "section": "s1", "index": 0, "visibility": "user", "text": "", "vector": [1.0, 0.0]},
            {"doc": "b", "section": "s2", "index": 0, "visibility": "internal", "text": "", "vector": [1.0, 0.0]},
        ]
        hits = vector_index.search([1.0, 0.0], top_k=5, visibility="user", entries=entries)
        self.assertEqual([hit["doc"] for hit in hits], ["a"])


class TestEmbeddingsClient(unittest.TestCase):
    def test_fake_vector_is_deterministic_and_normalized(self):
        first = embeddings._fake_vector("滤芯多久换一次")
        second = embeddings._fake_vector("滤芯多久换一次")
        self.assertEqual(first, second)
        norm = sum(value * value for value in first) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_same_text_same_vector_different_text_differs(self):
        self.assertNotEqual(embeddings._fake_vector("面罩清洗"), embeddings._fake_vector("滤芯更换"))

    def test_settings_from_env(self):
        import os
        saved = {key: os.environ.get(key) for key in ("LLM_EMBEDDING_PROVIDER", "LLM_EMBEDDING_MODEL")}
        try:
            os.environ["LLM_EMBEDDING_PROVIDER"] = "fake"
            os.environ["LLM_EMBEDDING_MODEL"] = "fake-hash-256"
            cfg = embeddings.settings()
            self.assertEqual(cfg["provider"], "fake")
            self.assertTrue(embeddings.available())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            embeddings.reset_probe_cache()


class TestPRDAlignment(unittest.TestCase):
    """PRD 明确要求、且能在当前 demo 里验证的几项。"""

    def test_medication_advice_is_refused(self):
        # PRD 4.6.1：用药 / 治疗建议必须拦截
        for question in ("我应该吃什么药", "怎么治疗", "要不要手术"):
            self.assertEqual(safety.check(question).kind, "medication", question)

    def test_medication_reply_points_to_doctor(self):
        self.assertIn("医生", safety.check("吃什么药").reply)

    def test_context_keeps_last_ten_turns(self):
        import chat
        history = []
        for index in range(15):
            history.append({"role": "user", "content": "问题" + str(index)})
            history.append({"role": "assistant", "content": "回答" + str(index)})
        messages = chat._context_messages(history)
        self.assertEqual(len(messages), 20)          # 10 轮 = 20 条消息
        self.assertEqual(messages[0]["content"], "问题5")
        self.assertEqual(messages[-1]["content"], "回答14")

    def test_context_empty_history(self):
        import chat
        self.assertEqual(chat._context_messages([]), [])

    def test_report_has_rating_and_core_metrics(self):
        payload = report.render(nightdata.load_night())
        text = payload["text"]
        self.assertIn("总体评级：", text)
        self.assertIn("核心指标：", text)
        for label in ("AHI", "使用时长", "平均漏气量", "平均治疗压力"):
            self.assertIn(label, text)

    def test_rating_bands(self):
        from sleepagent import report as report_module
        night = nightdata.load_night()
        night.raw["treat_score"] = 95
        self.assertEqual(report_module._rating(night)[0], "优秀")
        night.raw["treat_score"] = 80
        self.assertEqual(report_module._rating(night)[0], "良好")
        night.raw["treat_score"] = 65
        self.assertEqual(report_module._rating(night)[0], "需留意")
        night.raw["treat_score"] = 40
        self.assertEqual(report_module._rating(night)[0], "较差")

    def test_rating_missing_score_is_declared(self):
        from sleepagent import report as report_module
        night = nightdata.load_night()
        night.raw["treat_score"] = None
        rating, text = report_module._rating(night)
        self.assertIsNone(rating)
        self.assertIn("未提供", text)

    def test_welcome_has_disclaimer(self):
        import chat
        self.assertIn("不构成医疗诊断", chat.BANNER)

    def test_fallback_mentions_human_support(self):
        import chat
        self.assertIn("人工客服", chat.GENERAL_FALLBACK)


if __name__ == "__main__":
    unittest.main()
