import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import knowledge_base as kb
from orchestrator import Orchestrator
from llm_client import LIVE_MODE, LLM_PROVIDER

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

SEARCH_LIVE = os.environ.get("PAPER_AGENT_FORCE_MOCK_SEARCH") != "1"


def load_profile():
    with open(os.path.join(DATA_DIR, "user_profile.json"), "r", encoding="utf-8") as f:
        return json.load(f)


async def main():
    search_mode = "LIVE（真实arXiv检索，stdlib直连）" if SEARCH_LIVE else "DEMO（本地示例候选集）"
    llm_mode = f"LIVE（{LLM_PROVIDER}）" if LIVE_MODE else "DEMO（无API key，见 llm_client.py 说明）"
    print(f"搜集Agent 模式: {search_mode}")
    print(f"LLM 调用模式: {llm_mode}")
    print()

    kb.reset()
    profile = load_profile()
    orchestrator = Orchestrator(profile, search_live=SEARCH_LIVE)

    query = " AND ".join(profile["interest_keywords"][:2])
    notifications = await orchestrator.run_daily_cycle(query)

    print("\n" + "#" * 60)
    print("# 本轮推送给用户的通知汇总")
    print("#" * 60)
    for n in notifications:
        print(f"\n【{n['title']}】")
        print(f"  相关性: {n['score']}  理由: {n['reason']}")
        print(f"  核心结果: {n['key_result']}")

    print("\n" + "#" * 60)
    print("# 知识库当前状态")
    print("#" * 60)
    for row in kb.all_papers():
        print(f"  {row[0]} | {row[2]:<10} | score={row[3]} | {row[1][:50]}")

    # 保存消息总线日志，作为报告里"运行日志"的原始材料
    log_path = os.path.join(os.path.dirname(__file__), "logs", "message_history.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(orchestrator.bus.history, f, ensure_ascii=False, indent=2)
    print(f"\n完整消息总线日志已保存到: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())
