"""
筛选去重Agent：对应我们讨论过的"规则引擎 vs LLM驱动决策"权衡点的落地——
去重用确定性规则（查知识库主键），相关性判断用LLM语义理解，
两阶段组合，兼顾成本和准确率。

关键：本Agent做出"拒收"决定时，不会静默丢弃论文，而是通过消息总线
**发布一条 paper.rejected 事件**。它并不知道、也不关心谁会响应这条事件——
这正是事件驱动解耦的意义。实际上追踪预警Agent订阅了该事件，会在论文命中
用户高优先信号时对这次拒收发起仲裁申诉。这条协商链路是本平台区别于
顺序流水线的核心。
"""

from .base import BaseAgent
import llm_client
import knowledge_base as kb


class FilterAgent(BaseAgent):
    name = "筛选去重Agent"

    def __init__(self, user_profile: dict, bus=None):
        self.profile = user_profile
        self.bus = bus

    async def process(self, candidates: list) -> list:
        accepted = []
        for paper in candidates:
            arxiv_id = paper["arxiv_id"]

            # 第一阶段：规则引擎做去重（确定性判断，不需要LLM）
            if kb.exists(arxiv_id):
                self.log(f"[规则去重] {arxiv_id} 已在知识库中，跳过")
                continue

            # 第二阶段：LLM做语义相关性判断
            result = llm_client.call_llm("relevance", arxiv_id, {
                "profile": self.profile.get("interest_keywords"),
                "title": paper["title"],
                "abstract": paper["abstract"],
            })
            self.log(f"[相关性判断] {arxiv_id} 《{paper['title'][:30]}...》 "
                      f"score={result['score']} relevant={result['relevant']}")
            self.log(f"    理由: {result['reason']}")

            paper["relevance_score"] = result["score"]
            paper["relevance_reason"] = result["reason"]
            threshold = self.profile.get("relevance_threshold", 0.6)
            common_kwargs = dict(
                relevance_score=result["score"], relevance_reason=result["reason"],
                authors=paper.get("authors"), pdf_url=paper.get("pdf_url"),
                published=paper.get("published"),
            )
            if result["relevant"] and result["score"] >= threshold:
                accepted.append(paper)
                kb.upsert_paper(arxiv_id, paper["title"], paper["abstract"],
                                 status="accepted", **common_kwargs)
            else:
                self.log(f"    → 未达阈值({threshold})，初判拒收")
                kb.upsert_paper(arxiv_id, paper["title"], paper["abstract"],
                                 status="rejected", **common_kwargs)
                # 发布拒收事件——谁响应由订阅者自己决定（追踪预警Agent会据此判断是否申诉）
                if self.bus is not None:
                    await self.bus.publish(
                        self.name, "追踪预警Agent", "paper.rejected",
                        {"paper": paper, "score": result["score"], "threshold": threshold},
                    )
        self.log(f"本轮筛选完成：{len(accepted)}/{len(candidates)} 篇初判通过")
        return accepted
