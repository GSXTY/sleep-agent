"""BM25 检索（纯标准库）。

为什么需要它：现在的关键词检索是手写打分 + 固定阈值，阈值是在 146 个切片上调出来的；
文档变多以后这些常数会失准。BM25 的 IDF 由语料统计自动决定，
越常见的词权重越低，不需要手工调阈值。

中文处理：不引入分词器，用「汉字二元组 + 英文词」作为 term。
这是中文检索里常见的无分词方案，配合 BM25 的 IDF 效果稳定。
注意这里不做停用词删除——IDF 会自动压低「怎么」「什么」这类高频词的权重，
比手工维护停用词表更不容易过拟合。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

K1 = 1.2          # 词频饱和参数
B = 0.75          # 文档长度归一化强度


def tokenize(text: str) -> list:
    """把文本切成检索用的 term：汉字二元组 + 英文/数字词。"""
    normalized = re.sub(r"\s+", "", str(text).lower())
    terms = []
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    terms.extend(re.findall(r"[a-z]+\d*|\d+(?:\.\d+)?", normalized))
    return terms


class BM25Index:
    """倒排索引 + BM25 打分。

    规模说明：几千到几万个切片都可以常驻内存，不需要向量库。
    """

    def __init__(self, documents: list, k1: float = K1, b: float = B):
        # documents: [{"key": 唯一键, "text": 用于检索的文本, "payload": 原始信息}]
        self.k1 = k1
        self.b = b
        self.documents = documents
        self.postings = {}          # term -> {doc_index: tf}
        self.lengths = []

        for index, document in enumerate(documents):
            terms = tokenize(document["text"])
            self.lengths.append(len(terms))
            counts = {}
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
            for term, tf in counts.items():
                self.postings.setdefault(term, {})[index] = tf

        total = len(self.lengths) or 1
        self.avg_length = sum(self.lengths) / total
        self.total_docs = len(documents)
        # IDF 由语料自动决定：df 越大权重越低
        self.idf = {
            term: math.log(1 + (self.total_docs - len(postings) + 0.5) / (len(postings) + 0.5))
            for term, postings in self.postings.items()
        }

    def search(self, query: str, top_k: int = 10) -> list:
        query_terms = tokenize(query)
        if not query_terms or not self.documents:
            return []
        scores = {}
        for term in set(query_terms):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self.idf[term]
            for index, tf in postings.items():
                length = self.lengths[index]
                denominator = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_length or 1))
                scores[index] = scores.get(index, 0.0) + idf * tf * (self.k1 + 1) / denominator
        ranked = sorted(scores.items(), key=lambda item: -item[1])[:top_k]
        results = []
        for index, score in ranked:
            document = self.documents[index]
            results.append({"score": round(score, 4), **(document["payload"] or {})})
        return results


def build_from_chunks(chunks) -> BM25Index:
    """从 product_kb 的切片构建索引。章节名也进检索文本——
    很多时候问题的答案就是章节标题本身（例如「连接手机 App」）。"""
    documents = []
    for chunk in chunks:
        documents.append({
            "key": chunk.doc + "|" + chunk.section + "|" + str(chunk.index),
            "text": chunk.section + "\n" + chunk.text,
            "payload": {
                "doc": chunk.doc,
                "section": chunk.section,
                "text": chunk.text,
                "visibility": chunk.visibility,
                "index": chunk.index,
            },
        })
    return BM25Index(documents)
