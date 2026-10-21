"""命令行助手：通用问答 + 产品文档问答 + 设备数据解读。

四种模式，按优先级判定：

  1. 安全边界   —— 红旗症状 / 要求调压 / 要求诊断 / 要求停用 → 固定话术，不过模型
  2. 设备数据   —— 命中指标主题且事实层有数据 → 用本晚真实数值回答
  3. 产品文档   —— 产品文档检索命中 → 依据资料回答，带出处
  4. 通用对话   —— 其余问题直接交给大模型，但不得编造产品细节和医学结论

设计原则：
  · 事实与资料是唯一的信息来源，模型只负责组织语言
  · 模型输出必须过校验闸门；不过就回退（产品/设备模式退到原文，通用模式退到说明）
  · 大模型不可用时产品文档仍可直接检索出原文，功能不中断

用法：
  python chat.py                 进入交互对话
  python chat.py "滤芯多久换一次"   单次提问
  python chat.py --report        打印单晚报告
  python chat.py --no-llm "..."  强制使用规则版
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from sleepagent.logging_config import configure_logging
from sleepagent import (
    embeddings,
    knowledge,
    llm,
    nightdata,
    product_kb,
    report,
    safety,
    social,
    vector_index,
)
from sleepagent.pipeline import (  # noqa: F401  保持 chat.<name> 对外可用
    AgentResponse,
    FACT_TOPIC_ALIASES,
    GENERAL_FALLBACK,
    MAX_CONTEXT_TURNS,
    UserQuery,
    VALIDATION_MODE,
    _context_messages,
    _facts_for,
    _has_time_ref,
    answer,
    resolve_pending_confirmation,
)

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BANNER = """睡眠呼吸产品助手（命令行版）
----------------------------------------------------
直接说话就行：一般问题直接回答，产品相关的问题会去查产品资料。

免责声明：本分析仅供设备使用参考，不构成医疗诊断意见，个人治疗方案请遵医嘱。

命令：
  /docs       列出已加载的产品资料
  /report     生成单晚治疗报告（样板数据）
  /facts      查看本晚确定性事实
  /knowledge  列出指标解释条目
  /safety X   只做安全判定
  /llm        查看大模型配置 / on / off / test
  /status     查看聊天模型、向量模型、索引与降级状态
  /clear      清空对话历史
  /help       显示本帮助
  /quit       退出
