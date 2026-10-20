r"""产品文档知识库：读取产品资料并做检索。

支持三种文件，全部只用标准库：
  .md / .txt  —— 直接读
  .docx       —— docx 本质是 zip，解开 word/document.xml 取文本

文本抽取踩过的两个坑（都由真实的注册用技术要求文档暴露出来）：
  1. <w:tblPr>/<w:tcPr> 这类标签也以 <w:t 开头，用 <w:t[^>]*> 匹配会把 XML 当成正文读进来。
     正确写法是 <w:t(?:\s[^>]*)?>，要求标签名后面要么直接是 >，要么是空白。
  2. 中文注册文档常把几乎所有段落都设成 Heading 样式，靠样式判标题会让正文全部被丢掉。
     所以判标题还要加上长度和句末标点的约束，并且表格内容一律当正文。
"""
from __future__ import annotations

import html
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DOCS_DIR_NAME = "product_docs"
# 默认检索 user、internal、examples 三个目录。
# 来源会带上 visibility，入口层可以区分正式说明书、内部要求和示例资料。
# 直接放在根目录的文件按对用户资料处理，保持向后兼容。
VISIBILITY_USER = "user"
VISIBILITY_INTERNAL = "internal"
VISIBILITY_EXAMPLE = "examples"
VISIBILITY_ALL = "all"
CHUNK_CHARS = 480          # 每个切片的软上限
MIN_SCORE = 3.0            # 低于这个分数视为没有命中
MIN_COVERAGE = 0.3         # 内容词重合覆盖率下限

SUPPORTED_SUFFIXES = (".md", ".txt", ".docx")

from . import bm25, embeddings, retrieval_constants, vector_index

# <w:t> 的正确匹配：标签名后必须是 > 或空白，否则会命中 <w:tblPr> 这类标签
WT_PATTERN = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)

# 用户口语与文档用词不一致时的映射。文档写"加温湿化器"，用户说"加湿器"，
# 纯 bigram 匹配对不上，这类同义词只能人工维护 —— 这也是关键词检索的固有成本。
SYNONYMS = (
    ("加湿器", "加温湿化器"), ("湿化器", "加温湿化器"), ("加湿", "加温湿化"),
    ("机器", "设备"), ("呼吸机", "设备"), ("机子", "设备"),
    ("手机", "app"), ("移动端", "app"), ("软件", "app"),
    ("分贝", "噪音"), ("声音", "噪音"),
    ("能用多久", "连续工作时间"), ("管路口径", "接口管径"),
    ("多沉", "重量"), ("多重", "重量"),
    ("密码", "登录"), ("登陆", "登录"),
)

STOP_TOKENS = {
    "怎么", "什么", "么样", "怎幺", "怎样", "如何", "是否", "可以", "需要", "为什",
    "一下", "的话", "这样", "那样", "这个", "那个", "哪些", "哪个", "多少", "以及",
    "还有", "就是", "但是", "因为", "所以", "然后", "现在", "请问", "帮我", "我想",
    "一个", "一些", "不能", "没有", "是不", "是的", "了吗", "了呢", "时候", "问题",
    "告诉", "知道", "了解", "介绍", "我们", "你们", "他们", "自己",
    "多久", "多长时间", "几个", "几档", "哪些", "可以", "能否", "是不是", "有没有",
    "我的", "你的", "他的", "咱们", "我想", "帮我",
}


@dataclass
class Chunk:
    doc: str
    section: str
    text: str
    index: int
    visibility: str = "user"      # user = 可对用户回答；internal = 仅内部使用


def docs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / DOCS_DIR_NAME


# ---------- 文本抽取 ----------

def _paragraph_text(fragment: str) -> str:
    return html.unescape("".join(WT_PATTERN.findall(fragment))).strip()


