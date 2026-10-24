"""作业中心 API：AI 出题、保存/布置、学生提交批改、学情分析。

挂载在 app.py 上（app.include_router(homework_router)），与教师助手、校园问答并列。

接口一览：
  学生端（校园小助手）
    GET  /api/homework/subjects              已布置作业涉及的科目
    GET  /api/homework/assignments           已布置作业列表（可按 ?subject= 过滤）
    POST /api/homework/submit                提交作答并批改 → 返回得分/错题
    GET  /api/homework/preview/{hw_id}       学生作答前查看作业元信息

  教师端（教师工作平台）
    POST /api/homework/design                AI 生成作业题目（返回草稿，不落库）
    POST /api/homework/save                  保存/更新作业（含题目、科目、班级、截止日）
    POST /api/homework/assign/{hw_id}        布置（状态置为 assigned，推送到校园小助手）
    POST /api/homework/unassign/{hw_id}      撤回布置
    GET  /api/homework/list?teacher_id=       教师名下作业列表
    GET  /api/homework/{hw_id}               作业详情（含题目）
    DELETE /api/homework/{hw_id}             删除作业
    GET  /api/homework/analytics/{hw_id}     学情分析
    GET  /api/homework/submissions/{hw_id}   提交记录列表
    POST /api/homework/rescore               教师手动批改简答题（{hw_id,sub_id,qid,score}）
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import homework as hw
from core import jobs
from core.llm import explain_llm_error

router = APIRouter()


# ————————————————————————— 请求模型 —————————————————————————
class DesignReq(BaseModel):
    teacher_id: str = ""
    subject: str = ""
    title: str = ""
    topic: str = ""
    students: str = ""
    difficulty: str = "中等"
    n_choice: int = 5
    n_judge: int = 5
    n_short: int = 2
    requirements: str = ""
    model: Optional[str] = None  # Pydantic v2 下须显式 Optional，否则前端传 null 报 422


class SaveReq(BaseModel):
    id: str = ""           # 空则新建
    teacher_id: str
    subject: str
    title: str
    description: str = ""
    assign_class: str = ""
    deadline: str = ""
    status: str = "draft"
    questions: list = []


class SubmitReq(BaseModel):
    homework_id: str
    student_name: str
    student_class: str = ""
    answers: dict = {}     # { "0": 选项下标 / true|false / "文本" }


class RescoreReq(BaseModel):
    homework_id: str
    sub_id: str
    qid: str
    score: int


# ————————————————————————— 学生端 —————————————————————————
@router.get("/api/homework/subjects")
def subjects():
    return {"subjects": hw.list_subjects()}


@router.get("/api/homework/assignments")
def assignments(subject: str = ""):
    items = hw.list_assigned()
    if subject:
        items = [i for i in items if i["subject"] == subject]
    return {"assignments": items}


@router.get("/api/homework/preview/{hw_id}")
def preview(hw_id: str):
    h = hw.get_homework(hw_id)
    if not h or h.get("status") != "assigned":
        raise HTTPException(status_code=404, detail="作业未找到或未布置")
    return {
        "id": h["id"],
        "subject": h["subject"],
        "title": h["title"],
        "description": h.get("description", ""),
        "assign_class": h.get("assign_class", ""),
        "deadline": h.get("deadline", ""),
        "total_score": h.get("total_score", 100),
        "question_count": len(h.get("questions", [])),
        "questions": [
            {
                "index": i,
                "type": q["type"],
                "stem": q["stem"],
                "options": q.get("options", []) if q["type"] == "choice" else [],
            }
            for i, q in enumerate(h["questions"])
        ],
    }


@router.post("/api/homework/submit")
def submit(req: SubmitReq):
    h = hw.get_homework(req.homework_id)
    if not h or h.get("status") != "assigned":
        raise HTTPException(status_code=404, detail="作业未找到或未布置")

    student_name = (req.student_name or "").strip()
    if not student_name:
        raise HTTPException(status_code=400, detail="请填写学生姓名")

    # answers 规整
    answers = {}
    for i, q in enumerate(h["questions"]):
        raw = req.answers.get(str(i))
        if raw is None:
            continue
        if q["type"] == "choice":
            try:
                answers[str(i)] = int(raw)
            except (TypeError, ValueError):
                answers[str(i)] = -1
        elif q["type"] == "judge":
            answers[str(i)] = bool(raw)
        else:
            answers[str(i)] = str(raw).strip()

    result = hw.grade_submission(h, answers, model=None)
    sub = hw.add_submission(
        h["id"], student_name, req.student_class, answers, result
    )

    # 返回批改详情（含正确答案/解析，供学生复盘）
    detail = []
    for i, q in enumerate(h["questions"]):
        qid = str(i)
        g = result["grading"].get(qid, {})
        detail.append({
            "index": i,
            "type": q["type"],
            "stem": q["stem"],
            "options": q.get("options", []) if q["type"] == "choice" else [],
            "student_answer": answers.get(qid),
            "correct_answer": g.get("correct_answer"),
            "score": g.get("score", 0),
            "max": g.get("max", q["score"]),
            "correct": g.get("correct"),
            "analysis": q.get("analysis", ""),
            "reference": q.get("reference", "") if q["type"] == "short" else "",
            "needs_review": g.get("needs_review", False),
        })

    return {
        "ok": True,
        "submission_id": sub["id"],
        "score": result["score"],
        "total": result["total"],
        "passed": result["passed"],
        "wrong_questions": result["wrong_questions"],
        "needs_review": result["needs_review"],
        "detail": detail,
    }


# ————————————————————————— 教师端 —————————————————————————
@router.post("/api/homework/design")
def design(req: DesignReq):
    return _do_design(req, None)


@router.post("/api/homework/design/async")
def design_async(req: DesignReq):
    """提交 AI 出题任务，立即返回 job_id，前端轮询 /api/jobs/{job_id} 显示进度。"""
    if not (req.subject or "").strip():
        raise HTTPException(status_code=400, detail="请至少填写科目")
    job = jobs.submit("homework_design", lambda j: _do_design(req, j))
    return {"ok": True, "job_id": job.id}


def _do_design(req: DesignReq, job=None) -> dict:
    """实际执行 AI 出题；job 非空时按阶段上报进度。"""
    def step(pct, stage, detail=None):
        if job is not None:
            job.update(pct, stage, detail)

    step(4, "校验出题参数")
    step(10, "检索你的教学资料")
    # 检索 + 大模型出题是一段连续阻塞：用 Ticker 让进度条持续前进到 78%
    tk = jobs.Ticker(job, 10, 78, "大模型正在出题…").start()
    try:
        draft = hw.design_homework(
            req.dict(exclude={"model"}), model=req.model, teacher_id=req.teacher_id
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        msg = str(e)
        # 连接/密钥类问题给可自查的提示，内容问题则原样返回
        if "调用大模型失败" in msg or "connection" in msg.lower():
            raise HTTPException(status_code=503, detail=_explain_design_error(e))
        raise HTTPException(status_code=502, detail=msg)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=_explain_design_error(e))
    finally:
        tk.stop()

    step(84, "校验题目与分值", f"共 {len(draft.get('questions') or [])} 道题")
    step(94, "整理题目")
    return {
        "ok": True,
        "title": draft["title"],
        "subject": draft["subject"],
        "description": draft.get("description", ""),
        "total_score": draft["total_score"],
        "questions": draft["questions"],
    }


def _explain_design_error(e: Exception) -> str:
    """把底层异常翻译成教师能看懂、能自助排查的中文提示。"""
    return explain_llm_error(e)


@router.post("/api/homework/save")
def save(req: SaveReq):
    if not req.teacher_id or not req.subject or not req.title:
        raise HTTPException(status_code=400, detail="教师身份、科目、标题为必填项")
    if not req.questions:
        raise HTTPException(status_code=400, detail="作业至少需要一道题")
    h = hw.save_homework({
        "id": req.id,
        "teacher_id": req.teacher_id,
        "subject": req.subject,
        "title": req.title,
        "description": req.description,
        "assign_class": req.assign_class,
        "deadline": req.deadline,
        "status": req.status if req.status in ("draft", "assigned") else "draft",
        "questions": req.questions,
    })
    return {"ok": True, "id": h["id"], "status": h["status"], "total_score": h["total_score"]}


@router.post("/api/homework/assign/{hw_id}")
def assign(hw_id: str):
    h = hw.set_status(hw_id, "assigned")
    if not h:
        raise HTTPException(status_code=404, detail="作业未找到")
    return {"ok": True, "id": hw_id, "status": "assigned"}


@router.post("/api/homework/unassign/{hw_id}")
def unassign(hw_id: str):
    h = hw.set_status(hw_id, "draft")
    if not h:
        raise HTTPException(status_code=404, detail="作业未找到")
    return {"ok": True, "id": hw_id, "status": "draft"}


@router.get("/api/homework/list")
def list_hw(teacher_id: str = ""):
    items = hw.list_homeworks(teacher_id=teacher_id)
    return {"homeworks": items}


@router.get("/api/homework/{hw_id}")
def get_hw(hw_id: str):
    h = hw.get_homework(hw_id)
    if not h:
        raise HTTPException(status_code=404, detail="作业未找到")
    h = dict(h)
    h["question_count"] = len(h.get("questions", []))
    return h


@router.delete("/api/homework/{hw_id}")
def delete_hw(hw_id: str):
    if not hw.delete_homework(hw_id):
        raise HTTPException(status_code=404, detail="作业未找到")
    return {"ok": True}


@router.get("/api/homework/analytics/{hw_id}")
def analytics(hw_id: str):
    a = hw.compute_analytics(hw_id)
    if a is None:
        raise HTTPException(status_code=404, detail="作业未找到")
    return a


@router.get("/api/homework/submissions/{hw_id}")
def submissions(hw_id: str):
    subs = hw.list_submissions(hw_id)
    out = []
    for s in sorted(subs, key=lambda x: x.get("submitted_at", 0), reverse=True):
        out.append({
            "id": s["id"],
            "student_name": s.get("student_name"),
            "student_class": s.get("student_class"),
            "submitted_at": s.get("submitted_at"),
            "score": s["score"],
            "total": s["total"],
            "passed": s["passed"],
            "needs_review": s.get("needs_review"),
        })
    return {"submissions": out}


@router.post("/api/homework/rescore")
def rescore(req: RescoreReq):
    sub = hw.update_short_score(req.homework_id, req.sub_id, req.qid, req.score)
    if not sub:
        raise HTTPException(status_code=404, detail="提交或题目未找到")
    return {"ok": True, "submission": sub}
