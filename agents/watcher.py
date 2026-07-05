"""
追踪预警Agent。

两个职责：
  1) 订阅筛选去重Agent发布的 paper.rejected 事件。当一篇被初判拒收的论文
     命中用户的"高优先信号"（关注的作者 / 高优先关键词）时，它认为这次拒收
     可能误伤了用户真正关心的方向，于是**发起仲裁申诉**（发布 arbitration.requested
     事件，交由编排Agent裁决）。这是本Agent与筛选Agent之间真实的"判断冲突"。
  2) 对最终入库的论文生成推送通知。

注意：本Agent发起申诉时，并不直接改写筛选Agent的结论，也不自己决定收录——
它只是"提出异议"，最终由编排Agent根据仲裁策略裁决。职责分离，符合多Agent
协商中"提议者 / 裁决者"分立的设计。
"""

from .base import BaseAgent


class WatcherAgent(BaseAgent):
    name = "追踪预警Agent"

    def __init__(self, user_profile: dict, bus=None):
        self.profile = user_profile
        self.bus = bus
        self.priority_keywords = [k.lower() for k in user_profile.get("priority_keywords", [])]
        self.priority_authors = [a.lower() for a in user_profile.get("priority_authors", [])]

    def _match_priority_signal(self, paper: dict):
        """返回命中的优先信号描述；未命中返回 None。"""
        text = f"{paper.get('title','')} {paper.get('abstract','')}".lower()
        for kw in self.priority_keywords:
            if kw in text:
                return f"命中高优先关键词「{kw}」"
        authors_lower = [a.lower() for a in paper.get("authors", [])]
        for author in self.priority_authors:
            if any(author in a for a in authors_lower):
                return f"作者包含关注学者「{author}」"
        return None

    async def on_rejection(self, message: dict):
        """订阅回调：筛选Agent每拒收一篇论文，这里都会被总线调用一次。"""
        paper = message["payload"]["paper"]
        signal = self._match_priority_signal(paper)
        if signal is None:
            return  # 没命中任何优先信号，尊重筛选Agent的拒收，不干预
        self.log(f"⚠ 对拒收提出异议: {paper['arxiv_id']} —— {signal}")
        self.log(f"    向编排Agent发起仲裁申诉，请求复核该论文是否应被静默丢弃")
        if self.bus is not None:
            await self.bus.publish(
                self.name, "编排Agent", "arbitration.requested",
                {"paper": paper, "signal": signal,
                 "score": message["payload"]["score"],
                 "threshold": message["payload"]["threshold"]},
            )

    async def notify(self, paper: dict, summary: dict) -> dict:
        via = "（经仲裁改判收录）" if paper.get("admitted_by_arbitration") else ""
        self.log(f"检测到入库论文匹配关注方向{via}: {paper['arxiv_id']} "
                  f"(score={paper.get('relevance_score')})")
        notification = {
            "arxiv_id": paper["arxiv_id"],
            "title": paper["title"],
            "score": paper.get("relevance_score"),
            "reason": paper.get("relevance_reason"),
            "key_result": (summary or {}).get("result", ""),
            "via_arbitration": bool(paper.get("admitted_by_arbitration")),
            "push_channel": "面板提醒（可扩展微信/邮件）",
        }
        self.log(f"[推送通知] 《{paper['title'][:40]}...》 已推送给用户")
        return notification
