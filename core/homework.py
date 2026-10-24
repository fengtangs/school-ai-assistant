"""作业中心：AI 出题、布置、作答批改与学情分析。

数据全部以 JSON 文件落地（与教师助手一致的轻量方案，无需数据库）：
    data/homework/homeworks.json             全部作业（含题目）
    data/homework/submissions/{hw_id}.json   每份作业的提交记录

题型：
    choice  选择题（4 选项，下标作答，自动判分）
    judge   判断题（true/false，自动判分）
    short   简答题（由大模型按参考答案评分；无模型时标记「需教师批改」）

满分固定 100 分。所有分数在保存时强制归一，保证三题型分值之和恰为 100。
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

from core.llm import get_llm
from core.docgen import parse_json_block

PASS_SCORE = 60
_QTYPE_LABELS = {"choice": "选择题", "judge": "判断题", "short": "简答题"}


# ————————————————————————————————————————————————
# 路径与基础工具
# ————————————————————————————————————————————————
def homework_root() -> Path:
    return Path(os.getenv("HOMEWORK_DIR", "data/homework")).resolve()


def _homeworks_file() -> Path:
    return homework_root() / "homeworks.json"


def _submissions_file(hw_id: str) -> Path:
    return homework_root() / "submissions" / f"{hw_id}.json"


def safe_id(s: str, maxlen: int = 64) -> str:
    """净化标识（教师/学生/班级名），只保留字母数字下划线中文连字符。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]", "", str(s or ""))
    return cleaned[:maxlen] or "anon"


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ————————————————————————————————————————————————
# 分值归一：保证题目分值之和恒为 100
# ————————————————————————————————————————————————
def normalize_scores(questions: list) -> None:
    """就地调整每题 score，使总分恰为 100。不破坏题型分布。"""
    if not questions:
        return
    for q in questions:
        try:
            q["score"] = max(0, int(q.get("score", 0)))
        except (TypeError, ValueError):
            q["score"] = 0

    total = sum(q["score"] for q in questions)
    target = 100
    if total == target:
        return
    if total <= 0:
        base = target // len(questions)
        for q in questions:
            q["score"] = base
        questions[-1]["score"] += target - base * len(questions)
        return
    scaled = [round(q["score"] * target / total) for q in questions]
    diff = target - sum(scaled)
    scaled[-1] += diff
    if scaled[-1] < 0:
        scaled[-1] = 0
    for q, s in zip(questions, scaled):
        q["score"] = s


# ————————————————————————————————————————————————
# 作业 CRUD
# ————————————————————————————————————————————————
def list_homeworks(teacher_id: str = None, status: str = None) -> list:
    all_hw = _read_json(_homeworks_file(), [])
    out = all_hw
    if teacher_id:
        out = [h for h in out if h.get("teacher_id") == safe_id(teacher_id)]
    if status:
        out = [h for h in out if h.get("status") == status]
    out = sorted(out, key=lambda x: x.get("created_at", 0), reverse=True)
    for h in out:
        h["question_count"] = len(h.get("questions", []))
    return out


def get_homework(hw_id: str) -> dict:
    for h in _read_json(_homeworks_file(), []):
        if h.get("id") == hw_id:
            return h
    return None


def save_homework(hw: dict) -> dict:
    """新增或更新一份作业（按 id 去重）。返回保存后的作业。"""
    hw_id = hw.get("id")
    if not hw_id:
        hw_id = "hw_" + uuid.uuid4().hex[:12]
        hw["id"] = hw_id
    hw["teacher_id"] = safe_id(hw.get("teacher_id") or "anon")
    hw["subject"] = (hw.get("subject") or "未分类").strip()
    hw["title"] = (hw.get("title") or "未命名作业").strip()
    normalize_scores(hw.setdefault("questions", []))
    hw["total_score"] = sum(q["score"] for q in hw["questions"])
    hw["updated_at"] = int(time.time())
    if "created_at" not in hw:
        hw["created_at"] = int(time.time())
    if "status" not in hw:
        hw["status"] = "draft"

    all_hw = _read_json(_homeworks_file(), [])
    replaced = False
    for i, h in enumerate(all_hw):
        if h.get("id") == hw_id:
            all_hw[i] = hw
            replaced = True
            break
    if not replaced:
        all_hw.append(hw)
    _write_json(_homeworks_file(), all_hw)
    return hw


