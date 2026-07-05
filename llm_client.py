"""
统一LLM调用层。

设计意图：Agent不直接依赖某个具体的LLM SDK/供应商，而是依赖这个薄封装——
以后想换模型/换供应商，只需要改这一个文件，符合我们架构设计里
"领域适配"的可插拔思路（同样的模式也用在了搜集Agent对接不同论文源上）。

三种运行模式（按优先级探测）：
  - LIVE(DeepSeek)：检测到 DEEPSEEK_API_KEY 时启用，走 DeepSeek 的
    OpenAI兼容Chat Completions接口（用 requests 直接调用，不引入官方SDK依赖）。
  - LIVE(Anthropic)：检测到 ANTHROPIC_API_KEY 时启用，走 Claude API。
  - DEMO：没有任何 key 时启用，返回预置的分析结果，保证无网络/无密钥环境下
    也能完整跑通整条流水线。所有 DEMO 输出都在日志里标注 [SIMULATED]。
"""

import os
import json
import time

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_FALLBACK_MODEL = "deepseek-chat"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

LIVE_MODE = bool(DEEPSEEK_API_KEY or ANTHROPIC_API_KEY)
LLM_PROVIDER = "deepseek" if DEEPSEEK_API_KEY else ("anthropic" if ANTHROPIC_API_KEY else None)


def configure_deepseek(key: str, model: str = None):
    """运行时配置DeepSeek key（供Web面板使用，仅存于进程内存，不落盘）。

    环境变量在模块导入时就已经读入了 DEEPSEEK_API_KEY 等常量，之后再 os.environ
    也不会让已导入的模块自动感知——所以Web面板要支持“现场粘贴key切换LIVE模式”，
    必须提供这样一个运行时setter，而不是指望改环境变量生效。
    """
    global DEEPSEEK_API_KEY, DEEPSEEK_MODEL, LIVE_MODE, LLM_PROVIDER
    DEEPSEEK_API_KEY = key or None
    if model:
        DEEPSEEK_MODEL = model
    LIVE_MODE = bool(DEEPSEEK_API_KEY or ANTHROPIC_API_KEY)
    LLM_PROVIDER = "deepseek" if DEEPSEEK_API_KEY else ("anthropic" if ANTHROPIC_API_KEY else None)

