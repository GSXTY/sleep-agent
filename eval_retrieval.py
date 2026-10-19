"""检索质量评估：对比不同检索方案在同一批真实问题上的表现。

用法：
    python eval_retrieval.py                 对比 现有关键词 / BM25 / 混合
    python eval_retrieval.py --verbose       打印未命中的问题

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

# 真实产品问题 -> 期望命中的章节名（子串匹配）
CASES = [
    # (问题, 可接受的章节名列表)。两份资料用词不同（说明书说「工作模式」，
    # 技术要求说「通气模式」），所以要允许多种正确命中。
    ("YA70 有哪些型号", ["产品型号"]),
    ("支持哪些通气模式", ["工作模式", "通气模式"]),
    ("滤芯多久换一次", ["更换空气滤芯", "滤芯更换"]),
    ("水箱怎么注水", ["注水"]),
    ("管路怎么连接", ["连接管路", "管路与接口"]),
    ("面罩脱落会自动停吗", ["开始/停止治疗", "启动与停机"]),
    ("怎么连接手机app", ["APP下载", "App 登录", "连接手机"]),
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
    ("加湿器有几个档位", ["加温湿化器"]),
    ("连续能工作多久", ["连续工作时间"]),
]


def evaluate(name: str, retriever, verbose: bool = False) -> dict:
    first = top3 = 0
    reciprocal = 0.0
    misses = []
    for question, expected in CASES:
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
    total = len(CASES)
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
    chunks = [chunk for chunk in product_kb.load_chunks() if chunk.visibility == "user"]
    index = bm25.build_from_chunks(chunks)
    print("对外切片数: " + str(len(chunks)) + "  |  用例数: " + str(len(CASES)))
    cfg = embeddings.settings()
    print("向量模型: " + cfg["provider"] + " / " + cfg["model"]
          + ("  可用的" if embeddings.available() else "  不可用（只跑稀疏）"))
    if cfg["provider"] == "fake":
        print()
        print("  [!] 当前用的是 fake 向量：它只反映字面重合、没有语义能力。")
        print("      下面的向量/混合数字只说明链路已打通，**不能用来判断效果**。")
    print()

    evaluate("A 纯关键词", lambda q: product_kb.lexical_search(q, top_k=3), verbose)
    evaluate("B BM25", lambda q: index.search(q, top_k=3), verbose)

    if embeddings.available():
        def dense_only(question, k=3):
            hits = product_kb.dense_search(question, top_k=k)
            return hits or []

        evaluate("C 纯向量", dense_only, verbose)
        evaluate("D 混合(稀疏+向量)", lambda q: product_kb.retrieve(q, top_k=3), verbose)
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
