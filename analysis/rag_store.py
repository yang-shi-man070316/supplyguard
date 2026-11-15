"""阶段06板块2: 简易 RAG 检索模块 rag_store.

从 rag_docs/ 目录加载业务保障知识文档 (Markdown/文本), 基于字符 bigram
TF-IDF 余弦相似度做检索 (纯标准库, 无外部模型依赖)。

规则 (严格按板块规格):
    - 检索结果强制携带来源引用 citations:
      [{doc: 文档名, section: 章节标题, snippet: 原文摘录}]
    - 检索不到内容 (无文档 / 相似度低于阈值) → answer 固定返回
      "无可靠依据", citations 为空列表, matched=false
    - RAG 结果经 run_rag_query() 写入 evidence_v2 辅助证据
      (evidence_type='rag', 新增类型); 检索无结果也落库, evidence
      status='ok' (检索动作成功完成), 结果在 value_json 中携带
      matched=false —— 与「API 宕机 unavailable」区分开。
    - 不触碰 task_results / dependency_checks / runs, 不影响 T1~T4 判定。

CLI 用法:
    python analysis/rag_store.py "查询文本"

纯检索函数 search_rag(query) 不依赖数据库, 可独立调用。
"""

import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

# 项目根加入 sys.path, 保证独立运行或被 backend/main.py import 时
# 都能解析 backend 包与 db 模块
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (_PROJECT_ROOT, _PROJECT_ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

RAG_DOCS_DIR = Path(__file__).resolve().parents[1] / "rag_docs"
NO_MATCH_ANSWER = "无可靠依据"   # 检索无结果固定文本 (板块规格)
MIN_SCORE = 0.10                 # 余弦相似度阈值, 低于即判无匹配
TOP_K = 2                        # 最多返回的片段数
_SNIPPET_LEN = 100               # 引用摘录长度 (字符)

_CJK_RE = re.compile(r"[一-鿿]+")
_ASCII_RE = re.compile(r"[a-z0-9]{2,}")

# 模块级缓存: 进程内只加载一次 (与项目「无热加载, 重启生效」节奏一致)
_docs_cache = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tokenize(text: str) -> list:
    """中文按相邻双字 (bigram) 切分, 英文/数字按连续片段切分。

    如 "雨雪天气车辆调度" -> ["雨雪","雪天","天气","气车","车辆","辆调","调度"];
    单字中文片段保留单字, 保证极短查询 (如 "水") 也能命中。
    """
    tokens = []
    for seg in _CJK_RE.findall(text or ""):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    tokens.extend(_ASCII_RE.findall((text or "").lower()))
    return tokens


def _parse_doc(path: Path) -> list:
    """解析知识文档为章节片段列表。

    返回 [{"doc": 文件名, "section": 章节标题, "text": 章节正文}...];
    '#' 行作为章节边界, 正文按行合并 (空行归一为换行)。
    """
    chunks = []
    doc_name = path.name
    section = doc_name  # 无任何标题时用文件名兜底
    buf = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#"):
                if buf:
                    chunks.append({"doc": doc_name, "section": section,
                                   "text": "\n".join(buf).strip()})
                section = stripped.lstrip("#").strip()
                buf = []
            elif stripped:
                buf.append(stripped)
    if buf:
        chunks.append({"doc": doc_name, "section": section,
                       "text": "\n".join(buf).strip()})
    # 丢弃空片段 (如仅标题无正文的章节)
    return [c for c in chunks if c["text"]]


def load_docs() -> list:
    """加载 rag_docs/ 全部知识片段 (进程内缓存一次)。

    单文件读取失败跳过不中断; 目录不存在/无文档返回空列表。
    """
    global _docs_cache
    if _docs_cache is not None:
        return _docs_cache
    chunks = []
    if RAG_DOCS_DIR.is_dir():
        for path in sorted(RAG_DOCS_DIR.glob("*")):
            if path.suffix.lower() not in (".md", ".txt"):
                continue
            try:
                chunks.extend(_parse_doc(path))
            except (OSError, UnicodeDecodeError):
                print("[rag_store] skip unreadable doc: %s" % path,
                      file=sys.stderr)
    _docs_cache = chunks
    return chunks


def search_rag(query: str) -> dict:
    """RAG 检索 (纯计算, 不依赖数据库, 不抛异常)。

    返回:
        {
          "query": 查询原文,
          "matched": True/False,
          "answer": 命中的知识原文拼接 (未命中为固定文本 "无可靠依据"),
          "citations": [{doc, section, snippet}...],  未命中为空列表,
          "top_score": 最高余弦相似度 (保留 4 位),
          "doc_count": 加载文档片段数,
        }
    """
    chunks = load_docs()
    query_tokens = _tokenize(query)
    base = {
        "query": query,
        "matched": False,
        "answer": NO_MATCH_ANSWER,
        "citations": [],
        "top_score": 0.0,
        "doc_count": len(chunks),
    }
    if not chunks or not query_tokens:
        return base

    # ---- idf: ln((1+N)/(1+df)) ----
    n = len(chunks)
    df = {}
    for c in chunks:
        for t in set(_tokenize(c["text"])):
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((1 + n) / (1 + df[t])) for t in df}

    # ---- 查询向量 (tf * idf) ----
    qv = {}
    for t in query_tokens:
        qv[t] = qv.get(t, 0) + 1
    qv = {t: c * idf.get(t, 0.0) for t, c in qv.items() if idf.get(t)}
    q_norm = math.sqrt(sum(v * v for v in qv.values()))
    if q_norm == 0:
        return base  # 查询词全为未见词, 无任何重叠

    # ---- 余弦相似度打分 ----
    scored = []
    for c in chunks:
        ctf = {}
        for t in _tokenize(c["text"]):
            ctf[t] = ctf.get(t, 0) + 1
        dot = sum(ctf.get(t, 0) * idf.get(t, 0.0) * qv.get(t, 0.0)
                  for t in set(ctf) & set(qv))
        c_norm = math.sqrt(
            sum((ctf[t] * idf.get(t, 0.0)) ** 2 for t in ctf))
        if c_norm == 0:
            continue
        scored.append((dot / (q_norm * c_norm), c))
    if not scored:
        return base
    scored.sort(key=lambda x: x[0], reverse=True)

    top = [(s, c) for s, c in scored[:TOP_K] if s >= MIN_SCORE]
    if not top:
        base["top_score"] = round(scored[0][0], 4)
        return base

    answer_parts = []
    citations = []
    for s, c in top:
        answer_parts.append("「%s」%s" % (c["section"], c["text"]))
        citations.append({
            "doc": c["doc"],
            "section": c["section"],
            "snippet": c["text"][:_SNIPPET_LEN],
        })
    return {
        "query": query,
        "matched": True,
        "answer": "\n\n".join(answer_parts),
        "citations": citations,
        "top_score": round(top[0][0], 4),
        "doc_count": len(chunks),
    }


