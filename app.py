"""FastAPI 服务：多智能体问答接口与聊天界面。

支持多个智能体（见 agentkit/agents.json），通过 /api/agents 列出，
聊天时可用 agent 参数指定使用哪一个（默认第一个）。
"""
import os
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Query as QueryParam
from pydantic import BaseModel

from agentkit import list_agents, get_agent, get_default_agent_id, build_all
from core.embeddings import get_embedding_provider

app = FastAPI(title="校园AI问答助手（多智能体）", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UI_PATH = os.path.join(os.path.dirname(__file__), "ui", "index.html")
_embedding_provider_name = get_embedding_provider(
    os.getenv("EMBEDDING_PROVIDER", "mock"),
    os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
).__class__.__name__


class Query(BaseModel):
    question: str
    agent: str = None  # 智能体 id，留空则用默认


@app.get("/")
def index():
    if os.path.exists(UI_PATH):
        return FileResponse(UI_PATH)
    return {"message": "UI 未找到，请访问 /docs 使用接口"}


@app.get("/api/agents")
def agents():
    return {"agents": list_agents(), "default": get_default_agent_id()}


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
        return agent.ask(q.question.strip())

    # 流式：以 SSE（text/event-stream）逐事件推送 ask_stream 的结果
    def event_stream():
        try:
            for ev in agent.ask_stream(q.question.strip()):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

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