# ---- DEMO 模式下的预置响应：按 arxiv_id + task 类型索引 ----
# 这些内容是基于每篇论文的真实摘要人工写好的结构化分析，
# 用来在没有真实API调用的环境里，依然产出和真实LLM输出格式一致的结果。
_CANNED_RESPONSES = {
    ("2504.13647", "relevance"): {
        "relevant": True,
        "score": 0.93,
        "reason": "同时覆盖LiDAR-相机融合的多类别动态目标检测与轨迹预测，并在真实机器人平台上做到了实时部署，和Paper 1的感知+局部避障闭环高度相关。"
    },
    ("2502.20607", "relevance"): {
        "relevant": True,
        "score": 0.95,
        "reason": "直接聚焦LiDAR-视觉融合的动态障碍物检测与跟踪，服务于机器人自主导航，和你的研究题目几乎是同一细分方向。"
    },
    ("2502.01856", "relevance"): {
        "relevant": True,
        "score": 0.62,
        "reason": "同属LiDAR-相机融合检测，但关注点是传感器故障下的鲁棒性，和动态避障/costmap的直接关联较弱，可作为鲁棒性设计的参考文献。"
    },
    ("2505.08388", "relevance"): {
        "relevant": False,
        "score": 0.31,
        "reason": "重点是室内定位精度而非动态障碍物感知，应用场景偏静态地图构建，和当前研究方向相关性不足，建议不收录。"
    },
    ("2504.13647", "extract"): {
        "motivation": "现有单一或松耦合的LiDAR-相机检测方法难以兼顾多类别动态目标检测精度与轨迹预测能力，且在低算力平台上部署困难。",
        "method": "设计了一个多类别3D动态目标检测网络，将LiDAR点云与相机图像特征融合后统一输出检测结果与轨迹预测。",
        "experiment": "在CODa和nuScenes数据集上评测，并部署到搭载入门级RTX 3060的轮椅机器人上做实时推理验证。",
        "result": "检测mAP较已有方法提升3.71%，行人轨迹预测minADE5降低0.408米，在nuScenes上mAP达72.7%，机器人端实时推理13.9FPS。",
        "limitation": "论文未详细讨论极端遮挡或多目标密集交互场景下的失败案例，迁移到不同机器人平台的泛化成本待验证。"
    },
    ("2502.20607", "extract"): {
        "motivation": "单一传感器（纯相机或纯LiDAR）在动态障碍物识别上存在感知范围或语义信息的天然短板。",
        "method": "提出轻量级LiDAR-视觉融合框架，结合LiDAR的大范围高精度测距与相机的丰富视觉特征，做动态障碍物检测与跟踪。",
        "experiment": "在自主移动机器人平台上进行导航场景下的动态障碍物检测与跟踪实验。",
        "result": "相比单传感器方案，动态目标识别的准确性和鲁棒性有明显提升，同时保持轻量化、适合机载部署。",
        "limitation": "论文未充分说明在遮挡严重或目标快速穿越视野边缘时的跟踪丢失恢复策略。"
    }
}


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def _build_prompt(task: str, payload: dict) -> str:
    prompts = {
        "relevance": (
            "你是论文筛选助手。根据用户研究方向和论文摘要，判断是否相关，"
            "只返回JSON，不要任何多余文字: {\"relevant\": bool, \"score\": 0到1之间的小数, \"reason\": \"一句话中文理由\"}\n\n"
            f"用户研究方向关键词: {payload.get('profile')}\n论文标题: {payload.get('title')}\n摘要: {payload.get('abstract')}"
        ),
        "extract": (
            "你是论文结构化摘要助手。阅读摘要，抽取要点，只返回JSON，不要任何多余文字，字段为"
            "motivation, method, experiment, result, limitation（均为一句话中文）。\n\n"
            f"标题: {payload.get('title')}\n摘要: {payload.get('abstract')}"
        ),
        "translate": (
            f"将以下英文学术摘要翻译成准确、通顺的中文学术摘要，只输出译文本身：\n\n{payload.get('text')}"
        ),
        "qa": (
            "你是科研文献助手。请**只依据下面提供的文献资料**回答用户的问题，"
            "不要编造资料之外的内容；若资料不足以回答，就直言资料不足。"
            "回答用中文，条理清晰，并在相关论断后用方括号标注引用的论文编号（如[2502.20607]）。\n\n"
            f"用户问题：{payload.get('question')}\n\n"
            f"可用文献资料：\n{payload.get('context')}"
        ),
        "arbitrate": (
            "你是科研文献平台的仲裁员。一篇论文被筛选Agent以'核心相关性不足'为由初判拒收，"
            "但追踪预警Agent指出它命中了用户明确标注的高优先信号，提出异议。"
            "请你从一个不同于'核心关键词匹配'的视角重新裁决：用户之所以标注这些高优先信号，"
            "是因为它们是其研究工作赖以依托的基础设施方向或必须持续跟踪的对象。"
            "因此判断标准是——这篇论文作为该优先方向的**参考文献**是否真的有保留价值，"
            "而不是它是否精确命中核心研究关键词。若论文确实是该优先方向上有实质内容的工作，"
            "则应改判收录；若只是词面上偶然提及、实质内容与用户毫无关联，则维持拒收。\n"
            "只返回JSON，不要多余文字: {\"admit\": bool, \"reason\": \"一句话中文裁决理由\"}\n\n"
            f"用户高优先信号：{payload.get('signal')}\n"
            f"筛选Agent的拒收理由：{payload.get('reject_reason')}\n"
            f"论文标题：{payload.get('title')}\n论文摘要：{payload.get('abstract')}"
        ),
    }
    return prompts[task]


def call_llm(task: str, arxiv_id: str, payload: dict) -> dict:
    """
    统一入口。task in {"relevance", "extract", "translate"}
    """
    if LIVE_MODE:
        return _call_live(task, arxiv_id, payload)
    return _call_demo(task, arxiv_id, payload)


