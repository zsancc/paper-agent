"""
交互层（L1）Web 应用：多页面架构。

设计要点：
  - 多页面：文献库 / 发现 / 问答 / 设置，各司其职，共享顶部导航（templates/base.html）。
  - 长耗时操作（检索、手动添加、问答都要真实调大模型，往往数十秒）走**后台任务**：
    POST 立即返回并跳到带 ?job=<id> 的页面，页面用轻量轮询 /api/job/<id> 实时刷新
    Agent 协作日志，完成后自动刷新展示结果。浏览器不再假死干等。
  - 配置持久化：DeepSeek Key/模型与研究画像保存到本机文件，重启自动加载（见 settings.py）。

启动：python webapp.py，然后浏览器打开 http://127.0.0.1:5000
"""

import asyncio
import contextlib
import io
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(__file__))

import knowledge_base as kb
import llm_client
import settings as cfg
from orchestrator import Orchestrator

from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, url_for)

app = Flask(__name__)
app.secret_key = "paper-agent-local-ui"

# 启动时加载已保存的 Key/模型，注入 llm_client
cfg.apply_settings_to_llm(llm_client)

# ---------------- 后台任务运行器 ----------------
JOBS = {}                                   # job_id -> 记录
LAST_JOB = {"discover": None, "ask": None}  # 每类的最近一次任务
ASK_HISTORY = []                            # 问答会话历史（进程内存）
_job_lock = threading.Lock()               # 串行化任务，避免并发重定向 stdout 冲突


def _run_job(jid, work):
    rec = JOBS[jid]
    try:
        with _job_lock:
            with contextlib.redirect_stdout(rec["buf"]):
                print(f"LLM 调用模式: {'LIVE（' + (llm_client.LLM_PROVIDER or '') + '）' if llm_client.LIVE_MODE else 'DEMO（未配置 Key）'}\n")
                rec["result"] = work()
        rec["status"] = "done"
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["status"] = "error"


def start_job(kind: str, title: str, work) -> str:
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"id": jid, "kind": kind, "title": title, "status": "running",
                 "buf": io.StringIO(), "result": None, "error": None}
    LAST_JOB[kind] = jid
    threading.Thread(target=_run_job, args=(jid, work), daemon=True).start()
    return jid


def _job_view(jid):
    rec = JOBS.get(jid)
    if not rec:
        return None, None
    view = {"id": rec["id"], "status": rec["status"], "title": rec["title"],
            "log": rec["buf"].getvalue(), "error": rec["error"]}
    return view, rec["result"]


# ---------------- 模板全局注入 ----------------
@app.context_processor
def inject_globals():
    return {"llm_live": llm_client.LIVE_MODE,
            "llm_provider": llm_client.LLM_PROVIDER,
            "kb_count": len(kb.all_papers())}


# ---------------- 文献库 ----------------
@app.route("/")
def library():
    kw = request.args.get("kw", "").strip()
    papers = [p for p, _s in kb.retrieve(kw, top_k=200)] if kw else kb.all_papers_full()
    all_p = kb.all_papers_full()
    stats = {
        "total": len(all_p),
        "direct": sum(1 for p in all_p if p["status"] == "processed" and not p["via_arbitration"]),
        "arbitrated": sum(1 for p in all_p if p["via_arbitration"]),
        "manual": sum(1 for p in all_p if p["status"] == "manually_added"),
    }
    return render_template("library.html", active="library", papers=papers, kw=kw, stats=stats)


# ---------------- 发现 ----------------
@app.route("/discover")
def discover():
    jid = request.args.get("job") or LAST_JOB["discover"]
    job, result = _job_view(jid) if jid else (None, None)
    running = bool(job and job["status"] == "running")
    profile = cfg.load_profile()
    default_query = " AND ".join(profile.get("interest_keywords", [])[:2])
    return render_template("discover.html", active="discover", job=job, result=result,
                            running=running, default_query=default_query)


@app.route("/discover/run", methods=["POST"])
def run_search():
    query = request.form.get("query", "").strip()
    profile = cfg.load_profile()
    if not query:
        query = " AND ".join(profile.get("interest_keywords", [])[:2])

    def work():
        orch = Orchestrator(cfg.load_profile(), search_live=True)
        notifications = asyncio.run(orch.run_daily_cycle(query))
        return {"kind": "search", "notifications": notifications,
                "arbitration": orch.arbitration_records,
                "summary": f"入库处理 {len(notifications)} 篇，仲裁 {len(orch.arbitration_records)} 条"}

    jid = start_job("discover", f"检索「{query}」", work)
    return redirect(url_for("discover", job=jid))