def run_rag_query(query: str) -> tuple:
    """RAG 检索 + 结果写入 evidence_v2 (辅助证据, 不触碰任何判定结果)。

    - query 缺失/空白 → (False, 400, ...)
    - 知识库有文档: 命中/未命中都落库, evidence status='ok', value_json
      含 matched 标记; 未命中 answer 为固定文本 "无可靠依据"
    - 知识库为空/不可读 (doc_count=0, 组件故障): answer 仍为固定文本
      "无可靠依据", 但 evidence status='unavailable' 并携带 error_message
      (阶段06板块4 失效降级专项: 组件故障标记 unavailable)
    - 数据库错误 → (False, 500, ...)
    返回 (ok, http_status, msg, data); data = 检索结果 + evidence_id。
    """
    from backend.run_store import save_evidence  # 懒加载, 纯检索不依赖 DB

    if query is None or not str(query).strip():
        return False, 400, "query required", None
    query = str(query).strip()
    result = search_rag(query)
    # 组件故障判定: 知识库无任何可用文档 → 组件降级 (区别于查询无匹配)
    component_degraded = result["doc_count"] == 0
    result["component_status"] = "degraded" if component_degraded else "ok"
    evidence_id = None
    try:
        ok, evidence_id = save_evidence(
            evidence_type="rag",
            source_id="query:" + hashlib.sha1(
                query.encode("utf-8")).hexdigest()[:12],
            status="unavailable" if component_degraded else "ok",
            value_json=json.dumps(result, ensure_ascii=False),
            observed_at=_now(),
            error_message=("rag_docs 知识库为空或不可读, RAG 组件异常"
                           if component_degraded else ""),
        )
    except ValueError as exc:
        return False, 500, "rag evidence save failed: %s" % exc, None
    if not ok:
        return False, 500, "rag evidence save failed: %s" % evidence_id, None
    data = dict(result)
    data["evidence_id"] = evidence_id
    return True, 200, (
        "rag answer found" if result["matched"] else
        "rag no match (%s)" % NO_MATCH_ANSWER), data


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python rag_store.py <query>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = run_rag_query(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
