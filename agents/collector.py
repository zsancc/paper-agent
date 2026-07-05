"""
搜集Agent：对应架构图"领域Agent层"里的第一个角色。

真实模式：直接用标准库 urllib + xml.etree 调用 arXiv 官方 Atom API 检索，
不依赖第三方 arxiv 包——零额外依赖，符合"领域适配层"轻量可插拔的设计取向。
若网络异常或本次检索无结果，自动降级到本地示例候选集，并在日志里明确
标注降级原因，不做静默失败。
"""

import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from .base import BaseAgent

MOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "mock_candidates.json")
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_API = "http://export.arxiv.org/api/query"


class CollectorAgent(BaseAgent):
    name = "搜集Agent"

    def __init__(self, live_mode: bool = True):
        self.live_mode = live_mode

    async def search(self, query: str, max_results: int = 5) -> list:
        if self.live_mode:
            try:
                results = self._search_live(query, max_results)
                if results:
                    return results
                self.log("[LIVE模式] 检索结果为空，降级使用本地示例候选集")
            except Exception as e:
                self.log(f"[LIVE模式异常] {type(e).__name__}: {e}，降级使用本地示例候选集")
        return self._search_mock(query, max_results)

    _STOPWORDS = {"and", "or", "the", "a", "of", "for", "in", "on", "with"}

    def _build_arxiv_query(self, query: str) -> str:
        # 两个实测发现：
        # 1) 给带连字符的多词短语加引号做精确短语匹配几乎总是0命中
        #    （"all:\"lidar-camera fusion\""→0条），因为arXiv的分词器不支持这种
        #    连字符短语的精确匹配；所以这里把短语拆成单词，逐词AND。
        # 2) 用 all: 字段（标题+摘要+作者+评论一起搜）会把不相关领域的论文
        #    （比如同时提到"dynamic"和"camera"的核物理论文）拉进来；改用 abs:
        #    （只搜摘要）配合 sortBy=relevance，实测结果明显更贴题
        #    （abs:lidar AND abs:camera AND abs:fusion AND abs:dynamic AND abs:obstacle
        #     的relevance排序第一条就是LV-DOT，和用户实际研究方向高度吻合）。
        terms = [t.strip() for t in query.split(" AND ") if t.strip()]
        words = []
        for term in terms:
            for w in term.replace("-", " ").split():
                lw = w.lower()
                if lw not in self._STOPWORDS and w not in words:
                    words.append(w)
        return " AND ".join(f"abs:{w}" for w in words)

    def _fetch_arxiv_api(self, params: dict):
        url = ARXIV_API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "paper-agent-prototype/1.0"})
        # arXiv 官方礼貌请求限制是≤1次/3秒；这里只做1次重试且退避5秒，
        # 避免因为重试过密反被判定为滥用请求（曾实测触发过 HTTP 429）。
        raw = None
        last_exc = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
                break
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    self.log(f"[LIVE模式] 网络异常({type(e).__name__})，5秒后重试一次")
                    import time as _time
                    _time.sleep(5)
        if raw is None:
            raise last_exc
        return ET.fromstring(raw)

    @staticmethod
    def _clean_latex(text: str) -> str:
        # 有些作者会在摘要里直接写 \href{url}{text} 这类LaTeX命令，
        # arXiv的Atom API原样返回，不清理的话会在翻译/展示环节把LaTeX源码
        # 原封不动地喂给LLM和页面，既难看又可能干扰翻译质量。
        text = re.sub(r"\\href\{([^}]*)\}\{([^}]*)\}", r"\2 (\1)", text)
        text = re.sub(r"\\url\{([^}]*)\}", r"\1", text)
        text = text.replace(r"\%", "%")
        return text

    def _parse_entry(self, entry) -> dict:
        id_url = entry.findtext(f"{ATOM_NS}id", "") or ""
        arxiv_id = id_url.rsplit("/", 1)[-1]
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip().replace("\n", " ")
        abstract = self._clean_latex((entry.findtext(f"{ATOM_NS}summary") or "").strip().replace("\n", " "))
        authors = [
            (a.findtext(f"{ATOM_NS}name") or "").strip()
            for a in entry.findall(f"{ATOM_NS}author")
        ]
        published = (entry.findtext(f"{ATOM_NS}published") or "")[:10]
        pdf_url = ""
        for link in entry.findall(f"{ATOM_NS}link"):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
        return {
            "arxiv_id": arxiv_id, "title": title, "authors": authors,
            "abstract": abstract, "published": published, "pdf_url": pdf_url,
            "source": "arXiv",
        }

    def _search_live(self, query: str, max_results: int) -> list:
        arxiv_query = self._build_arxiv_query(query)
        self.log(f"[LIVE模式] 正在检索 arXiv (stdlib urllib，无第三方依赖): query={arxiv_query!r}")
        root = self._fetch_arxiv_api({
            "search_query": arxiv_query, "start": 0, "max_results": max_results,
            "sortBy": "relevance", "sortOrder": "descending",
        })
        results = [self._parse_entry(e) for e in root.findall(f"{ATOM_NS}entry")]
        self.log(f"[LIVE模式] 检索到 {len(results)} 篇论文（真实 arXiv 实时数据）")
        return results

    def _search_mock(self, query: str, max_results: int) -> list:
        self.log(f"[DEMO模式] 改为读取本地示例候选集: {MOCK_PATH}")
        with open(MOCK_PATH, "r", encoding="utf-8") as f:
            candidates = json.load(f)
        self.log(f"命中 {len(candidates)} 篇候选论文（本地预置示例）")
        return candidates[:max_results]

    _ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")

    async def fetch_by_id(self, raw_id: str) -> dict:
        """按用户指定的arXiv编号/链接拉取单篇论文，供“手动添加论文”功能使用。"""
        m = self._ID_RE.search(raw_id.strip())
        if not m:
            self.log(f"[手动添加] 无法从输入中识别出arXiv编号: {raw_id!r}")
            return None
        arxiv_id = m.group(1)
        self.log(f"[手动添加] 正在按编号拉取: {arxiv_id}")
        try:
            root = self._fetch_arxiv_api({"id_list": arxiv_id, "max_results": 1})
        except Exception as e:
            self.log(f"[手动添加异常] {type(e).__name__}: {e}")
            return None
        entries = root.findall(f"{ATOM_NS}entry")
        if not entries:
            self.log(f"[手动添加] arXiv上未找到编号: {arxiv_id}")
            return None
        paper = self._parse_entry(entries[0])
        self.log(f"[手动添加] 已获取: {paper['title'][:60]}")
        return paper