@app.route("/discover/add", methods=["POST"])
def run_add():
    raw_id = request.form.get("arxiv_id", "").strip()

    def work():
        orch = Orchestrator(cfg.load_profile(), search_live=True)
        notif = asyncio.run(orch.process_single_paper(raw_id))
        return {"kind": "add", "notifications": [notif] if notif else [],
                "arbitration": orch.arbitration_records,
                "summary": "手动添加完成" if notif else "未能添加该论文"}

    jid = start_job("discover", f"手动添加 {raw_id}", work)
    return redirect(url_for("discover", job=jid))


# ---------------- 问答 ----------------
@app.route("/ask")
def ask_page():
    jid = request.args.get("job") or LAST_JOB["ask"]
    job, _result = _job_view(jid) if jid else (None, None)
    running = bool(job and job["status"] == "running")
    return render_template("ask.html", active="ask", job=job, running=running, history=ASK_HISTORY)


@app.route("/ask/run", methods=["POST"])
def run_ask():
    question = request.form.get("question", "").strip()
    if not question:
        return redirect(url_for("ask_page"))

    def work():
        orch = Orchestrator(cfg.load_profile(), search_live=True)
        res = asyncio.run(orch.answer_question(question))
        ASK_HISTORY.insert(0, {"question": question, "answer": res["answer"],
                                "citations": res["citations"]})
        return {"kind": "ask"}

    jid = start_job("ask", f"问答：{question[:20]}", work)
    return redirect(url_for("ask_page", job=jid))


# ---------------- 设置 ----------------
@app.route("/settings")
def settings_page():
    s = cfg.load_settings()
    prof = cfg.load_profile()
    return render_template(
        "settings.html", active="settings",
        has_key=bool(s.get("deepseek_key")), model=s.get("deepseek_model"),
        provider=llm_client.LLM_PROVIDER,
        interest_keywords="\n".join(prof.get("interest_keywords", [])),
        priority_keywords="\n".join(prof.get("priority_keywords", [])),
        priority_authors="\n".join(prof.get("priority_authors", [])),
        relevance_threshold=prof.get("relevance_threshold", 0.6),
    )


@app.route("/settings/llm", methods=["POST"])
def save_llm():
    key = request.form.get("deepseek_key", "")
    model = request.form.get("deepseek_model", "")
    # 留空表示不改动已保存的 Key
    cfg.save_settings(deepseek_key=key if key.strip() else None, deepseek_model=model)
    cfg.apply_settings_to_llm(llm_client)
    flash("LLM 设置已保存并生效。")
    return redirect(url_for("settings_page"))


@app.route("/settings/profile", methods=["POST"])
def save_profile():
    cfg.save_profile(
        interest_keywords=request.form.get("interest_keywords", ""),
        priority_keywords=request.form.get("priority_keywords", ""),
        priority_authors=request.form.get("priority_authors", ""),
        relevance_threshold=request.form.get("relevance_threshold", ""),
    )
    flash("研究画像已保存。")
    return redirect(url_for("settings_page"))


# ---------------- 通用操作 ----------------
@app.route("/delete", methods=["POST"])
def delete():
    arxiv_id = request.form.get("arxiv_id", "").strip()
    if arxiv_id:
        kb.delete_paper(arxiv_id)
    return redirect(request.referrer or url_for("library"))


@app.route("/reset", methods=["POST"])
def reset():
    kb.reset()
    ASK_HISTORY.clear()
    LAST_JOB["discover"] = None
    flash("文献库已清空。")
    return redirect(url_for("library"))


@app.route("/paper/<arxiv_id>")
def paper_detail(arxiv_id):
    p = kb.get_paper(arxiv_id)
    if p is None:
        return render_template("paper.html", active="library", p=None), 404
    return render_template("paper.html", active="library", p=p)


# ---------------- 任务轮询 ----------------
@app.route("/api/job/<jid>")
def api_job(jid):
    rec = JOBS.get(jid)
    if not rec:
        return jsonify({"status": "unknown", "log": "", "error": None})
    return jsonify({"status": rec["status"], "log": rec["buf"].getvalue(), "error": rec["error"]})


if __name__ == "__main__":
    print("论文管理多Agent平台 · Web 应用")
    print("浏览器打开: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