def _call_demo(task: str, arxiv_id: str, payload: dict) -> dict:
    key = (arxiv_id, task)
    if key in _CANNED_RESPONSES:
        _log(f"  [SIMULATED] LLM({task}) 对 {arxiv_id} 返回预置分析（无API key，演示模式）")
        return _CANNED_RESPONSES[key]
    _log(f"  [SIMULATED-FALLBACK] LLM({task}) 对 {arxiv_id} 无预置数据，使用保守兜底规则")
    if task == "relevance":
        return {"relevant": False, "score": 0.4, "reason": "演示模式无预置分析，保守判定为待人工复核。"}
    if task == "qa":
        return {"raw": "（演示模式：未配置API Key，无法生成基于文献库的问答。"
                        "请在面板顶部的『LLM设置』里填入 DeepSeek Key 后重试。）"}
    if task == "arbitrate":
        # 演示模式下的保守裁决：命中优先信号即改判收录（真实模式由LLM按参考价值判断）
        return {"admit": True, "reason": "演示模式：命中用户高优先信号，保守改判收录待人工复核。"}
    return {"note": "演示模式无预置内容"}


def _call_live(task: str, arxiv_id: str, payload: dict) -> dict:
    if DEEPSEEK_API_KEY:
        return _call_deepseek(task, arxiv_id, payload)
    return _call_anthropic(task, arxiv_id, payload)


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _call_deepseek(task: str, arxiv_id: str, payload: dict) -> dict:
    """真实模式：调用 DeepSeek 的 OpenAI兼容 Chat Completions 接口。

    网络实测中出现过间歇性代理/连接重置（ProxyError/ConnectionResetError），
    这类瞬时故障不代表模型或代码有问题，所以加了几次短退避重试，
    而不是让整条流水线因为一次网络抖动就崩溃——这也是原型阶段
    "外部依赖容错" 的一个真实设计取舍点，可以写进报告的局限性/权衡部分。
    """
    import requests

    prompt = _build_prompt(task, payload)
    use_json_mode = task in ("relevance", "extract", "arbitrate")
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}

    def _post(model: str):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}
        return requests.post(DEEPSEEK_API_URL, json=body, headers=headers, timeout=60)

    def _post_with_retry(model: str, attempts: int = 3):
        last_exc = None
        for i in range(attempts):
            try:
                return _post(model)
            except requests.exceptions.RequestException as e:
                last_exc = e
                if i < attempts - 1:
                    wait = 2 * (i + 1)
                    _log(f"  [LIVE-DeepSeek] 网络异常({type(e).__name__})，{wait}秒后重试 "
                         f"({i + 1}/{attempts})")
                    time.sleep(wait)
        raise last_exc

    model = DEEPSEEK_MODEL
    resp = _post_with_retry(model)
    if resp.status_code != 200 and model != DEEPSEEK_FALLBACK_MODEL:
        _log(f"  [LIVE-DeepSeek] 模型 {model!r} 调用失败(HTTP {resp.status_code}: "
             f"{resp.text[:200]!r})，回退到 {DEEPSEEK_FALLBACK_MODEL!r} 重试")
        model = DEEPSEEK_FALLBACK_MODEL
        resp = _post_with_retry(model)

    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    _log(f"  [LIVE-DeepSeek:{model}] LLM({task}) 对 {arxiv_id} 返回真实模型输出")

    if task in ("translate", "qa"):
        return {"raw": text}
    return _parse_llm_json(text)


def _call_anthropic(task: str, arxiv_id: str, payload: dict) -> dict:
    """真实模式：调用Claude API。需要 pip install anthropic 且设置 ANTHROPIC_API_KEY。"""
    import anthropic
    client = anthropic.Anthropic()
    prompt = _build_prompt(task, payload)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    _log(f"  [LIVE-Anthropic] LLM({task}) 对 {arxiv_id} 返回真实模型输出")
    if task in ("translate", "qa"):
        return {"raw": text}
    return _parse_llm_json(text)
