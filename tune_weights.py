"""扫 RRF 权重：找出稀疏与稠密的最佳配比。

用法：
    python tune_weights.py

背景：实测出现过「纯向量 R@1 86%、混合 R@1 82%」——说明默认权重（稀疏 1.0 / 稠密 0.7）
让较弱的稀疏路压过了较强的稠密路。这个脚本把常见配比一次跑完，按 R@1 排序。

选完之后把最优配比写进 sleepagent/retrieval_constants.py 的 RRF_WEIGHTS，
并同步更新 tests/test_retrieval_constants.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_retrieval as ev
from sleepagent import embeddings, product_kb

# (稀疏权重, 稠密权重)
CANDIDATES = [
    (1.0, 0.0),   # 纯关键词（等价于只用稀疏）
    (0.0, 1.0),   # 纯向量
    (1.0, 0.7),   # 当前默认
    (1.0, 1.0),
    (0.7, 1.0),
    (0.5, 1.0),
    (0.3, 1.0),
]


def score_for(weights: tuple) -> tuple:
    def retriever(question: str):
        lexical = product_kb.lexical_search(question, top_k=10)
        dense = product_kb.dense_search(question, top_k=10)
        if not dense:
            return lexical[:3]
        return product_kb._rrf_fuse([lexical, dense], top_k=3, weights=weights)

    hits1 = hits3 = 0
    reciprocal = 0.0
    for question, expected in ev.CASES:
        words = expected if isinstance(expected, list) else [expected]
        results = retriever(question)
        rank = next((i for i, hit in enumerate(results, 1)
                     if any(word in str(hit.get("section", "")) for word in words)), None)
        if rank == 1:
            hits1 += 1
        if rank and rank <= 3:
            hits3 += 1
        if rank:
            reciprocal += 1.0 / rank
    total = len(ev.CASES)
    return hits1 / total, hits3 / total, reciprocal / total


def main() -> int:
    if not embeddings.available():
        print("向量不可用，无法扫权重。先确认 Ollama 在运行且已 pull "
              + embeddings.settings()["model"] + "。")
        return 1

    print("模型: " + embeddings.settings()["model"] + "  用例: " + str(len(ev.CASES)))
    print()
    print("稀疏权重  稠密权重    R@1    R@3    MRR")
    rows = []
    for sparse, dense in CANDIDATES:
        r1, r3, mrr = score_for((sparse, dense))
        rows.append((sparse, dense, r1, r3, mrr))
        print(("%8.1f%10.1f" % (sparse, dense))
              + ("%7.0f%%%6.0f%%" % (r1 * 100, r3 * 100))
              + ("%8.3f" % mrr))

    best = max(rows, key=lambda row: (row[2], row[3], row[4]))
    print()
    print("按 R@1 最优：稀疏 " + str(best[0]) + " / 稠密 " + str(best[1])
          + "  → R@1 " + str(round(best[2] * 100)) + "%  R@3 " + str(round(best[3] * 100)) + "%")
    print("把这一组写进 sleepagent/retrieval_constants.py 的 RRF_WEIGHTS。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
