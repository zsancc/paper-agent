# 论文管理多Agent平台 —— 原型说明

对应课程报告要求3（领域适配实现）与要求4（原型）。

## 怎么用

```bash
cd paper_agent
pip install -r requirements.txt   # 首次：flask + requests
python webapp.py                  # 或 py webapp.py
```
然后浏览器打开 `http://127.0.0.1:5000`。这是一个**多页面 Web 应用**，顶部导航切换四个页面：

- **设置**：填入 DeepSeek API Key、选模型（默认 `deepseek-v4-pro`）、编辑研究画像
  （兴趣关键词、高优先关键词/关注学者、相关性阈值）。**这些都会持久化保存到本机文件**
  （Key/模型存 `data/settings.local.json`，画像存 `data/user_profile.json`），
  重启服务自动加载生效，不用每次重填。不填 Key 则走 DEMO 演示模式。
- **发现**：按方向检索 arXiv，或按编号手动添加论文。检索是**后台任务 + 实时进度**——
  页面实时刷新编排Agent的协作日志，跑完自动展示本轮的仲裁记录与入库推送，浏览器不会假死干等。
  若某篇被筛选Agent拒收、但命中你的高优先信号，会自动进入仲裁分支。
- **问答**：就已收录的论文做 RAG 检索增强问答，回答标注引用了库里哪几篇（可点开看详情），
  保留本次会话的问答历史。
- **文献库**：藏书统计 + 库内关键词检索 + 点标题看详情（中文翻译 + 结构化摘要）+ 逐篇删除。

这个前端就是架构图里"交互层（L1）"的真实实现——所有操作走的都是 `orchestrator.py` 里
真实的编排调度逻辑，和命令行 `python main.py` 跑的是同一套后端代码。长耗时的检索/问答
通过 `webapp.py` 里的后台任务运行器执行，前端用 `/api/job/<id>` 轮询实时日志。

## 六个领域Agent与真正的"多Agent协作"

本平台**不是顺序流水线**，而是通过事件总线解耦的多Agent协作系统。两处协作是它区别于
传统单体工具的核心：

1. **冲突仲裁（筛选Agent ↔ 追踪预警Agent ↔ 编排Agent）**：筛选Agent按"核心关键词相关性"
   拒收一篇论文时，并不直接丢弃，而是经消息总线**发布 `paper.rejected` 事件**；追踪预警Agent
   订阅了该事件，若论文命中用户在 `user_profile.json` 里标注的高优先信号（`priority_keywords`/
   `priority_authors`），就**发起仲裁申诉**（发布 `arbitration.requested`）；编排Agent订阅申诉后，
   从"该优先方向的参考价值"这一**不同于筛选Agent的视角**请LLM二次裁决，做出"改判收录"或
   "维持拒收"的决定。两个Agent从不同立场得出不同判断、由第三方裁决——这是流水线做不到的。
2. **库内检索问答（检索问答Agent）**：对已沉淀的本地文献库做RAG问答，回答可溯源到具体论文。

发布方（筛选Agent）根本不知道谁在监听——全靠 `message_bus.py` 的 subscribe/publish 解耦，
这正是"对等式/事件驱动协作"的落地。

## 架构层 ↔ 代码模块对照表

| 架构图里的层/角色 | 代码模块 |
|---|---|
| 交互层（用户查询/管理面板） | `webapp.py`（Flask Web面板，见上方"怎么用"） |
| 编排Agent（含冲突仲裁裁决） | `orchestrator.py` |
| 搜集Agent | `agents/collector.py` |
| 筛选去重Agent | `agents/filter_agent.py` |
| 翻译Agent | `agents/translator.py` |
| 知识抽取Agent | `agents/extractor.py` |
| 追踪预警Agent（订阅拒收事件、发起仲裁申诉） | `agents/watcher.py` |
| 检索问答Agent（库内RAG问答） | `agents/librarian.py` |
| 任务消息队列（真正的发布/订阅事件总线） | `message_bus.py` |
| 向量知识库（演示版用SQLite+词项重合度检索代替语义向量） | `knowledge_base.py` |
| 领域适配层 | `agents/collector.py` 里 `_search_live`/`_search_mock` 切换 + `llm_client.py` 的 DeepSeek/Claude 可插拔 |
| LLM能力（可插拔的模型调用层） | `llm_client.py` |

