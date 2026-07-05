from .base import BaseAgent
import llm_client
import knowledge_base as kb


class ExtractorAgent(BaseAgent):
    name = "知识抽取Agent"

    async def process(self, paper: dict, zh_abstract: str = None) -> dict:
        arxiv_id = paper["arxiv_id"]
        summary = llm_client.call_llm("extract", arxiv_id, {
            "title": paper["title"],
            "abstract": paper["abstract"],
        })
        self.log(f"提炼结构化摘要完成: {arxiv_id}")
        for field in ("motivation", "method", "result"):
            if field in summary:
                self.log(f"    {field}: {summary[field][:40]}...")

        kb.upsert_paper(arxiv_id, paper["title"], paper["abstract"],
                         relevance_score=paper.get("relevance_score"),
                         relevance_reason=paper.get("relevance_reason"),
                         structured_summary=summary, status="processed",
                         zh_abstract=zh_abstract,
                         authors=paper.get("authors"), pdf_url=paper.get("pdf_url"),
                         published=paper.get("published"))
        self.log(f"已写入知识库: {arxiv_id}")
        return summary
