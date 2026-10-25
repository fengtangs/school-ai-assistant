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
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.docgen import build_docx, build_pptx, parse_json_block
from core.llm import get_llm
from core.tasks import build_messages, get_task, list_tasks
from core.teacher import TeacherStore, safe_filename, safe_teacher_id
from core import jobs

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


# ————————————————————————— 课件模板 —————————————————————————
def _templates_root() -> Path:
    return Path(os.getenv("TEACHER_TEMPLATE_DIR", "data/teacher_templates")).resolve()


def _template_dir(teacher_id: str) -> Path:
    d = _templates_root() / safe_teacher_id(teacher_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _template_path(teacher_id: str):
    """返回该教师上传的模板路径；没有则返回 None。"""
    p = _templates_root() / safe_teacher_id(teacher_id) / "template.pptx"
    return p if p.is_file() else None


def _inspect_template(path: Path) -> dict:
    """读取模板的幻灯片尺寸与版式数量，供前端展示。"""
    from pptx import Presentation

    try:
        prs = Presentation(str(path))
        w_px = prs.slide_width / 9525
        h_px = prs.slide_height / 9525
        ratio = "16:9" if abs(w_px / h_px - 16 / 9) < 0.05 else (
            "4:3" if abs(w_px / h_px - 4 / 3) < 0.05 else f"{w_px:.0f}×{h_px:.0f}"
        )
        return {
            "slides": len(prs.slides),
            "layouts": len(prs.slide_layouts),
            "canvas": f"{w_px:.0f}×{h_px:.0f}px",
            "ratio": ratio,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"模板解析失败：{e}"}


@router.post("/api/teacher/template")
async def upload_template(teacher_id: str = Form(...), file: UploadFile = File(...)):
    """上传一份 .pptx 作为课件模板（继承其母版、主题、背景与版式）。"""
    store = _store_or_400(teacher_id)
    name = (file.filename or "").lower()
    if not name.endswith(".pptx"):
        raise HTTPException(status_code=400, detail="模板必须是 .pptx 文件（.ppt 旧格式请先另存为 .pptx）")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="模板文件为空")
    if len(data) > 40 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="模板文件过大（上限 40MB）")

    d = _template_dir(store.teacher_id)
    target = d / "template.pptx"
    target.write_bytes(data)

    info = _inspect_template(target)
    if info.get("error"):
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=info["error"])

    meta = {
        "filename": file.filename,
        "size_kb": round(len(data) / 1024, 1),
        "uploaded_at": int(time.time()),
        **info,
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "template": meta}


