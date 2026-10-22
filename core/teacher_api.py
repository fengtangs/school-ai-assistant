"""教师助手 API：资料上传、知识库管理、材料生成与下载。

路由挂载在 app.py 上（app.include_router），与校园问答接口并列。

核心流程（/api/teacher/generate）：
    教师资料 → 检索相关片段 → 组装提示词 → LLM 产出 JSON
    → docgen 渲染成 .docx / .pptx → 落盘 → 返回下载链接与预览
"""
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.docgen import build_docx, build_pptx, parse_json_block
from core.llm import get_llm
from core.tasks import build_messages, get_task, list_tasks
from core.teacher import TeacherStore, safe_filename, safe_teacher_id

router = APIRouter()


def _outputs_root() -> Path:
    return Path(os.getenv("TEACHER_OUTPUT_DIR", "data/teacher_outputs")).resolve()


def _teacher_outputs(teacher_id: str) -> Path:
    d = _outputs_root() / safe_teacher_id(teacher_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_or_400(teacher_id: str) -> TeacherStore:
    try:
        return TeacherStore(teacher_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ————————————————————————— 任务类型 —————————————————————————
@router.get("/api/teacher/tasks")
def teacher_tasks():
    return {"tasks": list_tasks()}


# ————————————————————————— 资料管理 —————————————————————————
@router.get("/api/teacher/materials")
def list_materials(teacher_id: str):
    store = _store_or_400(teacher_id)
    return {"teacher_id": store.teacher_id, "files": store.list_files(), "stats": store.stats()}


@router.post("/api/teacher/upload")
async def upload_materials(
    teacher_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """上传一份或多份资料，全部上传完成后重建该教师索引。"""
    store = _store_or_400(teacher_id)
    saved, errors = [], []
    for f in files:
        try:
            data = await f.read()
            if not data:
                errors.append({"filename": f.filename, "error": "文件为空"})
                continue
            info = store.add_file(f.filename, data)
            saved.append(info)
        except ValueError as e:
            errors.append({"filename": f.filename, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            errors.append({"filename": f.filename, "error": f"上传失败: {e}"})

    # 统一重建一次索引（比每个文件都重建更高效）
    indexed = store.rebuild() if saved else store.store.count()
    return {
        "ok": bool(saved),
        "saved": saved,
        "errors": errors,
        "indexed_chunks": indexed,
        "stats": store.stats(),
    }


@router.delete("/api/teacher/materials/{filename}")
def delete_material(filename: str, teacher_id: str):
    store = _store_or_400(teacher_id)
    try:
        removed = store.delete_file(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    indexed = store.rebuild()
    return {"ok": removed, "filename": filename, "indexed_chunks": indexed}


@router.post("/api/teacher/rebuild")
def rebuild_teacher_index(teacher_id: str = Form(...)):
    store = _store_or_400(teacher_id)
    return {"ok": True, "indexed_chunks": store.rebuild(), "stats": store.stats()}


# ————————————————————————— 生成 —————————————————————————
class GenerateReq(BaseModel):
    teacher_id: str
    task: str
    params: dict = {}
    model: str = None  # 可选：临时指定模型


def _preview_from_spec(spec: dict, task: dict) -> str:
    """把结构化 JSON 转成人类可读的预览文本（前端直接展示）。"""
    lines = []
    title = spec.get("title")
    if title:
        lines.append(f"# {title}")
    sub = spec.get("subtitle")
    if sub:
        lines.append(f"*{sub}*")
    meta = spec.get("meta")
    if isinstance(meta, dict) and meta:
        lines.append("")
        lines.append("　".join(f"**{k}**：{v}" for k, v in meta.items() if str(v).strip()))
    slides = spec.get("slides")
    if isinstance(slides, list) and slides:
        for i, s in enumerate(slides, 1):
            if not isinstance(s, dict):
                continue
            lines.append("")
            lines.append(f"## {i}. {s.get('title', '')}")
            for b in (s.get("bullets") or []):
                lines.append(f"- {b}")
            if s.get("note"):
                lines.append(f"  > 备注：{s['note']}")
    sections = spec.get("sections")
    if isinstance(sections, list) and sections:
        for i, s in enumerate(sections, 1):
            if not isinstance(s, dict):
                continue
            lines.append("")
            lines.append(f"## {i}. {s.get('heading', '')}")
            body = str(s.get("body") or "").strip()
            if body:
                lines.append(body)
            for it in (s.get("items") or []):
                lines.append(f"- {it}")
    return "\n".join(lines).strip() or "（模型未返回可展示的内容）"


@router.post("/api/teacher/generate")
def generate_material(req: GenerateReq):
    store = _store_or_400(req.teacher_id)
    try:
        task = get_task(req.task)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    llm = get_llm()
    if llm is None:
        raise HTTPException(
            status_code=503,
            detail="大模型未就绪，请检查 .env 中的 LLM_PROVIDER 与对应密钥",
        )

    # 用「课程 + 课题 + 补充要求」拼检索 query，比只搜标题更准
    query_parts = [
        str(req.params.get("course") or ""),
        str(req.params.get("topic") or ""),
        str(req.params.get("requirements") or ""),
        str(req.params.get("situation") or ""),
        str(req.params.get("class_name") or ""),
    ]
    query = " ".join(p for p in query_parts if p.strip()) or task["name"]
    context, sources = store.build_context(query, top_k=8)

    messages = build_messages(req.task, req.params, context)

    # 思考型模型的 reasoning 消耗波动极大（实测同一提示词从 143 到 4773 token 不等），
    # max_tokens 偏小时会出现「思考完了但正文为空」。这里用较大的额度并做一次重试。
    max_tokens = int(os.getenv("TEACHER_MAX_TOKENS", "8192"))
    raw = ""
    last_err = ""
    for attempt in range(2):
        try:
            raw = llm.chat(
                messages,
                model=req.model,
                json_mode=True,
                temperature=0.35,
                max_tokens=max_tokens,
            )
        except Exception as e:  # noqa: BLE001
            last_err = f"调用大模型失败: {e}"
            continue
        if raw and raw.strip():
            break
        last_err = (
            f"模型返回空内容（第 {attempt + 1} 次）。通常是因为思考过程耗尽了 "
            f"max_tokens={max_tokens}，可调大 TEACHER_MAX_TOKENS 或换个模型重试"
        )

    if not raw or not raw.strip():
        raise HTTPException(status_code=502, detail=last_err or "大模型未返回内容")

    try:
        spec = parse_json_block(raw)
    except ValueError as e:
        # 解析失败时把模型原文返回，便于教师直接复制使用，而不是白等一场
        return {
            "ok": False,
            "error": str(e),
            "raw_text": raw[:8000],
            "sources": sources,
            "hint": "模型未按 JSON 格式输出，已返回原文，可直接复制使用或换个模型重试。",
        }

    ext = "pptx" if task["output"] == "pptx" else "docx"
    file_id = uuid.uuid4().hex[:16]
    title = str(spec.get("title") or task["name"]).strip()
    display_name = f"{safe_filename(title)}{'.' + ext}"
    out_dir = _teacher_outputs(store.teacher_id)
    out_path = out_dir / f"{file_id}.{ext}"

    try:
        if ext == "pptx":
            build_pptx(spec, str(out_path))
        else:
            build_docx(spec, str(out_path), f"{task['name']}材料")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"生成文件失败: {e}")

    meta = {
        "file_id": file_id,
        "teacher_id": store.teacher_id,
        "task": req.task,
        "task_name": task["name"],
        "title": title,
        "display_name": display_name,
        "ext": ext,
        "size_kb": round(out_path.stat().st_size / 1024, 1),
        "created_at": int(time.time()),
        "sources": sources,
        "model": req.model or getattr(llm, "model", None),
    }
    (out_dir / f"{file_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta["download_url"] = f"/api/teacher/download/{store.teacher_id}/{file_id}"
    meta["ok"] = True
    meta["preview"] = _preview_from_spec(spec, task)
    return meta


# ————————————————————————— 下载与历史 —————————————————————————
@router.get("/api/teacher/download/{teacher_id}/{file_id}")
def download(teacher_id: str, file_id: str):
    """下载生成的文件。限定在该教师自己的输出目录内，防越权。"""
    if not file_id.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="非法的文件标识")
    out_dir = (_outputs_root() / safe_teacher_id(teacher_id)).resolve()
    meta_path = out_dir / f"{file_id}.json"
    if not meta_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已过期")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    ext = meta.get("ext", "docx")
    target = (out_dir / f"{file_id}.{ext}").resolve()
    # 双保险：确认路径确实落在该教师目录内
    if out_dir not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(
        str(target),
        filename=meta.get("display_name") or f"{file_id}.{ext}",
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            if ext == "pptx"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )


@router.get("/api/teacher/outputs")
def list_outputs(teacher_id: str, limit: int = 20):
    """列出该教师最近生成的材料。"""
    out_dir = _outputs_root() / safe_teacher_id(teacher_id)
    if not out_dir.is_dir():
        return {"outputs": []}
    items = []
    for p in out_dir.glob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        m["download_url"] = f"/api/teacher/download/{m.get('teacher_id')}/{m.get('file_id')}"
        items.append(m)
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"outputs": items[: max(1, min(limit, 100))]}


@router.delete("/api/teacher/outputs/{file_id}")
def delete_output(file_id: str, teacher_id: str):
    out_dir = _outputs_root() / safe_teacher_id(teacher_id)
    removed = []
    for p in out_dir.glob(f"{file_id}.*"):
        if p.is_file():
            p.unlink()
            removed.append(p.name)
    return {"ok": bool(removed), "removed": removed}
