"""
配置持久化模块。

分两类持久化：
  1) 敏感/运行配置 → data/settings.local.json：DeepSeek API Key、模型名。
     应用启动时自动加载并注入 llm_client，这样 Key 一次保存、长期生效，
     不必每次开面板重填。（单机个人工具，Key 以明文存于本机该文件，请勿分享此文件。）
  2) 研究画像 → data/user_profile.json：兴趣关键词、高优先关键词/作者、相关性阈值。
     这些直接驱动筛选Agent的相关性判断与追踪预警Agent的仲裁触发，放在设置页可编辑。
"""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.local.json")
PROFILE_PATH = os.path.join(DATA_DIR, "user_profile.json")

DEFAULT_SETTINGS = {"deepseek_key": "", "deepseek_model": "deepseek-v4-pro"}


def load_settings() -> dict:
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {**DEFAULT_SETTINGS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(deepseek_key: str = None, deepseek_model: str = None) -> dict:
    cur = load_settings()
    if deepseek_key is not None:
        cur["deepseek_key"] = deepseek_key.strip()
    if deepseek_model:
        cur["deepseek_model"] = deepseek_model.strip()
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return cur


def apply_settings_to_llm(llm_client) -> dict:
    """把已保存的 Key/模型注入 llm_client，返回当前设置。"""
    s = load_settings()
    if s.get("deepseek_key"):
        llm_client.configure_deepseek(s["deepseek_key"], s.get("deepseek_model"))
    return s


def load_profile() -> dict:
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _split_lines(text: str):
    """把多行/逗号分隔的输入拆成去空的列表。"""
    if not text:
        return []
    parts = []
    for chunk in text.replace("\r", "\n").replace("，", ",").split("\n"):
        for item in chunk.split(","):
            item = item.strip()
            if item:
                parts.append(item)
    return parts


def save_profile(interest_keywords=None, priority_keywords=None,
                  priority_authors=None, relevance_threshold=None) -> dict:
    prof = load_profile()
    if interest_keywords is not None:
        prof["interest_keywords"] = _split_lines(interest_keywords)
    if priority_keywords is not None:
        prof["priority_keywords"] = _split_lines(priority_keywords)
    if priority_authors is not None:
        prof["priority_authors"] = _split_lines(priority_authors)
    if relevance_threshold is not None:
        try:
            prof["relevance_threshold"] = max(0.0, min(1.0, float(relevance_threshold)))
        except (TypeError, ValueError):
            pass
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False, indent=2)
    return prof
