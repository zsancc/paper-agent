# 论文管理多Agent平台

一个面向科研文献管理的多智能体协作平台。系统由六个各司其职的智能体组成，覆盖从检索、
筛选、翻译、结构化到追踪、问答的完整链条，并通过事件总线支持智能体之间的分歧仲裁。

- 搜集Agent：从 arXiv 检索新论文，或按编号单篇拉取。
- 筛选去重Agent：剔除重复，并用大模型判断论文与研究方向的相关性。
- 翻译Agent：把英文摘要翻译为中文。
- 知识抽取Agent：把每篇论文提炼为研究动机、方法、实验、结论、局限五个结构化字段。
- 追踪预警Agent：推送符合关注方向的新论文；并在筛选阶段发生分歧时发起仲裁申诉。
- 检索问答Agent：对已收录论文做检索增强问答（RAG），回答标注引用来源。

## 快速开始

```bash
cd paper_agent
pip install -r requirements.txt
python webapp.py
```

浏览器打开 `http://127.0.0.1:5000`。首次使用到「设置」页填入 DeepSeek API Key 即可启用
真实大模型；不填则以演示模式运行。Key 与研究画像会保存到本机文件，重启后自动加载。

## 页面

- **设置**：填写 DeepSeek Key、选择模型、编辑研究画像（兴趣关键词、高优先关键词、
  关注学者、相关性阈值）。这些配置持久化保存，重启自动生效。
- **发现**：按方向检索 arXiv，或按编号手动添加论文。检索在后台执行，页面实时回显智能体
  协作日志，完成后展示本轮的仲裁记录与入库推送。
- **问答**：就已收录论文做 RAG 问答，回答标注引用了库里哪几篇。
- **文献库**：藏书统计、库内关键词检索、查看单篇详情（中文翻译与结构化摘要）、逐篇删除。

## 多Agent协作机制

平台通过 `message_bus.py` 的发布/订阅事件总线让智能体保持松耦合。一个典型的协作链路是
冲突仲裁：

1. 筛选去重Agent 判定某论文相关性不足时，向总线发布 `paper.rejected` 事件。
2. 追踪预警Agent 订阅该事件；若论文命中用户在 `user_profile.json` 中标注的高优先关键词或
   关注学者，则发布 `arbitration.requested` 申诉。
3. 编排Agent 订阅申诉，从“该优先方向的参考价值”这一角度请大模型二次裁决，做出改判收录或
   维持拒收的决定。

发布方不需知道谁在监听，订阅方独立决定是否响应，两个智能体的不同判断由编排Agent 居中裁决。

## 架构与代码模块

| 架构层 / 角色 | 代码模块 |
|---|---|
| 交互层（Web 应用） | `webapp.py` |
| 编排Agent（含仲裁裁决） | `orchestrator.py` |
| 搜集Agent | `agents/collector.py` |
| 筛选去重Agent | `agents/filter_agent.py` |
| 翻译Agent | `agents/translator.py` |
| 知识抽取Agent | `agents/extractor.py` |
| 追踪预警Agent | `agents/watcher.py` |
| 检索问答Agent | `agents/librarian.py` |
| 事件总线 | `message_bus.py` |
| 知识库 | `knowledge_base.py` |
| LLM 调用层（DeepSeek / Claude 可插拔） | `llm_client.py` |
| 配置持久化 | `settings.py` |

## 运行模式

搜集Agent 的检索与 LLM 调用是两个独立开关。

- **检索**：默认直连 arXiv 官方 Atom API（标准库实现，零第三方依赖）；检索异常或结果为空时
  自动降级到本地示例集 `data/mock_candidates.json`。
- **LLM**：配置了 DeepSeek Key（或环境变量 `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY`）时
  调用真实大模型，优先使用 DeepSeek；未配置时以演示模式运行，返回预置分析结果，日志中标注
  `[SIMULATED]`。

命令行方式运行一次完整流程：

```bash
set DEEPSEEK_API_KEY=你的key   # Windows；未设置则为演示模式
python main.py
```

## 已知局限

- 论文来源目前仅接入 arXiv，尚未覆盖 Google Scholar、知网及各出版社官网。
- 翻译与抽取目前只处理摘要，未解析 PDF 全文。
- 所用 DeepSeek 为纯文本模型，无法理解论文中的图表。
- 知识库检索与 RAG 召回基于词项重合度，尚未使用语义向量检索。
- 编排Agent 同步逐篇处理，尚未实现多篇异步并发。

## 许可

MIT License，见 [LICENSE](LICENSE)。
