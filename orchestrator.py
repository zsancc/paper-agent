"""
编排Agent（Coordinator）。

对应我们协作机制设计里说的混合式协作：
  - 宏观任务分解走主从式：由Orchestrator统一接收触发信号、
    按任务DAG（检索→筛选→[仲裁]→翻译→抽取→预警）派发子任务。
  - 微观协作走对等式/事件驱动：Agent之间通过 MessageBus 发布/订阅事件解耦。
    典型例子就是"筛选Agent拒收 → 追踪预警Agent申诉 → 编排Agent仲裁"这条
    真实的冲突协商链路，它不是顺序调用，而是事件触发的动态分支。

编排Agent既是任务派发者，也是冲突的**裁决者**：当追踪预警Agent对某次拒收
发起仲裁申诉时，编排Agent按既定仲裁策略做出"改判收录"或"维持拒收"的决定，
并把整个协商过程记录到消息总线与日志中。
"""

import knowledge_base as kb
import llm_client
from message_bus import MessageBus
from agents.collector import CollectorAgent
from agents.filter_agent import FilterAgent
from agents.translator import TranslatorAgent
from agents.extractor import ExtractorAgent
from agents.watcher import WatcherAgent
from agents.librarian import LibrarianAgent


class Orchestrator:
    name = "编排Agent"

    def __init__(self, user_profile: dict, search_live: bool = True):
        self.profile = user_profile
        self.bus = MessageBus()
        self.collector = CollectorAgent(live_mode=search_live)
        self.filter_agent = FilterAgent(user_profile, bus=self.bus)
        self.translator = TranslatorAgent()
        self.extractor = ExtractorAgent()
        self.watcher = WatcherAgent(user_profile, bus=self.bus)
        self.librarian = LibrarianAgent()
        self.notifications = []
        self.pending_arbitration = []   # 追踪预警Agent发起的仲裁申诉，等待本Agent裁决
        self.arbitration_records = []   # 裁决结果，供报告/界面展示

        # 事件订阅接线：这三行就是"对等式/事件驱动协作"的骨架。
        # 发布方（筛选/追踪Agent）无需知道谁在监听，全靠总线解耦。
        self.bus.subscribe("paper.rejected", self.watcher.on_rejection)
        self.bus.subscribe("arbitration.requested", self.on_arbitration_request)

    def log(self, msg):
        import time
        print(f"[{time.strftime('%H:%M:%S')}] [{self.name}] {msg}")

    async def on_arbitration_request(self, message: dict):
        """订阅回调：追踪预警Agent每发起一次仲裁申诉，这里被总线调用一次。
        这里只登记，真正的裁决集中在 _run_arbitration 阶段做，保持流程分段清晰。"""
        self.pending_arbitration.append(message["payload"])
        self.log(f"收到仲裁申诉: {message['payload']['paper']['arxiv_id']} "
                  f"（{message['payload']['signal']}），登记待裁决")

    def _arbitrate(self, req: dict) -> dict:
        """对单条申诉做出裁决。

        裁决方式是LLM二次仲裁：编排Agent不沿用筛选Agent"核心关键词匹配"的旧视角，
        而是把追踪预警Agent提供的优先信号作为新证据，请LLM从"该优先方向的参考价值"
        这个不同视角重新裁决。这样两个Agent从不同立场给出的判断，由编排Agent召集
        一次全新评估来协调——这是真正的多Agent协商，而非固定阈值的机械规则。
        """
        paper = req["paper"]
        verdict = llm_client.call_llm("arbitrate", paper["arxiv_id"], {
            "signal": req["signal"],
            "reject_reason": paper.get("relevance_reason", ""),
            "title": paper["title"], "abstract": paper["abstract"],
        })
        admit = bool(verdict.get("admit"))
        decision = "overturn" if admit else "uphold"
        verdict_reason = verdict.get("reason", "")
        if admit:
            rationale = f"{req['signal']}；仲裁认定有参考价值，改判收录。裁决理由：{verdict_reason}"
        else:
            rationale = f"{req['signal']}；但仲裁认定实质无关，维持拒收。裁决理由：{verdict_reason}"
        return {"arxiv_id": paper["arxiv_id"], "title": paper["title"],
                "decision": decision, "rationale": rationale, "paper": paper}

    async def _run_arbitration(self):
        """仲裁阶段：集中裁决所有待处理申诉，返回被改判收录的论文列表。"""
        if not self.pending_arbitration:
            return []
        self.log("-" * 60)
        self.log(f"进入仲裁阶段：共 {len(self.pending_arbitration)} 条来自追踪预警Agent的申诉")
        rescued = []
        for req in self.pending_arbitration:
            rec = self._arbitrate(req)
            self.arbitration_records.append(rec)
            mark = "✔ 改判收录" if rec["decision"] == "overturn" else "✘ 维持拒收"
            self.log(f"  裁决 {rec['arxiv_id']}: {mark} —— {rec['rationale']}")
            if rec["decision"] == "overturn":
                paper = rec["paper"]
                paper["admitted_by_arbitration"] = True
                kb.upsert_paper(paper["arxiv_id"], paper["title"], paper["abstract"],
                                 relevance_score=paper.get("relevance_score"),
                                 relevance_reason=paper.get("relevance_reason"),
                                 authors=paper.get("authors"), pdf_url=paper.get("pdf_url"),
                                 published=paper.get("published"),
                                 status="admitted_by_arbitration", via_arbitration=True)
                rescued.append(paper)
        self.pending_arbitration = []
        self.log("-" * 60)
        return rescued

    async def _process_pipeline(self, paper: dict):
        """对一篇已确认收录的论文，跑翻译→抽取→预警。"""
        arxiv_id = paper["arxiv_id"]
        self.log(f"--- 处理论文 {arxiv_id} ---")

        await self.bus.publish(self.name, "翻译Agent", "translate", {"arxiv_id": arxiv_id})
        translation = await self.translator.process(paper)
        await self.bus.publish("翻译Agent", self.name, "translate_result",
                                {"arxiv_id": arxiv_id}, status="done")

        await self.bus.publish(self.name, "知识抽取Agent", "extract", {"arxiv_id": arxiv_id})
        summary = await self.extractor.process(paper, zh_abstract=translation.get("zh_abstract"))
        await self.bus.publish("知识抽取Agent", self.name, "extract_result",
                                {"arxiv_id": arxiv_id}, status="done")

        await self.bus.publish(self.name, "追踪预警Agent", "notify", {"arxiv_id": arxiv_id})
        notification = await self.watcher.notify(paper, summary)
        self.notifications.append(notification)

    async def run_daily_cycle(self, query: str):
        self.log("=" * 60)
        self.log(f"任务触发：开始处理研究方向 「{query}」")
        self.log("=" * 60)

        # M1/M2: 检索
        await self.bus.publish(self.name, "搜集Agent", "search", {"query": query})
        candidates = await self.collector.search(query)
        await self.bus.publish("搜集Agent", self.name, "search_result",
                                {"count": len(candidates)}, status="done")

        # M3/M4: 筛选（拒收时会经总线触发追踪预警Agent的申诉→本Agent登记）
        await self.bus.publish(self.name, "筛选去重Agent", "filter", {"count": len(candidates)})
        accepted = await self.filter_agent.process(candidates)
        await self.bus.publish("筛选去重Agent", self.name, "filter_result",
                                {"accepted": len(accepted)}, status="done")

        # 仲裁阶段：处理筛选阶段中积累的冲突申诉
        rescued = await self._run_arbitration()
        if rescued:
            self.log(f"仲裁改判收录 {len(rescued)} 篇，将与初判通过的 {len(accepted)} 篇一并处理")
        accepted = accepted + rescued

        if not accepted:
            self.log("本轮没有需要处理的论文，流程结束")
            return []

        # 单篇论文的翻译/抽取若因外部服务持续故障而失败，只跳过该篇并记录，
        # 不让一次网络异常拖垮整批后续论文的处理（真实的批处理容错设计）。
        failed = 0
        for paper in accepted:
            try:
                await self._process_pipeline(paper)
            except Exception as e:
                failed += 1
                self.log(f"⚠ 论文 {paper['arxiv_id']} 处理失败({type(e).__name__}: {e})，"
                          f"跳过该篇，继续处理其余论文")

        self.log("=" * 60)
        done = len(accepted) - failed
        self.log(f"本轮流程结束：入库处理 {done}/{len(accepted)} 篇"
                  f"（含仲裁改判 {len(rescued)} 篇" + (f"，{failed} 篇因故障跳过" if failed else "") + "），"
                  f"消息总线共记录 {len(self.bus.history)} 条协作消息")
        self.log("=" * 60)
        return self.notifications

    async def process_single_paper(self, raw_id: str):
        """手动添加流程：用户指定一个具体的arXiv编号，直接拉取并跑完整条链路。

        以 collector 实际返回的 paper["arxiv_id"]（可能带版本号）为唯一标识，
        不做相关性阈值过滤（用户明确指定即视为收录意愿）。
        """
        self.log("=" * 60)
        self.log(f"手动添加：拉取指定论文 {raw_id}")
        self.log("=" * 60)

        await self.bus.publish(self.name, "搜集Agent", "fetch_by_id", {"arxiv_id": raw_id})
        paper = await self.collector.fetch_by_id(raw_id)
        await self.bus.publish("搜集Agent", self.name, "fetch_result",
                                {"found": paper is not None}, status="done")
        if paper is None:
            self.log(f"未能获取到 {raw_id}，流程终止")
            return None

        arxiv_id = paper["arxiv_id"]
        result = llm_client.call_llm("relevance", arxiv_id, {
            "profile": self.profile.get("interest_keywords"),
            "title": paper["title"], "abstract": paper["abstract"],
        })
        paper["relevance_score"] = result["score"]
        paper["relevance_reason"] = result["reason"]
        kb.upsert_paper(arxiv_id, paper["title"], paper["abstract"],
                         relevance_score=result["score"], relevance_reason=result["reason"],
                         authors=paper.get("authors"), pdf_url=paper.get("pdf_url"),
                         published=paper.get("published"), status="manually_added")

        await self._process_pipeline(paper)
        # _process_pipeline 里 extractor 会把 status 覆盖成 processed，这里保持它，
        # 但手动添加的语义仍通过 relevance_reason 与日志体现
        self.log(f"手动添加完成：{arxiv_id}")
        return self.notifications[-1] if self.notifications else {"arxiv_id": arxiv_id}

    async def answer_question(self, question: str):
        """检索问答：委托检索问答Agent对知识库做RAG问答。"""
        self.log("=" * 60)
        self.log(f"用户提问（面向本地文献库）：{question}")
        await self.bus.publish(self.name, "检索问答Agent", "qa", {"question": question})
        result = await self.librarian.answer(question)
        await self.bus.publish("检索问答Agent", self.name, "qa_result",
                                {"cited": [c["arxiv_id"] for c in result.get("citations", [])]},
                                status="done")
        self.log("=" * 60)
        return result
