"""
检索问答Agent（Librarian）。

这是把平台从"论文抓取+展示"升级为真正"论文管理"的关键角色：
用户可以就自己已经收录的文献库直接提问，Agent做检索增强问答（RAG）：
  1) 召回：调用知识库的 retrieve()，按与问题的相关度取回最相关的若干篇论文；
  2) 生成：把这些论文的标题+中文摘要+结构化要点拼成上下文，交给LLM，
     要求它严格基于给定文献作答，并在结论里标注引用了哪几篇（arXiv编号）。

这与传统文献工具（Zotero/知网）的本质区别在于：它回答的是"你的库里这些论文
就某个问题综合说了什么"，而不是把关键词丢给全网搜索引擎——知识沉淀在本地库，
回答可溯源到具体论文。演示版召回用词项重合度，生产版应替换为语义向量检索。
"""

from .base import BaseAgent
import knowledge_base as kb
import llm_client


class LibrarianAgent(BaseAgent):
    name = "检索问答Agent"

    async def answer(self, question: str, top_k: int = 4) -> dict:
        hits = kb.retrieve(question, top_k=top_k)
        if not hits:
            self.log("知识库中没有与该问题相关的论文")
            return {
                "answer": "你的文献库里暂时没有与这个问题相关的论文。可以先检索或手动添加一些论文后再来提问。",
                "citations": [],
            }

        self.log(f"召回 {len(hits)} 篇相关论文: "
                  + ", ".join(f"{p['arxiv_id']}(score={s:.1f})" for p, s in hits))

        # 拼接上下文：每篇给出编号、标题、中文摘要（无则用英文摘要）、结构化要点
        context_blocks = []
        citations = []
        for p, _score in hits:
            summary = p.get("summary") or {}
            zh = p.get("zh_abstract") or p.get("abstract") or ""
            block = (
                f"[{p['arxiv_id']}] {p['title']}\n"
                f"中文摘要: {zh[:400]}\n"
                f"方法: {summary.get('method', '—')}\n"
                f"核心结果: {summary.get('result', '—')}\n"
                f"局限性: {summary.get('limitation', '—')}"
            )
            context_blocks.append(block)
            citations.append({"arxiv_id": p["arxiv_id"], "title": p["title"]})

        context = "\n\n".join(context_blocks)
        result = llm_client.call_llm("qa", "library", {
            "question": question,
            "context": context,
        })
        answer_text = result.get("raw") or result.get("answer") or str(result)
        self.log("已基于本地文献库生成回答")
        return {"answer": answer_text, "citations": citations}