def delete_homework(hw_id: str) -> bool:
    all_hw = _read_json(_homeworks_file(), [])
    new = [h for h in all_hw if h.get("id") != hw_id]
    if len(new) == len(all_hw):
        return False
    _write_json(_homeworks_file(), new)
    sf = _submissions_file(hw_id)
    if sf.exists():
        sf.unlink()
    return True


def set_status(hw_id: str, status: str) -> dict:
    hw = get_homework(hw_id)
    if not hw:
        return None
    hw["status"] = status
    hw["updated_at"] = int(time.time())
    save_homework(hw)
    return hw


# ————————————————————————————————————————————————
# 学生端：按科目浏览可做的作业
# ————————————————————————————————————————————————
def list_assigned() -> list:
    """返回已布置（status=assigned）的作业摘要，供学生按科目选择。"""
    out = []
    for h in list_homeworks(status="assigned"):
        out.append({
            "id": h["id"],
            "subject": h["subject"],
            "title": h["title"],
            "teacher_id": h["teacher_id"],
            "assign_class": h.get("assign_class", ""),
            "deadline": h.get("deadline", ""),
            "question_count": len(h.get("questions", [])),
            "total_score": h.get("total_score", 100),
        })
    return out


def list_subjects() -> list:
    """已布置作业涉及到的科目列表（学生选择用）。"""
    subs = []
    for h in list_homeworks(status="assigned"):
        if h["subject"] not in subs:
            subs.append(h["subject"])
    return subs


# ————————————————————————————————————————————————
# AI 出题
# ————————————————————————————————————————————————
_DESIGN_SYSTEM = (
    "你是一位资深学科教师与命题专家，擅长依据课程标准与教学要求编制高质量测验题。"
    "题目必须严谨、无歧义、答案唯一正确；简答题的参考答案要点清晰、可评分。"
    "若提供了【参考资料】，应优先结合资料内容命题；资料为空则基于学科常识命题，但不得编造事实性错误。"
    "输出必须是纯 JSON，不要 ```json 代码块标记，不要任何解释性文字。所有内容使用简体中文。"
)

_DESIGN_PROMPT = (
    "请为以下课程/知识点设计一份总分为 100 分的作业，包含选择题、判断题、简答题三类题型。\n\n"
    "科目：$subject\n"
    "作业标题：$title\n"
    "知识点 / 章节：$topic\n"
    "适用对象：$students\n"
    "难度：$difficulty\n"
    "题量要求：选择题 $n_choice 题、判断题 $n_judge 题、简答题 $n_short 题\n"
    "补充要求：$requirements\n\n"
    "【分值分配】选择题每题 $s_choice 分、判断题每题 $s_judge 分、简答题每题 $s_short 分，"
    "三者分值之和必须为 100 分。\n\n"
    "输出 JSON 结构（严格遵守）：\n"
    '{\n'
    '  "title": "作业标题",\n'
    '  "subject": "科目",\n'
    '  "description": "作业说明 / 注意事项（1-2 句）",\n'
    '  "questions": [\n'
    '    {"type":"choice","stem":"题干","options":["A. xx","B. xx","C. xx","D. xx"],"answer": 1,"score": 20,"analysis":"解析（可选）"},\n'
    '    {"type":"judge","stem":"题干","answer": true,"score": 10,"analysis":"解析（可选）"},\n'
    '    {"type":"short","stem":"题干","reference":"参考答案要点","score": 20,"analysis":"评分要点（可选）"}\n'
    "  ]\n"
    "}\n\n"
    "注意：\n"
    "1. choice 的 answer 是正确选项的【下标，从 0 开始】；options 为 4 个，每个选项以 \"A. \" \"B. \" 等前缀开头。\n"
    "2. judge 的 answer 为 true 或 false。\n"
    "3. short 的 reference 是参考答案/评分要点，score 为该题分值。\n"
    "4. 所有题目 score 之和必须等于 100；题目应覆盖不同认知层次，避免歧义。\n\n"
    "【参考资料】\n$context"
)