def _table_to_text(table_xml: str) -> str:
    """把表格按行转成 单元格 | 单元格 的形式，保留行列对应关系。"""
    rows = []
    for row_xml in re.findall(r"<w:tr[ >].*?</w:tr>", table_xml, re.S):
        cells = [_paragraph_text(cell) for cell in re.findall(r"<w:tc[ >].*?</w:tc>", row_xml, re.S)]
        cells = [cell.replace("\n", " ").strip() for cell in cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _inline_tables(xml: str) -> str:
    """把表格就地替换成伪段落，这样后续处理不需要区分正文和表格。"""
    def replace(match):
        text = _table_to_text(match.group(0))
        if not text:
            return "<w:p></w:p>"
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return "<w:p><w:t>" + escaped.replace("\n", "</w:t></w:p><w:p><w:t>") + "</w:t></w:p>"
    return re.sub(r"<w:tbl>.*?</w:tbl>", replace, xml, flags=re.S)


def _is_heading_paragraph(line: str, style: str) -> bool:
    """中文注册文档经常把所有段落都设成 Heading 样式，光看样式会把正文全丢掉。"""
    if len(line) > 40:
        return False
    if line.endswith(("。", "；", "，", "：", "!", "！", ";", ",", ".")):
        return False
    if " | " in line:            # 表格行一律当正文
        return False
    if style:
        return bool(re.search(r"[Hh]eading|^\d+$", style))
    return bool(re.match(r"^\d+(?:\.\d+){1,3}\s*\S", line))


def _read_docx(path: Path) -> list:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "word/document.xml" not in names:
            raise ValueError("不是有效的 docx：" + path.name + "（缺少 word/document.xml）")
        xml = archive.read("word/document.xml").decode("utf-8", "replace")

    xml = _inline_tables(xml)
    lines = []
    for paragraph in re.split(r"</w:p>", xml):
        line = _paragraph_text(paragraph)
        if not line:
            continue
        style = re.search(r'<w:pStyle[^>]*w:val="([^"]+)"', paragraph)
        style = style.group(1) if style else ""
        if _is_heading_paragraph(line, style):
            line = "# " + line
        lines.append(line)
    return lines


def _read_text_file(path: Path) -> list:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _read_lines(path: Path) -> list:
    if path.suffix.lower() == ".docx":
        return _read_docx(path)
    return _read_text_file(path)


# ---------- 切片 ----------

def _is_heading(line: str) -> bool:
    """判标题。

    关键：不能把编号列表项当成标题。原来的模式会把
    「1. 手机安装配套 App 并完成注册。」也判成标题，而标题行是不进正文的，
    于是整个列表的内容被静默丢掉——「连接手机 App」那一节就是这样消失的。
    """
    if line.startswith("#"):
        return True
    if " | " in line:
        return False
    stripped = line.rstrip()
    # 以句末标点结尾的是正文或列表项，不是标题
    if stripped.endswith(("。", "；", "，", "！", "？", ".", ";", ",", "、", "：")):
        return False
    if re.match(r"^第[一二三四五六七八九十百]+[章节]", line):
        return True
    # 至少两级编号（3.4.2 这类）才按章节处理
    if re.match(r"^\d+(?:\.\d+)+[、.．]?\s*\S", line):
        return True
    # 单级编号只有很短时才可能是标题，例如「1. 治疗压力」
    return bool(re.match(r"^\d+[、.．]\s*\S{1,12}$", line))


def _chunk(doc_name: str, lines: list, visibility: str = VISIBILITY_USER) -> list:
    chunks = []
    section = doc_name
    buffer = []

    def flush(section_override: str | None = None):
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                chunks.append(Chunk(doc=doc_name, section=section, text=text,
                                    index=len(chunks), visibility=visibility))
                if section_override:
                    chunks[-1].section = section_override
            buffer.clear()

    for line in lines:
        if _is_heading(line):
            flush()
            section = line.lstrip("# ").strip() or section
            continue
        if " | " in line and "故障排除" in section:
            # 故障排除/规格表的一行通常就是一个独立事实。
            # 如果把整张表合并成一个切片，单个症状的向量会被其他行稀释。
            flush()
            buffer.append(line)
            row_label = line.split(" | ", 1)[0].strip()
            flush(section + (" · " + row_label if row_label and len(row_label) <= 24 else ""))
            continue
        buffer.append(line)
        if sum(len(item) for item in buffer) >= CHUNK_CHARS:
            flush()
    flush()
    return chunks


@lru_cache(maxsize=1)
def load_chunks() -> tuple:
    directory = docs_dir()
    if not directory.is_dir():
        return tuple()
    chunks = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        rel_parts = [part.lower() for part in path.relative_to(directory).parts[:-1]]
        # 只认 user/ 与 internal/ 两个子目录（以及直接放在根目录的文件）；
        # examples/ 之类的目录用于存放示例，不参与检索。
        if rel_parts and rel_parts[0] not in (
            VISIBILITY_USER, VISIBILITY_INTERNAL, VISIBILITY_EXAMPLE
        ):
            continue
        if VISIBILITY_INTERNAL in rel_parts:
            visibility = VISIBILITY_INTERNAL
        elif VISIBILITY_EXAMPLE in rel_parts:
            visibility = VISIBILITY_EXAMPLE
        else:
            visibility = VISIBILITY_USER
        try:
            chunks.extend(_chunk(path.name, _read_lines(path), visibility))
        except Exception as exc:
            chunks.append(Chunk(doc=path.name, section="读取失败",
                                text="文件无法解析：" + str(exc),
                                index=len(chunks), visibility=visibility))
    return tuple(chunks)


def reload_chunks() -> None:
    load_chunks.cache_clear()
    _sparse_cache.clear()


# ---------- 检索 ----------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _tokens(text: str) -> set:
    normalized = _normalize(text)
    latin = set(re.findall(r"[a-z]+\d*|\d+(?:\.\d+)?", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    bigrams = {chinese[i:i + 2] for i in range(max(len(chinese) - 1, 0))}
    return latin | bigrams


def _is_strong(token: str) -> bool:
    if token.isdigit():
        return False
    return len(token) >= 2


@lru_cache(maxsize=1)
def _synonyms() -> tuple:
    """同义词表：优先读项目根目录的 synonyms.json，读不到就用内置默认值。"""
    override = Path(__file__).resolve().parents[1] / "synonyms.json"
    if override.is_file():
        try:
            data = json.loads(override.read_text(encoding="utf-8"))
            mapping = data.get("map") if isinstance(data, dict) else None
            if isinstance(mapping, dict) and mapping:
                return tuple((str(k), str(v)) for k, v in mapping.items())
        except Exception:
            pass
    return SYNONYMS


def _apply_synonyms(text: str) -> str:
    """口语说法映射到文档用词。

    已经出现目标词时不再替换：否则「如何连接手机app」会被换成「连接appapp」，
    反而把「手机」这个本来能命中的词弄丢了。
    """
    for spoken, written in _synonyms():
        if spoken in text and written not in text:
            text = text.replace(spoken, written)
    return text


def _expand_synonyms_inplace(tokens: set) -> set:
    return tokens


def _content_tokens(text: str) -> set:
    """先挖掉停用词再切 bigram。

    这一步很关键：「今天天气怎么样」过去会切出「气怎」这个跨词边界的假 token，
    撞上文档里的「喘不上气怎么办」。把「怎么」「么样」先删除后，
    假 token 根本不会产生，「今天天气」只会切出 今天/天天/天气。
    """
    normalized = _apply_synonyms(_normalize(text))
    for word in sorted(STOP_TOKENS, key=len, reverse=True):
        if word.isascii():
            normalized = re.sub(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", " ", normalized)
        else:
            normalized = normalized.replace(word, " ")

    tokens = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        # 只切双字，不保留单字：「是」「的」这类单字单独成为 token 只会制造假命中
        tokens |= {run[i:i + 2] for i in range(len(run) - 1)}
    tokens |= set(re.findall(r"[a-z]+\d*", normalized))
    return tokens


def _score(question: str, chunk: Chunk, min_coverage: float = MIN_COVERAGE) -> float:
    normalized = _normalize(question)
    section = _normalize(chunk.section)
    body = _normalize(chunk.text)

    question_content = _content_tokens(question)
    document_content = _content_tokens(chunk.section + chunk.text)
    overlap = question_content & document_content
    strong_overlap = {token for token in overlap if _is_strong(token)}
    if not strong_overlap:
        return 0.0

    coverage = len(overlap) / len(question_content) if question_content else 0.0
    if coverage < min_coverage:
        return 0.0

    alnum_overlap = {token for token in overlap if any(ch.isdigit() for ch in token)}
    score = 2.0 * len(strong_overlap) + 5.0 * len(alnum_overlap)
    if question_content:
        score += coverage * 6.0
    if section and (section in normalized or normalized in section):
        score += 6.0
    for size in (6, 4):
        for i in range(0, max(len(normalized) - size + 1, 0)):
            if normalized[i:i + size] in body:
                score += 1.0
                break
    return round(score, 4)


# 稀疏检索的三个组成部分（都在实测里验证过）：
#   1. BM25 打分 —— IDF 随语料自适应，长切片不会凭体量取胜
#   2. 章节名加成 —— 问题里出现章节名时强信号，BM25 本身没有这个先验
#   3. 覆盖率闸门 —— 过滤「只匹配到一个词」的弱命中
# 实测（97 个真实切片，22 条用例）：R@1 68% / R@3 91%，高于纯手写打分的 45% / 82%。
SECTION_BONUS = retrieval_constants.SECTION_BONUS
SECTION_TOKEN_BONUS = retrieval_constants.SECTION_TOKEN_BONUS
MIN_COVERAGE_DEFAULT = retrieval_constants.MIN_COVERAGE_DEFAULT
# 分数下限实测无法区分合法问题与无关问题（合法最低 2.95、无关最高 4.92），
# 真正起作用的是覆盖率闸门，所以这里默认不加分数下限。
SPARSE_MIN_SCORE = retrieval_constants.SPARSE_MIN_SCORE
RETRIEVAL_POOL = retrieval_constants.RETRIEVAL_POOL

_sparse_cache = {}
logger = logging.getLogger("sleepagent.retrieval")


def _sparse(visibility: str) -> dict:
    """按可见性构建并缓存 BM25 索引与切片内容词。"""
    total = len(load_chunks())
    cached = _sparse_cache.get(visibility)
    if cached and cached["total"] == total:
        return cached
    chunks = [chunk for chunk in load_chunks()
              if visibility == "all" or chunk.visibility == visibility]
    entry = {
        "total": total,
        "chunks": chunks,
        "index": bm25.build_from_chunks(chunks),
        "tokens": {(chunk.doc, chunk.section, chunk.index): _content_tokens(chunk.section + chunk.text)
                   for chunk in chunks},
    }
    _sparse_cache[visibility] = entry
    return entry


def lexical_search(question: str, top_k: int = 10, min_score: float = None,
                   min_coverage: float = None,
                   visibility: str = VISIBILITY_ALL) -> list:
    """稀疏检索：BM25 + 章节名加成 + 覆盖率闸门。"""
    if not question.strip() or top_k <= 0:
        return []
    gate = MIN_COVERAGE_DEFAULT if min_coverage is None else min_coverage
    floor = SPARSE_MIN_SCORE if min_score is None else min_score
    sparse = _sparse(visibility)
    hits = sparse["index"].search(question, top_k=RETRIEVAL_POOL)
    if not hits:
        return []

    normalized_question = _normalize(question)
    question_tokens = _content_tokens(question)
    for hit in hits:
        section = _normalize(hit.get("section", ""))
        if section and (section in normalized_question or normalized_question in section):
            hit["score"] = round(hit["score"] + SECTION_BONUS, 4)
        section_overlap = question_tokens & _content_tokens(hit.get("section", ""))
        if section_overlap:
            hit["score"] = round(
                hit["score"] + SECTION_TOKEN_BONUS * len(section_overlap), 4
            )

    if question_tokens and gate > 0:
        kept = []
        for hit in hits:
            tokens = sparse["tokens"].get((hit["doc"], hit["section"], hit.get("index")))
            if not tokens:
                continue
            coverage = len(question_tokens & tokens) / len(question_tokens)
            if coverage >= gate:
                hit["coverage"] = round(coverage, 2)
                kept.append(hit)
        if not kept:
            # 没有任何切片的内容词覆盖率达到闸门 → 视为没有可用资料。
            # 之前写成「全部不达标时放行」，闸门等于没生效，瞎问也会命中。
            return []
        hits = kept

    if floor > 0:
        # 只看第一名是否达标来决定「有没有可用资料」；
        # 达标时保留后续名次，否则第 2、3 名会被一并切掉，R@3 明显下降。
        if not hits or hits[0]["score"] < floor:
            return []

    hits.sort(key=lambda item: (-item["score"], item["doc"], item.get("index") or 0))
    return hits[:top_k]
def _rrf_fuse(ranked_lists: list, top_k: int, weights=None, k: int = None) -> list:
    """Reciprocal Rank Fusion。

    只融合排名，不融合分数 —— 关键词分和余弦分的量纲完全不同（0~20 与 -1~1），
    归一化后再加权会随语料漂移，RRF 没有这个问题。
    """
    k = retrieval_constants.RRF_K if k is None else k
    scores = {}
    payloads = {}
    for list_index, ranked in enumerate(ranked_lists):
        weight = weights[list_index] if weights else 1.0
        for rank, hit in enumerate(ranked, 1):
            key = (hit["doc"], hit["section"], hit.get("index"))
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
            payloads.setdefault(key, hit)
    ordered = sorted(scores.items(), key=lambda item: -item[1])[:top_k]
    fused = []
    for key, score in ordered:
        item = dict(payloads[key])
        item["score"] = round(score, 6)
        item["fused"] = True
        fused.append(item)
    return fused


def dense_search(question: str, top_k: int = 10, visibility: str = VISIBILITY_ALL):
    """稠密（向量）检索。模型不可用或索引建不起来时返回 None。"""
    if not embeddings.available():
        return None
    query_vector = embeddings.embed_query(question)
    if not query_vector:
        return None
    chunks = [chunk for chunk in load_chunks()
              if visibility == "all" or chunk.visibility == visibility]
    cfg = embeddings.settings()
    entries = vector_index.ensure(chunks, embeddings.embed, cfg["model"])
    if not entries:
        return None
    hits = vector_index.search(query_vector, top_k=top_k, visibility=visibility, entries=entries)
    # 没有这条过滤，余弦会把任何问题都排出一个结果，导致所有提问都命中产品文档
    passed = [hit for hit in hits if hit["score"] >= retrieval_constants.DENSE_MIN_SCORE]
    return passed or None


def _hit_key(hit: dict) -> tuple:
    return (hit.get("doc"), hit.get("section"), hit.get("index"))


def trim_low_relevance(hits: list, min_ratio: float = 0.5, question: str = "") -> list:
    """定义类问题只保留与第一名分数接近的切片，避免长文档噪声。"""
    if not hits:
        return []
    if question:
        top_section_tokens = _content_tokens(hits[0].get("section", ""))
        question_tokens = _content_tokens(question)
        if question_tokens & top_section_tokens:
            return hits[:1]
    top_score = float(hits[0].get("score") or 0.0)
    if top_score <= 0:
        return hits[:1]
    return [hit for hit in hits
            if float(hit.get("score") or 0.0) >= top_score * min_ratio]


def _retrieve_base(question: str, top_k: int, min_score, min_coverage,
                   visibility: str, hybrid: bool, log_degrade: bool = True) -> list:
    lexical = lexical_search(question, top_k=10, min_score=min_score,
                             min_coverage=min_coverage, visibility=visibility)
    if not hybrid:
        return lexical[:top_k]
    dense = dense_search(question, top_k=10, visibility=visibility)
    if not dense:
        if log_degrade:
            logger.warning("[DEGRADE] degraded_to_lexical visibility=%s", visibility)
        return lexical[:top_k]
    return _rrf_fuse([lexical, dense], top_k=top_k,
                     weights=retrieval_constants.RRF_WEIGHTS)


def retrieve(question: str, top_k: int = 3, min_score: float = None,
             min_coverage: float = None, visibility: str = VISIBILITY_ALL,
             hybrid: bool = True) -> list:
    """混合检索入口：稀疏 + 稠密，RRF 融合。

    visibility=all 时优先保证正式 user 资料；user 命中不足 top_k 时，
    再用 internal/examples 补齐。这样内部资料和示例资料可被引用，
    但不会把正式说明书挤出前几条。
    """
    if visibility != VISIBILITY_ALL:
        return _retrieve_base(question, top_k, min_score, min_coverage,
                              visibility, hybrid)

    user_hits = _retrieve_base(question, top_k, min_score, min_coverage,
                                VISIBILITY_USER, hybrid, log_degrade=False)
    if len(user_hits) >= top_k:
        if not embeddings.available():
            logger.warning("[DEGRADE] degraded_to_lexical visibility=all")
        return user_hits[:top_k]

    all_hits = _retrieve_base(question, top_k, min_score, min_coverage,
                              VISIBILITY_ALL, hybrid)
    merged = []
    seen = set()
    for hit in user_hits + all_hits:
        key = _hit_key(hit)
        if key in seen:
            continue
        seen.add(key)
        merged.append(hit)
        if len(merged) >= top_k:
            break
    return merged


def render(hits: list) -> str:
    if not hits:
        return "没有检索到相关的产品资料。"
    blocks = []
    for i, hit in enumerate(hits, 1):
        blocks.append("[资料" + str(i) + "] 出自《" + hit["doc"] + "》" + hit["section"] + "\n" + hit["text"])
    return "\n\n".join(blocks)


def stats() -> dict:
    chunks = load_chunks()
    user_docs, internal_docs, example_docs = set(), set(), set()
    for chunk in chunks:
        if chunk.visibility == VISIBILITY_INTERNAL:
            internal_docs.add(chunk.doc)
        elif chunk.visibility == VISIBILITY_EXAMPLE:
            example_docs.add(chunk.doc)
        else:
            user_docs.add(chunk.doc)
    return {"documents": sorted(user_docs | internal_docs | example_docs),
            "user_documents": sorted(user_docs),
            "internal_documents": sorted(internal_docs),
            "example_documents": sorted(example_docs),
            "chunks": len(chunks), "directory": str(docs_dir())}