## 两种独立的运行模式开关

搜集Agent（是否真实检索arXiv）和LLM调用（是否真实调用大模型）现在是**两个独立的开关**，
不再像早期版本那样绑在一起——这样即使暂时没有API key，也能验证真实检索链路；
即使网络对arXiv不通，也能单独验证真实LLM决策链路。

**搜集Agent**：默认 `SEARCH_LIVE=True`，直接用标准库 `urllib` + `xml.etree` 调用
arXiv官方Atom API（零第三方依赖），检索异常或结果为空时自动降级到本地示例候选集
`data/mock_candidates.json`（4篇真实存在的2025年LiDAR-相机融合动态障碍物感知论文）。
如需强制使用本地示例集：
```bash
set PAPER_AGENT_FORCE_MOCK_SEARCH=1   # Windows
python main.py
```

**LLM调用**：探测到 `DEEPSEEK_API_KEY` 或 `ANTHROPIC_API_KEY` 环境变量时自动切换为LIVE，
优先使用DeepSeek（OpenAI兼容Chat Completions接口，`requests`直接调用，无需官方SDK）：
```bash
set DEEPSEEK_API_KEY=你的key
set DEEPSEEK_MODEL=deepseek-chat   # 可选，默认 deepseek-chat
python main.py
```
没有任何key时自动回退到DEMO模式，用 `llm_client.py` 里预置的分析结果，
所有DEMO输出在日志里都标注 `[SIMULATED]`，不会和真实结果混淆。

## 已验证的行为（日志见 `logs/`）

1. 完整跑通一次"检索→筛选去重→翻译→知识抽取→预警推送"全链路，消息总线记录19条协作消息
2. **真实LIVE验证**：接入DeepSeek后，筛选/翻译/抽取Agent的判断与摘要均为真实模型输出
  （相关性打分 0.98/0.95/0.9/0.1，和DEMO模式预置的0.93/0.95/0.62/0.31 不同，证明确实是真实调用而非读取缓存）
3. 筛选去重Agent正确识别出不相关论文（室内定位方向）予以拒收，其余通过
4. 二次运行同一批候选论文时，规则去重正确跳过（含之前被拒收的），验证了知识库查重的幂等性
5. 知识库（SQLite）落盘持久化，结构化摘要正确写入
6. **真实网络故障容错验证**：实测中遇到过 DeepSeek 端的 `SSLError`/`ReadTimeout` 瞬时抖动，
  以及 arXiv 端的连接超时和 HTTP 429（触发了官方"≤1请求/3秒"限流），
  两条外部依赖都加了退避重试+优雅降级，没有让整条流水线因为一次网络抖动而崩溃

## 已知局限 / 后续要做的事（可以写进报告的"局限性"部分）

- 翻译Agent目前只处理摘要文本，未接入真实PDF解析（生产版本需要接 `marker`/`MinerU` 等工具处理全文）
- 知识库用关键词匹配代替向量检索，语义召回能力有限，生产版本应替换为 embedding + 向量数据库
- 编排Agent目前是同步等待每个Agent返回后再派发下一步，真正的异步并发（多篇论文并行处理）还没实现，
  这也是我们在权衡分析里提到的"同步vs异步"要继续深化的地方
- 去重逻辑是"只要评估过就不再评估"，如果用户研究方向后续调整，被拒收的旧论文不会自动重新评估，
  可以作为架构的一个已知局限来讨论
- arXiv官方API对高频请求有严格限流（实测中触发过429），生产版本应加入请求节流队列
  （比如固定≥3秒的请求间隔）而不是仅靠重试，这也是"领域适配层要考虑外部服务SLA"的一个真实案例