def _distribute_scores(n_choice: int, n_judge: int, n_short: int) -> tuple:
    """把 100 分按题量大致均分到三类题型（保证整数且和为 100）。"""
    n_choice = max(0, int(n_choice))
    n_judge = max(0, int(n_judge))
    n_short = max(0, int(n_short))
    total_n = n_choice + n_judge + n_short
    if total_n == 0:
        n_choice, n_judge, n_short = 5, 5, 2
        total_n = 12
    raw = [100 * n_choice / total_n, 100 * n_judge / total_n, 100 * n_short / total_n]
    s = [int(x) for x in raw]
    rem = 100 - sum(s)
    # 把余数补到题量最多的题型上
    order = sorted(range(3), key=lambda i: -raw[i])
    for i in order:
        if rem <= 0:
            break
        s[i] += 1
        rem -= 1
    # 若仍剩余（极小概率），补到最后
    s[2] += rem
    return s[0] or 1, s[1] or 1, s[2] or 1


def design_homework(params: dict, model: str = None, teacher_id: str = None) -> dict:
    """调用大模型生成作业题目（结构化 JSON）。失败时抛异常由调用方降级。"""
    llm = get_llm()
    if llm is None:
        raise RuntimeError("大模型未就绪，请检查 .env 中的 LLM_PROVIDER 与对应密钥")

    n_choice = int(params.get("n_choice") or 5)
    n_judge = int(params.get("n_judge") or 5)
    n_short = int(params.get("n_short") or 2)
    s_choice, s_judge, s_short = _distribute_scores(n_choice, n_judge, n_short)

    # 若教师已上传资料，则基于其资料命题（RAG），让作业更贴合教学实际
    context = "（未提供参考资料）"
    if teacher_id:
        try:
            from core.teacher import TeacherStore

            store = TeacherStore(teacher_id)
            q = " ".join(str(params.get(k) or "") for k in ("subject", "topic", "requirements"))
            ctx, _ = store.build_context(q or "作业", top_k=6)
            if ctx.strip():
                context = ctx
        except Exception:  # noqa: BLE001  资料缺失或非致命，忽略即可
            pass

    import string

    user = string.Template(_DESIGN_PROMPT).safe_substitute(
        subject=params.get("subject") or "未指定",
        title=params.get("title") or (params.get("subject") or "作业") + " 练习",
        topic=params.get("topic") or "未指定",
        students=params.get("students") or "未指定",
        difficulty=params.get("difficulty") or "中等",
        n_choice=n_choice, n_judge=n_judge, n_short=n_short,
        s_choice=s_choice, s_judge=s_judge, s_short=s_short,
        requirements=params.get("requirements") or "无",
        context=context,
    )

    max_tokens = int(os.getenv("TEACHER_MAX_TOKENS", "8192"))
    messages = [
        {"role": "system", "content": _DESIGN_SYSTEM},
        {"role": "user", "content": user},
    ]

    # 思考型模型偶尔会「思考耗尽 token 导致正文为空」或返回非 JSON，
    # 这里重试一次，显著降低偶发失败率（与教师材料生成保持一致）。
    raw, last_err, spec = "", "", None
    for attempt in range(2):
        try:
            raw = llm.chat(
                messages,
                model=model,
                json_mode=True,
                temperature=0.4,
                max_tokens=max_tokens,
            )
        except Exception as e:  # noqa: BLE001  网络/网关异常，重试一次
            last_err = f"调用大模型失败：{e}"
            continue
        if not raw or not raw.strip():
            last_err = (
                f"模型返回空内容（第 {attempt + 1} 次）。通常是思考过程耗尽了 "
                f"max_tokens={max_tokens}，可调大 TEACHER_MAX_TOKENS 或换个模型重试"
            )
            continue
        try:
            spec = parse_json_block(raw)
        except ValueError as e:
            last_err = f"{e}（第 {attempt + 1} 次）"
            spec = None
            continue
        if isinstance(spec.get("questions"), list) and spec["questions"]:
            break
        last_err = f"模型未返回题目列表（第 {attempt + 1} 次）"
        spec = None

    if spec is None:
        raise ValueError(last_err or "模型未返回有效题目")

    questions = spec.get("questions") or []
    if not isinstance(questions, list) or not questions:
        raise ValueError("模型未返回题目列表")

    # 规整每题字段
    clean = []
    for i, q in enumerate(questions):
        qtype = str(q.get("type") or "").lower()
        if qtype not in ("choice", "judge", "short"):
            qtype = "short"
        item = {"type": qtype, "stem": str(q.get("stem") or "").strip()}
        if qtype == "choice":
            opts = q.get("options") or []
            item["options"] = [str(o).strip() for o in opts][:4]
            while len(item["options"]) < 4:
                item["options"].append(f"{'ABCD'[len(item['options'])]}. （选项缺失）")
            try:
                item["answer"] = int(q.get("answer", 0))
            except (TypeError, ValueError):
                item["answer"] = 0
        elif qtype == "judge":
            item["answer"] = bool(q.get("answer"))
        else:
            item["reference"] = str(q.get("reference") or "").strip()
        item["analysis"] = str(q.get("analysis") or "").strip()
        clean.append(item)

    hw = {
        "title": str(spec.get("title") or params.get("title") or "AI 生成的作业").strip(),
        "subject": str(spec.get("subject") or params.get("subject") or "未分类").strip(),
        "description": str(spec.get("description") or "").strip(),
        "questions": clean,
    }
    normalize_scores(hw["questions"])
    hw["total_score"] = sum(q["score"] for q in hw["questions"])
    return hw


