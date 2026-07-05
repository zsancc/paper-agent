"""
翻译Agent。

生产版本的完整流程：PDF下载 → 用 marker/MinerU 等工具转成Markdown（保留公式/图表引用）
→ 逐段调用LLM翻译 → 输出双语对照MD，存到本地文件系统。

原型简化为：只对摘要文本做"转MD结构 + 翻译"，先把Agent间协作的控制流跑通，
PDF解析这类重IO操作留到后续接真实PDF时再补上。

翻译本身在 LIVE 模式下会真实调用 llm_client（DeepSeek/Claude）做翻译；
DEMO 模式下用占位规则代替，避免在无API key的环境里报错。
"""

from .base import BaseAgent
import llm_client


class TranslatorAgent(BaseAgent):
    name = "翻译Agent"

    async def process(self, paper: dict) -> dict:
        self.log(f"处理 {paper['arxiv_id']}：PDF→MD 结构化（原型阶段对摘要生效）")
        md_en = f"# {paper['title']}\n\n## Abstract\n{paper['abstract']}\n"

        if llm_client.LIVE_MODE:
            result = llm_client.call_llm("translate", paper["arxiv_id"], {"text": paper["abstract"]})
            zh_abstract = result.get("raw") or result.get("translation") or str(result)
        else:
            zh_abstract = paper.get("zh_abstract_demo") or self._demo_translate(paper["abstract"])
        md_zh = f"# {paper['title']}\n\n## 摘要（中文）\n{zh_abstract}\n"

        self.log(f"生成双语MD完成：{paper['arxiv_id']}.md / {paper['arxiv_id']}.zh.md")
        return {"md_en": md_en, "md_zh": md_zh, "zh_abstract": zh_abstract}

    def _demo_translate(self, text: str) -> str:
        return f"[演示模式占位翻译] {text[:60]}...（LIVE模式下此处输出真实中文翻译）"
