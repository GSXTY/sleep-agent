"""检索质量评估：对比不同检索方案在同一批真实问题上的表现。

用法：
    python eval_retrieval.py                 对比 现有关键词 / BM25 / 混合
    python eval_retrieval.py --verbose       打印未命中的问题
    python eval_retrieval.py --diagnose      逐条诊断：召回、闸门、还是排序

指标说明：
    Recall@1  正确章节排在第 1 位的比例
    Recall@3  正确章节排进前 3 的比例
    MRR       正确章节排名的倒数均值（1 表示总是排第 1）

改动检索逻辑后请重跑本脚本，不要凭感觉判断有没有变好。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sleepagent import bm25, embeddings, product_kb
from eval_cases import load_cases

# 真实产品问题 -> 期望命中的章节名（子串匹配）
BASE_CASES = [
    # (问题, 可接受的章节名列表)。两份资料用词不同（说明书说「工作模式」，
    # 技术要求说「通气模式」），所以要允许多种正确命中。
    ("YA70 有哪些型号", ["产品型号", "客户应该如何选择 YA70系列的不同型号"]),
    ("支持哪些通气模式", ["工作模式", "通气模式"]),
    ("滤芯多久换一次", ["更换空气滤芯", "滤芯更换", "空气滤芯多久更换一次"]),
    ("水箱怎么注水", ["注水"]),
    ("管路怎么连接", ["连接管路", "管路与接口"]),
    ("面罩脱落会自动停吗", ["开始/停止治疗", "启动与停机"]),
    ("怎么连接手机app", ["APP下载", "App 登录", "连接手机", "APP 怎么连接", "APP 怎样绑定 YA70A"]),
    ("面罩怎么清洗", ["鼻罩", "清洁"]),
    ("怎么消毒设备表面", ["消毒"]),
    ("湿化器怎么清洁", ["清洁湿化器", "清洁"]),
    ("保修多久", ["保修"]),
    ("有哪些禁忌症", ["禁忌"]),
    ("安全使用要注意什么", ["安全使用原则", "注意"]),
    ("延时升压怎么设置", ["延时升压"]),
    ("机器噪音多大", ["噪音"]),
    ("技术指标有哪些", ["技术指标"]),
    ("设备不用了怎么处理", ["废弃处理"]),
    ("给别人用要注意什么", ["交给另一位患者"]),
    ("压力释放是什么", ["患者可设置的参数", "压力释放"]),
    ("管路口径是多少", ["管路与接口"]),
    ("加湿器有几个档位", ["加温湿化器", "加湿等级"]),
    ("连续能工作多久", ["连续工作时间"]),
]


def all_cases() -> list:
    """内置用例 + 回流用例（eval/cases.jsonl 里带 expected_section 的）。"""
    cases = list(BASE_CASES)
    seen = {question for question, _ in cases}
    for item in load_cases():
        section = item.get("expected_section")
        if not section:
            continue
        question = item["query"]
        if question in seen:
            continue
        cases.append((question, section if isinstance(section, list) else [section]))
        seen.add(question)
    return cases


def _rank_of(hits: list, expected: list):
    for index, hit in enumerate(hits, 1):
        if any(word in str(hit.get("section", "")) for word in expected):
            return index
    return None


def diagnose(cases: list) -> int:
    """回答"正确答案到底在哪一层丢的"。

    gated    正常覆盖率闸门下的关键词排名
    relaxed  放宽闸门后的排名（能区分"闸门挡掉"和"根本没召回"）
    dense    向量路排名
    fused    最终混合排名（含标题命中前置）
    """
    has_dense = embeddings.available()
    rows = []
    for question, expected in cases:
        gated = product_kb.lexical_search(question, top_k=50)
        relaxed = product_kb.lexical_search(question, top_k=50,
                                            min_score=0, min_coverage=0)
        dense = (product_kb.dense_search(question, top_k=50) or []) if has_dense else []
        fused = product_kb.retrieve(question, top_k=50)

        gated_rank = _rank_of(gated, expected)
        relaxed_rank = _rank_of(relaxed, expected)
        dense_rank = _rank_of(dense, expected)
        fused_rank = _rank_of(fused, expected)

        if fused_rank == 1:
            kind = "正常"
        elif fused_rank:
            kind = "排序（第 " + str(fused_rank) + " 位）"
        elif relaxed_rank and not gated_rank:
            kind = "覆盖率闸门挡掉"
        elif dense_rank or relaxed_rank:
            kind = "融合时丢失"
        else:
            kind = "召回失败（语料/切词）"
        rows.append((question, expected, gated_rank, relaxed_rank,
                     dense_rank, fused_rank, kind))

    print("=" * 78)
    print("逐条诊断（排名为空表示前 50 名里没有正确章节）")
    print("=" * 78)
    print("问题".ljust(22) + "闸门".rjust(5) + "放宽".rjust(5)
          + "向量".rjust(5) + "融合".rjust(5) + "  结论")
    for question, expected, gated_rank, relaxed_rank, dense_rank, fused_rank, kind in rows:
        print(question[:20].ljust(22)
              + str(gated_rank or "-").rjust(5)
              + str(relaxed_rank or "-").rjust(5)
              + str(dense_rank or "-").rjust(5)
              + str(fused_rank or "-").rjust(5)
              + "  " + kind)

    print()
    counts = {}
    for row in rows:
        counts[row[-1]] = counts.get(row[-1], 0) + 1
    for kind, count in sorted(counts.items(), key=lambda item: -item[1]):
        print("  " + str(count).rjust(2) + " 条  " + kind)
    # 诊断是信息性命令：有排序/召回问题也返回 0，避免 CI 误判为失败
    return 0


def evaluate(name: str, retriever, cases: list, verbose: bool = False) -> dict:
    first = top3 = 0
    reciprocal = 0.0
    misses = []
    for question, expected in cases:
        hits = retriever(question)
        expected_list = expected if isinstance(expected, list) else [expected]
        rank = next((i for i, hit in enumerate(hits, 1)
                     if any(word in str(hit.get("section", "")) for word in expected_list)), None)
        if rank == 1:
            first += 1
        if rank and rank <= 3:
            top3 += 1
        if rank:
            reciprocal += 1.0 / rank
        else:
            misses.append(question + " → " + " / ".join(str(h.get("section"))[:16] for h in hits[:2]))
    total = len(cases)
    result = {"name": name, "recall1": first / total, "recall3": top3 / total,
              "mrr": reciprocal / total, "misses": misses}
    print(name.ljust(20) + "R@1 " + (str(round(result["recall1"] * 100)) + "%").rjust(4)
          + "   R@3 " + (str(round(result["recall3"] * 100)) + "%").rjust(4)
          + "   MRR " + format(result["mrr"], ".3f")
          + "   未命中 " + str(len(misses)))
    if verbose:
        for miss in misses:
            print("    · " + miss)
    return result


def main() -> int:
    verbose = "--verbose" in sys.argv
    cases = all_cases()
    chunks = list(product_kb.load_chunks())
    index = bm25.build_from_chunks(chunks)
    counts = {
        visibility: sum(1 for chunk in chunks if chunk.visibility == visibility)
        for visibility in ("user", "internal", "examples")
    }
    print("检索切片数: " + str(len(chunks)) + "  |  用例数: " + str(len(cases)))
    print("切片来源: user=" + str(counts["user"])
          + " / internal=" + str(counts["internal"])
          + " / examples=" + str(counts["examples"]))
    cfg = embeddings.settings()
    print("向量模型: " + cfg["provider"] + " / " + cfg["model"]
          + ("  可用的" if embeddings.available() else "  不可用（只跑稀疏）"))
    if cfg["provider"] == "fake":
        print()
        print("  [!] 当前用的是 fake 向量：它只反映字面重合、没有语义能力。")
        print("      下面的向量/混合数字只说明链路已打通，**不能用来判断效果**。")
    print()

    if "--diagnose" in sys.argv:
        return diagnose(cases)

    evaluate("A 纯关键词", lambda q: product_kb.lexical_search(q, top_k=3), cases, verbose)
    evaluate("B BM25", lambda q: index.search(q, top_k=3), cases, verbose)

    if embeddings.available():
        def dense_only(question, k=3):
            hits = product_kb.dense_search(question, top_k=k)
            return hits or []

        evaluate("C 纯向量", dense_only, cases, verbose)
        evaluate("D 混合(稀疏+向量)", lambda q: product_kb.retrieve(q, top_k=3), cases, verbose)
        print()
        print("判读标准：D 要明显高于 A 才值得保留向量这一路；")
        print("若 D 与 A 持平或更低，说明当前语料上向量没有带来增量，应保持纯关键词。")
    else:
        print()
        print("向量化不可用，跳过 C/D。启动 Ollama 并拉取模型后重跑：")
        print("  ollama pull " + cfg["model"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