# ————————————————————————————————————————————————
# 批改
# ————————————————————————————————————————————————
def _grade_objective(hw: dict, answers: dict) -> dict:
    """客观题（选择/判断）自动判分，返回 {qid: {correct, score, max, correct_answer}}。"""
    grading = {}
    for i, q in enumerate(hw["questions"]):
        qid = str(i)
        if q["type"] == "short":
            continue
        qmax = q["score"]
        stu = answers.get(qid)
        if q["type"] == "choice":
            correct = stu is not None and int(stu) == int(q["answer"])
            correct_answer = q["answer"]
        else:  # judge
            correct = stu is not None and bool(stu) == bool(q["answer"])
            correct_answer = q["answer"]
        grading[qid] = {
            "correct": bool(correct),
            "score": qmax if correct else 0,
            "max": qmax,
            "correct_answer": correct_answer,
            "type": q["type"],
        }
    return grading


_SHORT_GRADE_SYSTEM = (
    "你是评分助手。根据题目的参考答案要点，对学生的简答题作答给出 0 到该题满分之间的整数分。"
    "要点完整给满分，部分正确给一半左右，错误/空白给 0 分。只输出 JSON，不要解释。"
)


def _grade_short_answers(hw: dict, answers: dict, model: str = None) -> tuple:
    """简答题评分：交由大模型一次性判分，返回 (grading, needs_review)。"""
    short_qs = [(i, q) for i, q in enumerate(hw["questions"]) if q["type"] == "short"]
    grading = {}
    if not short_qs:
        return grading, False

    items = []
    ans_lines = []
    for i, q in short_qs:
        qid = str(i)
        items.append(f"题号 {qid}（满分 {q['score']} 分）\n题干：{q['stem']}\n参考答案要点：{q.get('reference','')}")
        ans_lines.append(f"题号 {qid} 学生作答：{answers.get(qid) or '（未作答）'}")
    user = "【题目与参考答案】\n" + "\n\n".join(items) + "\n\n【学生作答】\n" + "\n".join(ans_lines) + \
           "\n\n请输出 JSON：{\"<题号>\": <整数得分>}，题号用上方数字字符串（如 \"2\"）。只判分不解释。"

    llm = get_llm()
    if llm is None:
        # 无模型：标记需教师批改，暂不计入得分
        for i, q in short_qs:
            grading[str(i)] = {
                "correct": None, "score": 0, "max": q["score"],
                "correct_answer": q.get("reference", ""), "type": "short",
                "needs_review": True,
            }
        return grading, True

    try:
        raw = llm.chat(
            [
                {"role": "system", "content": _SHORT_GRADE_SYSTEM},
                {"role": "user", "content": user},
            ],
            model=model, json_mode=True, temperature=0, max_tokens=1024,
        )
        scores = parse_json_block(raw)
    except Exception:  # noqa: BLE001
        scores = {}

    needs_review = False
    for i, q in short_qs:
        qid = str(i)
        qmax = q["score"]
        try:
            s = int(scores.get(qid, 0))
        except (TypeError, ValueError):
            s = 0
        s = max(0, min(qmax, s))
        # 模型完全没给分 → 标记需复核
        if qid not in scores:
            needs_review = True
        grading[qid] = {
            "correct": (s >= qmax), "score": s, "max": qmax,
            "correct_answer": q.get("reference", ""), "type": "short",
            "needs_review": qid not in scores,
        }
    return grading, needs_review


