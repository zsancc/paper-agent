"""
共享知识库，对应架构图里的"向量知识库"。

演示版用 SQLite + 关键词匹配代替真正的向量检索，
是为了让原型在没有额外依赖（embedding模型/向量库服务）的情况下也能跑通。
报告里要明确说明：生产版本应替换成 sentence-embedding + 向量数据库
（比如本地跑一个轻量embedding模型 + Chroma/FAISS），
这里的 SQLite 版本只是把"知识库作为多个Agent共享黑板"这个架构角色跑出来。
"""

import sqlite3
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "logs", "knowledge_base.sqlite3")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id TEXT PRIMARY KEY,
            title TEXT,
            abstract TEXT,
            zh_abstract TEXT,
            authors TEXT,
            pdf_url TEXT,
            published TEXT,
            relevance_score REAL,
            relevance_reason TEXT,
            structured_summary TEXT,
            status TEXT,
            via_arbitration INTEGER DEFAULT 0
        )
    """)
    # 轻量迁移：兼容早期没有 via_arbitration 列的旧数据库文件
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    if "via_arbitration" not in existing_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN via_arbitration INTEGER DEFAULT 0")
    return conn


def reset():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    _conn().close()


def upsert_paper(arxiv_id, title, abstract, relevance_score=None, relevance_reason=None,
                  structured_summary=None, status="collected", zh_abstract=None,
                  authors=None, pdf_url=None, published=None, via_arbitration=None):
    """局部更新：只有传入非None的字段才会覆盖已有值，避免不同Agent各自调用时
    互相用None把对方刚写入的字段冲掉（比如筛选Agent只知道score/reason，
    不知道翻译/抽取的结果，不该把它们清空）。via_arbitration 记录该论文是否
    经仲裁改判收录，一旦置1就不应被后续管线阶段清掉，因此也走同样的局部更新逻辑。"""
    conn = _conn()
    cur = conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id,))
    existing = cur.fetchone()
    cols = [d[0] for d in cur.description] if existing else None

    values = {
        "title": title, "abstract": abstract, "zh_abstract": zh_abstract,
        "authors": json.dumps(authors, ensure_ascii=False) if authors is not None else None,
        "pdf_url": pdf_url, "published": published,
        "relevance_score": relevance_score, "relevance_reason": relevance_reason,
        "structured_summary": json.dumps(structured_summary, ensure_ascii=False) if structured_summary else None,
        "status": status,
        "via_arbitration": (1 if via_arbitration else 0) if via_arbitration is not None else None,
    }
    if existing:
        existing_map = dict(zip(cols, existing))
        for k, v in values.items():
            if v is None:
                values[k] = existing_map.get(k)
    else:
        if values["via_arbitration"] is None:
            values["via_arbitration"] = 0

    conn.execute("""
        INSERT INTO papers (arxiv_id, title, abstract, zh_abstract, authors, pdf_url,
                             published, relevance_score, relevance_reason, structured_summary,
                             status, via_arbitration)
        VALUES (:arxiv_id, :title, :abstract, :zh_abstract, :authors, :pdf_url,
                :published, :relevance_score, :relevance_reason, :structured_summary,
                :status, :via_arbitration)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            title=excluded.title, abstract=excluded.abstract, zh_abstract=excluded.zh_abstract,
            authors=excluded.authors, pdf_url=excluded.pdf_url, published=excluded.published,
            relevance_score=excluded.relevance_score, relevance_reason=excluded.relevance_reason,
            structured_summary=excluded.structured_summary, status=excluded.status,
            via_arbitration=excluded.via_arbitration
    """, {**values, "arxiv_id": arxiv_id})
    conn.commit()
    conn.close()


def exists(arxiv_id) -> bool:
    conn = _conn()
    cur = conn.execute("SELECT 1 FROM papers WHERE arxiv_id=?", (arxiv_id,))
    found = cur.fetchone() is not None
    conn.close()
    return found


def all_papers():
    conn = _conn()
    cur = conn.execute("SELECT arxiv_id, title, status, relevance_score FROM papers")
    rows = cur.fetchall()
    conn.close()
    return rows


_FULL_COLS = ("arxiv_id", "title", "abstract", "zh_abstract", "authors", "pdf_url",
              "published", "relevance_score", "relevance_reason", "structured_summary",
              "status", "via_arbitration")


def _row_to_dict(row):
    d = dict(zip(_FULL_COLS, row))
    d["summary"] = json.loads(d["structured_summary"]) if d["structured_summary"] else None
    d["authors"] = json.loads(d["authors"]) if d["authors"] else []
    d["score"] = d["relevance_score"]
    d["via_arbitration"] = bool(d.get("via_arbitration"))
    return d


def all_papers_full():
    conn = _conn()
    cur = conn.execute(f"SELECT {','.join(_FULL_COLS)} FROM papers ORDER BY rowid DESC")
    rows = cur.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_paper(arxiv_id):
    conn = _conn()
    cur = conn.execute(f"SELECT {','.join(_FULL_COLS)} FROM papers WHERE arxiv_id=?", (arxiv_id,))
    row = cur.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def search_by_keyword(keyword: str):
    conn = _conn()
    cur = conn.execute(
        "SELECT arxiv_id, title FROM papers WHERE title LIKE ? OR abstract LIKE ?",
        (f"%{keyword}%", f"%{keyword}%")
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_paper(arxiv_id) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM papers WHERE arxiv_id=?", (arxiv_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


import re as _re


def _tokenize(text: str):
    if not text:
        return []
    return _re.findall(r"[a-z0-9]+", text.lower())


def retrieve(query: str, top_k: int = 4):
    """库内检索：对知识库中每篇论文按'与问题的词项重合度'打分，返回最相关的 top_k 篇。

    这是"检索问答Agent"做RAG问答时的召回环节。演示版用词项重合度打分
    （标题/摘要/中文摘要/结构化摘要一起纳入匹配），生产版本应替换为
    sentence-embedding 语义向量检索——这一点与知识库整体的演进方向一致。

    返回 [(paper_dict, score), ...]，按分数降序。
    """
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        return []
    scored = []
    for p in all_papers_full():
        summary_text = ""
        if p.get("summary"):
            summary_text = " ".join(str(v) for v in p["summary"].values() if v)
        haystack = " ".join([
            p.get("title") or "", p.get("abstract") or "",
            p.get("zh_abstract") or "", summary_text,
        ])
        doc_tokens = _tokenize(haystack)
        if not doc_tokens:
            continue
        doc_set = set(doc_tokens)
        # 命中的查询词数 + 轻微的词频加权，既奖励覆盖面也奖励主题集中度
        overlap = q_tokens & doc_set
        if not overlap:
            continue
        freq = sum(doc_tokens.count(t) for t in overlap)
        score = len(overlap) + 0.1 * freq
        scored.append((p, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
