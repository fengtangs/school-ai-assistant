"""FastAPI 服务：多智能体问答接口与聊天界面。

支持多个智能体（见 agentkit/agents.json），通过 /api/agents 列出，
聊天时可用 agent 参数指定使用哪一个（默认第一个）。
"""
import os
import json
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Query as QueryParam
from pydantic import BaseModel

from agentkit import list_agents, get_agent, get_default_agent_id, build_all
from core.embeddings import get_embedding_provider
from core.llm import RECOMMENDED_MODELS, list_custom_models, explain_llm_error
from core.teacher_api import router as teacher_router
from core.homework_api import router as homework_router
from core import jobs as job_store

app = FastAPI(title="校园AI问答助手（多智能体）", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
# 222
)
# 教师助手（资料上传 / 材料生成 / 文件下载）
app.include_router(teacher_router)
# 作业中心（AI 出题 / 布置 / 学情分析）
app.include_router(homework_router)

UI_PATH = os.path.join(os.path.dirname(__file__), "ui", "index.html")
TEACHER_UI_PATH = os.path.join(os.path.dirname(__file__), "ui", "teacher.html")
TEACHER_HW_PATH = os.path.join(os.path.dirname(__file__), "ui", "homework_teacher.html")
STUDENT_HW_PATH = os.path.join(os.path.dirname(__file__), "ui", "homework_student.html")


def _embedding_provider_label() -> str:
    """探测当前 embedding 实现名（仅供 /api/health 展示）。

    这里必须容错：若用户把 EMBEDDING_PROVIDER 配成 custom 却没填 CUSTOM_API_KEY，
    构造实例会抛异常。不能因此让整个服务起不来，降级为错误标记即可。
    """
    try:
        prov = get_embedding_provider(
            os.getenv("EMBEDDING_PROVIDER", "mock"),
            os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
        )
        return prov.__class__.__name__
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


_embedding_provider_name = _embedding_provider_label()


class Query(BaseModel):
    question: str
    # 注意：Pydantic v2 下必须显式 Optional，否则前端传 null 会被判为 422
    agent: Optional[str] = None   # 智能体 id，留空则用默认
    model: Optional[str] = None   # 可选：临时指定模型，留空则用 .env 里的默认模型


@app.get("/")
def index():
    if os.path.exists(UI_PATH):
        return FileResponse(UI_PATH)
    return {"message": "UI 未找到，请访问 /docs 使用接口"}


@app.get("/teacher")
def teacher_index():
    """教师工作台页面（资料上传、材料生成、下载）。"""
    if os.path.exists(TEACHER_UI_PATH):
        return FileResponse(TEACHER_UI_PATH)
    raise HTTPException(status_code=404, detail="教师工作台页面未找到")


@app.get("/homework/teacher")
def homework_teacher_index():
    """教师作业平台页面（AI 出题 / 布置 / 学情分析）。"""
    if os.path.exists(TEACHER_HW_PATH):
        return FileResponse(TEACHER_HW_PATH)
    raise HTTPException(status_code=404, detail="教师作业平台页面未找到")


@app.get("/homework/student")
def homework_student_index():
    """校园小助手·学生作业页面（按科目选作业、作答、查看成绩）。"""
    if os.path.exists(STUDENT_HW_PATH):
        return FileResponse(STUDENT_HW_PATH)
    raise HTTPException(status_code=404, detail="学生作业页面未找到")


@app.get("/api/models")
def models(refresh: bool = QueryParam(False)):
    """可选模型列表。

    默认返回经过实测验证、可直接对话的精选模型（RECOMMENDED_MODELS），
    并附带当前默认模型。refresh=true 时会实时向网关拉取全部模型 id，
    便于发现新上架的模型（其中混有图像/语音/OCR/审核模型，需自行甄别）。

    仅 custom 后端支持运行时切换模型；ollama / deepseek 后端返回空的可选项。
    """
    current = None
    try:
        provider = os.getenv("LLM_PROVIDER", "ollama").lower()
        if provider in ("custom", "tokensd"):
            current = os.getenv("CUSTOM_MODEL", "deepseek-v4-flash")
        elif provider == "deepseek":
            current = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        else:
            current = os.getenv("OLLAMA_MODEL", "deepseek-r1:14b")
    except Exception:  # noqa: BLE001
        pass

    switchable = os.getenv("LLM_PROVIDER", "ollama").lower() in ("custom", "tokensd")
    payload = {
        "switchable": switchable,
        "current": current,
        "models": RECOMMENDED_MODELS if switchable else [],
    }
    if refresh and switchable:
        try:
            payload["all_models"] = list_custom_models()
        except Exception as e:  # noqa: BLE001
            payload["all_models_error"] = str(e)
    return payload


@app.get("/api/agents")
def agents():
    return {"agents": list_agents(), "default": get_default_agent_id()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    """查询后台生成任务的进度（备课/教案/课件/AI 出题共用）。

    前端提交生成任务后拿到 job_id，按 0.5 秒轮询本接口，
    读取 pct（百分比）、stage（当前阶段）与最终 result。
    """
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新发起生成")
    return job.snapshot()


@app.get("/api/health")
def health():
    agents_info = []
    any_llm = False
    for meta in list_agents():
        try:
            a = get_agent(meta["id"])
            info = a.info()
            agents_info.append(info)
            any_llm = any_llm or info["llm_ready"]
        except Exception as e:  # noqa: BLE001
            agents_info.append({"id": meta["id"], "error": str(e)})
    return {
        "status": "ok",
        "llm_ready": any_llm,
        "agents": agents_info,
        "embedding_provider": _embedding_provider_name,
    }


@app.post("/api/chat")
def chat(q: Query, stream: bool = QueryParam(False)):
    if not q.question or not q.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    agent_id = q.agent or get_default_agent_id()
    try:
        agent = get_agent(agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未找到智能体: {agent_id}")

    # 非流式：直接返回完整 JSON（向后兼容）
    if not stream:
        try:
            return agent.ask(q.question.strip(), model=q.model)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=explain_llm_error(e))

    # 流式：以 SSE（text/event-stream）逐事件推送 ask_stream 的结果
    def event_stream():
        try:
            for ev in agent.ask_stream(q.question.strip(), model=q.model):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': explain_llm_error(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/rebuild")
def rebuild(q: Query = None):
    agent_id = (q.agent if q else None) or None
    if agent_id:
        try:
            n = get_agent(agent_id).build_index()
        except KeyError:
            raise HTTPException(status_code=404, detail=f"未找到智能体: {agent_id}")
        return {"agent": agent_id, "indexed_chunks": n}
    return {"agents": build_all()}


if __name__ == "__main__":
    import uvicorn

    # 默认 10086，避免与华技云项目(8080)冲突；可用环境变量 PORT 覆盖
    port = int(os.getenv("PORT", "10086"))
    uvicorn.run(app, host="0.0.0.0", port=port)