@router.get("/api/teacher/template")
def get_template(teacher_id: str):
    """查询该教师是否已上传课件模板。"""
    try:
        tid = safe_teacher_id(teacher_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    p = _template_path(tid)
    if not p:
        return {"ok": True, "has_template": False}
    meta_path = p.parent / "meta.json"
    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    meta.setdefault("size_kb", round(p.stat().st_size / 1024, 1))
    return {"ok": True, "has_template": True, "template": meta}


@router.delete("/api/teacher/template")
def delete_template(teacher_id: str):
    """删除已上传的模板，恢复使用内置版式。"""
    try:
        tid = safe_teacher_id(teacher_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    p = _template_path(tid)
    if not p:
        return {"ok": True, "deleted": False}
    p.unlink(missing_ok=True)
    (p.parent / "meta.json").unlink(missing_ok=True)
    return {"ok": True, "deleted": True}


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
    model: Optional[str] = None  # Pydantic v2 下须显式 Optional，否则前端传 null 报 422
    use_template: bool = True    # 课件：是否套用教师上传的模板
    with_images: bool = True     # 课件：是否为各页生成并插入配图


def _resolve_slide_images(spec: dict, limit: int = None, progress=None) -> tuple:
    """为课件各页生成配图，返回 ({页序号: 图片路径}, 统计信息)。

    · 只处理 content / data 页，且必须有 image_prompt；
    · limit 控制成本与耗时（每张约 3-10 秒），默认取环境变量 PPT_MAX_IMAGES（缺省 6）；
    · 任何一张失败都不影响其余页，也不影响 PPT 生成；
    · progress(done, total, ok) 供前端进度条显示「生成配图 2/5」。
    """
    from core import images as imggen

    if limit is None:
        try:
            limit = int(os.getenv("PPT_MAX_IMAGES", "6"))
        except ValueError:
            limit = 6

    if not imggen.is_available():
        return {}, {"enabled": False, "reason": "当前后端不支持配图（需 custom 网关密钥）"}

    slides = spec.get("slides") if isinstance(spec.get("slides"), list) else []
    # 先收集任务清单，才能算出配图总数、给出准确进度
    candidates = []
    for idx, s in enumerate(slides):
        if not isinstance(s, dict):
            continue
        if str(s.get("type") or "").lower() not in ("content", "data"):
            continue
        prompt = str(s.get("image_prompt") or "").strip()
        if prompt:
            candidates.append((idx, prompt))
    candidates = candidates[:limit]

    mapping, done, failed = {}, 0, 0
    total = len(candidates)
    for i, (idx, prompt) in enumerate(candidates):
        path = imggen.generate_image(prompt)
        if path:
            mapping[idx] = path
            done += 1
        else:
            failed += 1
        if progress is not None:
            try:
                progress(i + 1, total, bool(path))
            except Exception:  # noqa: BLE001  进度回调失败不能影响主流程
                pass

    return mapping, {
        "enabled": True,
        "total": done,
        "failed": failed,
        "model": imggen.image_model(),
    }


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
            lead = str(s.get("lead") or "").strip()
            if lead:
                lines.append(f"*{lead}*")
            for b in (s.get("bullets") or []):
                if isinstance(b, dict):
                    text = str(b.get("text") or "").strip()
                    detail = str(b.get("detail") or "").strip()
                    if text:
                        lines.append(f"- {text}")
                    if detail:
                        lines.append(f"  > {detail}")
                else:
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


def _do_generate(req: GenerateReq, job=None) -> dict:
    """实际执行生成。job 非空时按阶段上报进度（供前端进度条）。

    阶段划分刻意对应教师的心理预期：找资料 → 写内容 → 排版 → 出图 → 生成文件，
    这样进度条上的文字本身就是一句「现在在做什么」的解释。
    """
    def step(pct, stage, detail=None):
        if job is not None:
            job.update(pct, stage, detail)

    step(3, "校验任务与模型")
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

    step(8, "检索你的教学资料")
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

    step(
        14,
        "组装提示词",
        (f"引用 {len(sources)} 份资料" if sources else "未检索到资料，按通用框架生成"),
    )
    messages = build_messages(req.task, req.params, context)

    # 思考型模型的 reasoning 消耗波动极大（实测同一提示词从 143 到 4773 token 不等），
    # max_tokens 偏小时会出现「思考完了但正文为空」。这里用较大的额度并做一次重试。
    max_tokens = int(os.getenv("TEACHER_MAX_TOKENS", "16384"))
    raw = ""
    last_err = ""
    # 大模型调用是整条链路里最长的阻塞段，用 Ticker 让进度条在这段时间里持续前进
    with jobs.Ticker(job, 14, 64, "大模型正在撰写内容…"):
        for attempt in range(2):
            msgs = messages
            if attempt > 0:
                # 上一次返回空/被截断时，追加一条收敛指令：内容更紧凑，优先保证 JSON 完整
                msgs = messages + [{
                    "role": "user",
                    "content": "上一次输出因篇幅过长被截断了。请重新输出，"
                               "**压缩篇幅**：每个要点 detail 控制在 30-40 字，"
                               "note 不超过 50 字，务必输出完整且合法的 JSON。",
                }]
            try:
                raw = llm.chat(
                    msgs,
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

    step(66, "解析结构化内容")
    try:
        spec = parse_json_block(raw)
    except ValueError as first_err:
        # 常见原因是输出过长被截断（JSON 不完整）。用「收敛篇幅」的指令再试一次，
        # 而不是直接把半截内容丢给教师。
        retry_raw = ""
        try:
            with jobs.Ticker(job, 66, 72, "内容过长，正在压缩后重试…"):
                retry_raw = llm.chat(
                    messages + [{
                        "role": "user",
                        "content": "上一次输出 JSON 不完整（可能被截断）。请重新输出，"
                                   "**大幅压缩篇幅**：每页 4 条要点、detail 30-40 字，"
                                   "确保 JSON 结构完整闭合。",
                    }],
                    model=req.model,
                    json_mode=True,
                    temperature=0.3,
                    max_tokens=max_tokens,
                )
            spec = parse_json_block(retry_raw)
            raw = retry_raw
        except Exception:  # noqa: BLE001  重试也失败则回退到原文
            return {
                "ok": False,
                "error": str(first_err),
                "raw_text": (raw or "")[:8000],
                "sources": sources,
                "hint": "模型未按 JSON 格式输出，已返回原文，可直接复制使用或换个模型重试。",
            }

    ext = "pptx" if task["output"] == "pptx" else "docx"
    file_id = uuid.uuid4().hex[:16]
    title = str(spec.get("title") or task["name"]).strip()
    display_name = f"{safe_filename(title)}{'.' + ext}"
    out_dir = _teacher_outputs(store.teacher_id)
    out_path = out_dir / f"{file_id}.{ext}"

    tpl_path = _template_path(store.teacher_id) if (ext == "pptx" and req.use_template) else None
    img_map, img_info = {}, {}
    if ext == "pptx" and req.with_images:
        step(70, "生成课件配图", "正在逐页绘制示意图")
        img_map, img_info = _resolve_slide_images(
            spec,
            progress=lambda done, total, ok: step(
                70 + 22 * done / max(total, 1),
                "生成课件配图",
                f"已完成 {done}/{total} 张",
            ),
        )
        step(92, "排版生成课件")
    elif ext == "pptx":
        step(80, "排版生成课件")
    else:
        step(80, "排版生成文档")

    try:
        if ext == "pptx":
            build_pptx(spec, str(out_path),
                       template_path=str(tpl_path) if tpl_path else None,
                       images_map=img_map)
        else:
            build_docx(spec, str(out_path), f"{task['name']}材料")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"生成文件失败: {e}")

    step(98, "整理结果")
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
        "template_used": bool(tpl_path),
        "images": img_info,
    }
    (out_dir / f"{file_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta["download_url"] = f"/api/teacher/download/{store.teacher_id}/{file_id}"
    meta["ok"] = True
    meta["preview"] = _preview_from_spec(spec, task)
    return meta


@router.post("/api/teacher/generate")
def generate_material(req: GenerateReq):
    """同步生成（兼容旧调用方：一次性等结果）。"""
    return _do_generate(req, None)


@router.post("/api/teacher/generate/async")
def generate_material_async(req: GenerateReq):
    """提交生成任务，立即返回 job_id。

    前端随后轮询 /api/jobs/{job_id}，就能拿到百分比与当前阶段，
    用于显示进度条而不是让用户干等一个转圈。
    """
    # 参数错误同步返回，避免用户要等轮询才发现填错了
    _store_or_400(req.teacher_id)
    try:
        get_task(req.task)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = jobs.submit("teacher_generate", lambda j: _do_generate(req, j))
    return {"ok": True, "job_id": job.id}


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