def grade_submission(hw: dict, answers: dict, model: str = None) -> dict:
    """对一份作答做完整批改，返回批改明细、总分、错题与是否需复核。"""
    obj_grading = _grade_objective(hw, answers)
    short_grading, short_review = _grade_short_answers(hw, answers, model)
    grading = {**obj_grading, **short_grading}

    score = sum(g["score"] for g in grading.values())
    score = max(0, min(hw.get("total_score", 100), score))

    wrong = [qid for qid, g in grading.items()
             if g["type"] != "short" and not g["correct"]
             or (g["type"] == "short" and g.get("correct") is False)]
    # 简答题按"未满分"计入错题展示
    wrong = [qid for qid, g in grading.items()
             if (g["type"] != "short" and not g["correct"])
             or (g["type"] == "short" and g["score"] < g["max"])]

    passed = score >= PASS_SCORE
    return {
        "grading": grading,
        "score": score,
        "total": hw.get("total_score", 100),
        "passed": passed,
        "wrong_questions": wrong,
        "needs_review": short_review or any(
            g.get("needs_review") for g in grading.values()
        ),
    }


# ————————————————————————————————————————————————
# 提交记录
# ————————————————————————————————————————————————
def add_submission(hw_id: str, student_name: str, student_class: str,
                  answers: dict, grading_result: dict) -> dict:
    sub = {
        "id": "sub_" + uuid.uuid4().hex[:12],
        "homework_id": hw_id,
        "student_name": safe_id(student_name, 40),
        "student_class": safe_id(student_class, 40),
        "submitted_at": int(time.time()),
        "answers": answers,
        "score": grading_result["score"],
        "total": grading_result["total"],
        "passed": grading_result["passed"],
        "wrong_questions": grading_result["wrong_questions"],
        "needs_review": grading_result["needs_review"],
        "grading": grading_result["grading"],
    }
    path = _submissions_file(hw_id)
    subs = _read_json(path, [])
    subs.append(sub)
    _write_json(path, subs)
    return sub


def list_submissions(hw_id: str) -> list:
    return _read_json(_submissions_file(hw_id), [])


def update_short_score(hw_id: str, sub_id: str, qid: str, new_score: int) -> dict:
    """教师手动批改/修正某份提交中某道简答题得分。返回更新后的 submission。"""
    subs = list_submissions(hw_id)
    for sub in subs:
        if sub.get("id") != sub_id:
            continue
        g = sub["grading"].get(qid)
        if not g or g.get("type") != "short":
            return None
        qmax = g["max"]
        new_score = max(0, min(qmax, int(new_score)))
        g["score"] = new_score
        g["needs_review"] = False
        g["correct"] = (new_score >= qmax)
        sub["score"] = sum(x["score"] for x in sub["grading"].values())
        sub["score"] = max(0, min(sub["total"], sub["score"]))
        sub["passed"] = sub["score"] >= PASS_SCORE
        sub["needs_review"] = any(x.get("needs_review") for x in sub["grading"].values())
        sub["wrong_questions"] = [
            q for q, x in sub["grading"].items()
            if (x["type"] != "short" and not x["correct"])
            or (x["type"] == "short" and x["score"] < x["max"])
        ]
        _write_json(_submissions_file(hw_id), subs)
        return sub
    return None