"""


def history_messages(question: str, result, night=None) -> list:
    """把一轮问答压成写进对话历史的条目。

    报告正文有几千字，而且带着具体数值。整段存进历史有两个后果：
    一是挤占 10 轮上下文，二是模型会把别的轮次（往往是别的夜晚）的数字
    抄进当前回答，被校验闸门判为「材料中没有的数字」。
    所以报告类回答只留一句占位，数值不回灌历史。
    """
    structured = getattr(result, "structured", None)
    mode = str(getattr(result, "mode", ""))
    if structured or mode.startswith("报告生成"):
        date = ""
        if isinstance(structured, dict):
            date = str(structured.get("record_date") or "")
        if not date and night is not None:
            date = str(night.raw.get("record_date") or "")
        note = ("已生成" + (date + " 的" if date else "") + "单晚治疗报告。"
                "报告里的具体数值不放进对话历史；用户追问细节时，重新生成报告或指定日期。")
        return [
            {"role": "user", "content": question},
            {"role": "assistant", "content": note},
        ]
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": result.text},
    ]


def option_hint(result) -> str:
    """命令行里把可选项列出来，用户可以直接照着输入。"""
    picks = [item.get("title", "") for item in (result.candidates or [])
             if item.get("type") in ("date", "topic") and item.get("title")]
    return "  [可选] " + " / ".join(picks) if picks else ""


def build_status(use_llm: bool, *, chat_ready: bool | None = None,
                 vector_ready: bool | None = None) -> str:
    """返回运行状态。参数可注入，便于测试，不依赖本机模型是否安装。"""
    chat_cfg = llm.settings()
    emb_cfg = embeddings.settings()
    if chat_ready is None:
        chat_ready = llm.available()
    if vector_ready is None:
        vector_ready = embeddings.available(refresh=True)
    index = vector_index.stats()

    if not use_llm:
        chat_state = "已关闭，当前使用规则版"
    elif chat_ready:
        chat_state = "已开启"
    else:
        chat_state = "不可用，调用时自动回退规则版"

    lines = [
        "当前运行状态：",
        "  聊天模型：" + chat_cfg["provider"] + " / " + chat_cfg["model"]
        + "（" + chat_state + "）",
        "    地址：" + chat_cfg["base_url"],
        "  向量模型：" + emb_cfg["provider"] + " / " + emb_cfg["model"]
        + "（" + ("可用" if vector_ready else "不可用") + "）",
        "    地址：" + emb_cfg["base_url"],
    ]

    if index.get("built"):
        index_line = ("  向量索引：已建立，" + str(index.get("count", 0))
                      + " 条，模型 " + str(index.get("model") or "未知"))
        if str(index.get("model") or "") != emb_cfg["model"]:
            index_line += "（与当前配置不一致，首次检索时重建）"
    else:
        index_line = "  向量索引：未建立，首次检索时自动创建"
    lines.append(index_line)

    if vector_ready:
        lines.append("  检索模式：混合检索（稀疏 0.3 / 稠密 1.0）")
        lines.append("  降级状态：正常，未降级")
    else:
        lines.append("  检索模式：纯关键词（自动降级）")
        lines.append("  降级状态：已降级；向量服务或模型不可用")
    return "\n".join(lines)


def main(argv: list) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = [a for a in argv[1:] if a]
    verbose = "--verbose" in args
    if verbose:
        args.remove("--verbose")
    configure_logging(verbose=verbose)
    user_id = nightdata.DEFAULT_USER_ID
    nights = nightdata.load_dataset(user_id)
    night = nights[-1]
    use_llm = True
    if "--no-llm" in args:
        use_llm = False
        args.remove("--no-llm")

    if args and args[0] == "--report":
        print(report.render(night)["text"])
        return 0

    if args:
        question = " ".join(args)
        selection = nightdata.resolve_night(user_id, nights, question)
        if selection.night is not None:
            night = selection.night
        result = answer(
            UserQuery(question=question, use_llm=use_llm, user_id=user_id,
                      time_status=selection.status, time_note=selection.note,
                      available_dates=nightdata.dataset_dates(nights)),
            night,
        )
        if result.mode.startswith(("设备数据", "报告生成")):
            result.detail = result.detail + " · " + selection.note
        print(result.text)
        print("")
        print("  [模式] " + result.mode + " | " + result.detail)
        hint = option_hint(result)
        if hint:
            print(hint)
        if result.sources:
            print("  [来源] " + "、".join(result.sources))
        return 0

    print(BANNER)
    if use_llm and not llm.available():
        use_llm = False
        print("[提示] 大模型未就绪，当前使用规则版：产品问题会直接给出资料原文。")
        print(llm.describe())
        print("")
    # 预热向量可用性探测：否则第一次提问会多等 1~2 秒
    embeddings.available()
    stats = product_kb.stats()
    if stats["documents"]:
        print("[资料] 已加载 " + str(len(stats["documents"])) + " 份产品资料，共 "
              + str(stats["chunks"]) + " 个切片。输入 /docs 查看清单。")
        print("")

    history = []
    while True:
        try:
            question = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if not question:
            continue
        if question in ("/quit", "/exit", "/q"):
            break
        if question == "/help":
            print(BANNER)
            continue
        if question == "/clear":
            history.clear()
            print("[已清空对话历史]")
            continue
        if question == "/status":
            print(build_status(use_llm))
            print("")
            continue
        if question == "/docs":
            product_kb.reload_chunks()
            data = product_kb.stats()
            if not data["documents"]:
                print("  没有加载到产品资料。把 .md / .txt / .docx 放进 product_docs/user/ 即可。")
            else:
                print("  对外资料（可用于回答用户）：")
                for name in data["user_documents"]:
                    print("    " + name)
                if data["internal_documents"]:
                    print("  内部资料（默认不用于回答用户）：")
                    for name in data["internal_documents"]:
                        print("    " + name)
                if data.get("example_documents"):
                    print("  示例资料（演示用占位内容，请勿当作正式参数）：")
                    for name in data["example_documents"]:
                        print("    " + name)
                print("  目录：" + data["directory"])
            continue
        if question == "/knowledge":
            for kid, title, category in knowledge.all_titles():
                print("  " + kid.ljust(34) + title + "  [" + category + "]")
            continue
        if question == "/facts":
            for item in night.facts:
                print("  [" + item.fid + "] " + item.text)
                print("      来源：" + item.source + " | 计算：" + item.calculation)
            continue
        if question == "/report":
            print("（当前数据夜：" + str(night.raw.get("record_date")) + "）")
            print(report.render(night)["text"])
            continue
        if question.startswith("/safety"):
            target = question[len("/safety"):].strip() or "(空)"
            verdict = safety.check(target)
            print("  判定：" + verdict.kind + ("" if verdict.kind == "in_scope" else " · " + verdict.reason))
            continue
        if question.startswith("/llm"):
            value = question[len("/llm"):].strip().lower()
            if value == "on":
                use_llm = True
                print("[大模型：开启]" if llm.available() else "[提示] 配置看起来不可用，仍已开启，失败会自动回退。")
            elif value == "off":
                use_llm = False
                print("[大模型：关闭]")
            elif value == "test":
                print(llm.describe())
                print("连通测试：" + llm.test_connection())
            elif value == "":
                print(llm.describe())
                print("  当前状态：" + ("开启" if use_llm else "关闭"))
            else:
                print("用法：/llm 查看 | /llm on | /llm off | /llm test")
            continue

        question, pending_note = resolve_pending_confirmation(question, history)
        selection = nightdata.resolve_night(user_id, nights, question)
        if selection.night is not None:
            night = selection.night
        result = answer(UserQuery(question=question, history=history,
                                  use_llm=use_llm, user_id=user_id,
                                  time_status=selection.status,
                                  time_note=selection.note,
                                  available_dates=nightdata.dataset_dates(nights)), night)
        if result.mode.startswith(("设备数据", "报告生成")):
            detail_note = pending_note or selection.note
            result.detail = result.detail + " · " + detail_note
        print("")
        print(result.text)
        print("")
        print("  [模式] " + result.mode + " | " + result.detail)
        hint = option_hint(result)
        if hint:
            print(hint)
        if result.sources:
            print("  [来源] " + "、".join(result.sources))
        print("")
        history.extend(history_messages(question, result, night))

    print("再见。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