# ————————————————————————————————————————————————
# 学情分析
# ————————————————————————————————————————————————
def compute_analytics(hw_id: str) -> dict:
    hw = get_homework(hw_id)
    if not hw:
        return None
    subs = list_submissions(hw_id)
    questions = hw.get("questions", [])
    n_q = len(questions)

    submit_count = len(subs)
    # 去重人数（姓名+班级）
    students = {(s.get("student_name"), s.get("student_class")) for s in subs}
    student_count = len(students)

    if submit_count == 0:
        return {
            "homework_id": hw_id,
            "title": hw.get("title"),
            "subject": hw.get("subject"),
            "total_score": hw.get("total_score", 100),
            "submit_count": 0,
            "student_count": 0,
            "avg_score": 0,
            "pass_rate": 0,
            "accuracy_rate": 0,
            "score_distribution": {},
            "per_question": [],
            "wrong_ranking": [],
            "needs_review_count": 0,
        }

    scores = [s["score"] for s in subs]
    avg_score = sum(scores) / len(scores)
    total = hw.get("total_score", 100) or 100
    accuracy_rate = avg_score / total if total else 0
    pass_n = sum(1 for s in scores if s >= PASS_SCORE)
    pass_rate = pass_n / len(scores)

    # 分数段分布
    bands = [("90-100", 90, 101), ("80-89", 80, 90), ("70-79", 70, 80),
             ("60-69", 60, 70), ("0-59", 0, 60)]
    dist = {lab: 0 for lab, _, _ in bands}
    for s in scores:
        for lab, lo, hi in bands:
            if lo <= s < hi:
                dist[lab] += 1
                break

    # 逐题正确率 / 错题统计
    per_q = []
    for i, q in enumerate(questions):
        qid = str(i)
        answered = 0
        correct_like = 0  # 客观题答对；简答题得分==满分
        wrong_n = 0
        score_sum = 0
        max_sum = 0
        for s in subs:
            g = s["grading"].get(qid)
            if not g:
                continue
            answered += 1
            max_sum += g["max"]
            score_sum += g["score"]
            if q["type"] == "short":
                if g["score"] >= g["max"]:
                    correct_like += 1
                if g["score"] < g["max"]:
                    wrong_n += 1
            else:
                if g["correct"]:
                    correct_like += 1
                else:
                    wrong_n += 1
        per_q.append({
            "id": qid,
            "type": q["type"],
            "type_label": _QTYPE_LABELS.get(q["type"], q["type"]),
            "stem": q.get("stem", "")[:60],
            "max": q["score"],
            "answered": answered,
            "correct_rate": (correct_like / answered) if answered else 0,
            "wrong_count": wrong_n,
            "avg_score_rate": (score_sum / max_sum) if max_sum else 0,
        })

    # 错题排行：按错误人数降序
    wrong_ranking = sorted(
        [p for p in per_q if p["wrong_count"] > 0],
        key=lambda x: (-x["wrong_count"], x["correct_rate"]),
    )

    needs_review_count = sum(1 for s in subs if s.get("needs_review"))

    return {
        "homework_id": hw_id,
        "title": hw.get("title"),
        "subject": hw.get("subject"),
        "total_score": total,
        "submit_count": submit_count,
        "student_count": student_count,
        "avg_score": round(avg_score, 1),
        "pass_rate": round(pass_rate, 3),
        "accuracy_rate": round(accuracy_rate, 3),
        "score_distribution": dist,
        "per_question": per_q,
        "wrong_ranking": wrong_ranking,
        "needs_review_count": needs_review_count,
    }
