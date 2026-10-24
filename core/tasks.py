"""教师助手的任务模板：备课 / 教案 / 课件 / 学情分析。

每种任务定义：
- fields  ：前端需要教师填写的参数
- output  ：产物类型（docx / pptx）
- system  ：系统提示词（角色约束 + 禁止编造）
- prompt  ：用户提示词模板（含严格的 JSON 输出格式）

设计原则：**所有任务都先让模型产出结构化 JSON，再由 docgen 渲染成文件**。
这样比让模型直接生成 Markdown 再转文件稳定得多，也不会出现格式漂移。

统一的 docx JSON 结构（备课/教案/学情分析共用）：

    {
      "title": "文档标题",
      "meta": {"课程名称": "...", "课题": "...", ...},   // 键值对，渲染成信息栏
      "sections": [
        {"heading": "章节标题", "body": "段落正文（可空）", "items": ["列表项", ...]}
      ]
    }

PPT 的 JSON 结构：

    {
      "title": "课件标题",
      "subtitle": "副标题",
      "slides": [
        {"title": "页标题", "bullets": ["要点1", "要点2"], "note": "讲解提示（可空）"}
      ]
    }
"""

# 所有任务共用的“不许编造”约束
_NO_FABRICATE = (
    "【必须遵守】\n"
    "1. 内容必须严格基于下面提供的「参考资料」。资料里没有的信息，"
    "宁可写“资料未涉及，需教师补充”，绝对不要编造数据、文件号、人名、成绩。\n"
    "2. 如果参考资料为空或与任务无关，就直接说明“未提供可用资料”，"
    "并给出一个通用框架，且明确标注哪部分需要教师补充。\n"
    "3. 输出必须是**纯 JSON**，不要加 ```json 代码块标记，不要任何解释性文字。\n"
    "4. 所有内容使用简体中文。\n"
)

_DOCX_SCHEMA = (
    "输出 JSON 结构（严格遵守）：\n"
    '{"title":"文档标题",'
    '"meta":{"课程名称":"...","课题":"...","课时":"..."},'
    '"sections":[{"heading":"章节标题","body":"段落正文","items":["要点1","要点2"]}]}\n'
    "说明：meta 可放 2-5 个键值对；sections 至少 4 个章节；"
    "每个 section 的 body 与 items 至少有一个非空。"
)

_PPT_SCHEMA = (
    "输出 JSON 结构（严格遵守）：\n"
    '{"title":"课件标题","subtitle":"副标题（学科/章节/适用对象）",'
    '"slides":[ ... ]}\n'
    "slides 每一页必须是下列四种之一（type 字段必填）：\n"
    '1) 目录页  {"type":"toc","title":"目录","bullets":["01 网络基础","02 路由基础","03 交换基础"]}\n'
    '2) 章节扉页 {"type":"section","number":"01","title":"网络基础","bullets":["OSI 模型","IP 编址"]}\n'
    '3) 要点页  {"type":"content","title":"页标题",'
    '"bullets":["要点1","要点2","要点3"],"image_prompt":"配图提示词","note":"演讲者备注"}\n'
    '4) 数据页  {"type":"data","title":"页标题",'
    '"stats":[{"value":"25%","label":"路由基础"},{"value":"20%","label":"网络基础"}],'
    '"bullets":["补充说明"],"image_prompt":"配图提示词","note":""}\n'
    "要求：\n"
    "- 第一页用 toc（目录），之后用 section 划分 2-4 个章节，每个章节下跟 1-3 页 content；"
    "遇到分值占比、统计数据、关键数字时用 data 页\n"
    "- 每页 bullets 3-5 条，每条不超过 30 字，是短语不是整段话（data 页 bullets 可为空）\n"
    "- 封面与结束页由程序自动生成，**不要**输出 cover/end 类型的页\n"
    "- note 是讲给学生的口语化提示，30 字以内，可为空字符串\n"
    "- **image_prompt**：为 content / data 页写一段用于 AI 生成配图的提示词，"
    "描述该页要配的示意图（画什么、什么风格），不要写文字说明；"
    "风格统一为「扁平简约、白底、蓝色系、教学示意图、无文字」；"
    "toc 与 section 页不要给 image_prompt"
)


TASKS = {
    # ———————————————————————— 备课 ————————————————————————
    "lesson_prep": {
        "id": "lesson_prep",
        "name": "备课",
        "desc": "生成备课稿：教材分析、学情预判、教学目标、重难点、教学过程、板书与作业设计",
        "output": "docx",
        "icon": "📝",
        "fields": [
            {"key": "course", "label": "课程名称", "placeholder": "如：人工智能技术应用", "required": True},
            {"key": "topic", "label": "课题 / 章节", "placeholder": "如：第三章 机器学习基础", "required": True},
            {"key": "hours", "label": "课时", "placeholder": "如：2课时", "required": False, "default": "2课时"},
            {"key": "students", "label": "授课对象", "placeholder": "如：2026级人工智能1班（高职一年级）", "required": False},
            {"key": "requirements", "label": "补充要求", "placeholder": "如：结合华为ICT认证考点、加强实训环节", "required": False},
        ],
        "system": (
            "你是一位经验丰富的高职院校教师，擅长编写高质量的备课稿。"
            "你的备课稿结构清晰、可操作性强，能直接用于课堂教学。\n" + _NO_FABRICATE
        ),
        "prompt": (
            "请基于参考资料，为以下课程编写一份完整的**备课稿**。\n\n"
            "课程：$course\n课题：$topic\n课时：$hours\n授课对象：$students\n补充要求：$requirements\n\n"
            "备课稿需包含这些章节：教材分析、学情分析、教学目标（知识/能力/素质三维）、"
            "教学重点、教学难点、教学方法与手段、教学过程设计（分环节并标注时间）、"
            "板书设计、作业与预习布置、教学反思要点。\n\n"
            + _DOCX_SCHEMA
            + "\n\n【参考资料】\n$context"
        ),
    },

    # ———————————————————————— 教案 ————————————————————————
    "lesson_plan": {
        "id": "lesson_plan",
        "name": "教案",
        "desc": "生成规范教案：可直接提交教学检查的标准格式教案",
        "output": "docx",
        "icon": "📋",
        "fields": [
            {"key": "course", "label": "课程名称", "placeholder": "如：信息安全技术", "required": True},
            {"key": "topic", "label": "课题", "placeholder": "如：网络攻击与防御概述", "required": True},
            {"key": "hours", "label": "课时", "placeholder": "如：2课时", "required": False, "default": "2课时"},
            {"key": "class_name", "label": "授课班级", "placeholder": "如：2026级信息安全1班", "required": False},
            {"key": "requirements", "label": "补充要求", "placeholder": "如：融入课程思政元素", "required": False},
        ],
        "system": (
            "你是一位高职院校教学骨干，熟悉规范教案的写法，擅长把课上得既有知识密度"
            "又有实操环节。你写的教案可以直接提交教学检查。\n" + _NO_FABRICATE
        ),
        "prompt": (
            "请基于参考资料，为以下课程编写一份**规范教案**。\n\n"
            "课程：$course\n课题：$topic\n课时：$hours\n授课班级：$class_name\n补充要求：$requirements\n\n"
            "教案需包含这些章节：教学目标、教学重点与难点、教学方法、教学准备、"
            "教学过程（按“导入新课→讲授新知→课堂练习→归纳小结→布置作业”分环节，"
            "每环节注明教师活动、学生活动与时间分配）、板书设计、作业布置、课后反思。\n\n"
            + _DOCX_SCHEMA
            + "\n\n【参考资料】\n$context"
        ),
    },

    # ———————————————————————— 课件 ————————————————————————
    "ppt": {
        "id": "ppt",
        "name": "课件制作",
        "desc": "生成可直接讲解的 PPT 课件（.pptx 文件，16:9）",
        "output": "pptx",
        "icon": "📊",
        "fields": [
            {"key": "course", "label": "课程名称", "placeholder": "如：人工智能技术应用", "required": True},
            {"key": "topic", "label": "课件主题", "placeholder": "如：深度学习入门", "required": True},
            {"key": "slide_count", "label": "预计页数", "placeholder": "10-20", "required": False, "default": "12"},
            {"key": "audience", "label": "适用对象", "placeholder": "如：高职二年级学生", "required": False},
            {"key": "requirements", "label": "补充要求", "placeholder": "如：多举生活化例子、突出实训步骤", "required": False},
        ],
        "system": (
            "你是一位擅长制作教学课件的教师。你懂“一页一个观点”，"
            "要点精炼、层次分明，适合课堂投影讲解。\n" + _NO_FABRICATE
        ),
        "prompt": (
            "请基于参考资料，制作一份**教学课件**的内容大纲。\n\n"
            "课程：$course\n主题：$topic\n预计页数：$slide_count 页\n"
            "适用对象：$audience\n补充要求：$requirements\n\n"
            "课件结构建议：封面 → 学习目标 → 课程导入 → 核心知识点（多页）→ "
            "案例分析/实训演示 → 课堂练习 → 小结 → 课后作业。\n\n"
            + _PPT_SCHEMA
            + "\n\n【参考资料】\n$context"
        ),
    },

    # ———————————————————————— 学情分析 ————————————————————————
    "analysis": {
        "id": "analysis",
        "name": "学情分析",
        "desc": "分析成绩数据或班级情况描述，输出学情报告与教学改进对策",
        "output": "docx",
        "icon": "📈",
        "fields": [
            {"key": "course", "label": "课程名称", "placeholder": "如：信息安全技术", "required": True},
            {"key": "class_name", "label": "班级", "placeholder": "如：2026级信息安全1班", "required": True},
            {"key": "exam", "label": "考试/考核名称", "placeholder": "如：期中考试", "required": False},
            {"key": "situation", "label": "班级情况描述", "placeholder": "如：及格率偏低，学生对加密算法理解困难，实训参与度不高", "required": False},
            {"key": "requirements", "label": "关注重点", "placeholder": "如：重点关注后进生转化", "required": False},
        ],
        "system": (
            "你是一位善于用数据说话的教学管理者，能从成绩和现象中定位真实问题，"
            "并给出可落地的教学改进措施。你的分析客观、有层次，不空谈。\n" + _NO_FABRICATE
        ),
        "prompt": (
            "请基于参考资料，做一份**学情分析报告**。\n\n"
            "课程：$course\n班级：$class_name\n考核：$exam\n"
            "班级情况描述：$situation\n关注重点：$requirements\n\n"
            "注意：如果参考资料里包含成绩表，请据此分析；"
            "若参考资料中出现「已由程序精确计算的统计结果」，"
            "**必须直接引用其中的平均分、及格率、分数段人数等数字，不要自行重算或估算**。"
            "如果只有文字描述，就基于描述做定性分析，**不要虚构具体分数**。\n\n"
            "报告需包含这些章节：总体情况、成绩数据分析（如有数据）、"
            "存在的主要问题、问题成因分析、分层教学对策、后续教学改进措施、需要重点关注的学生群体。\n\n"
            + _DOCX_SCHEMA
            + "\n\n【参考资料】\n$context"
        ),
    },
}


def list_tasks() -> list:
    """给前端用的任务列表（含字段定义）。"""
    out = []
    for t in TASKS.values():
        out.append({
            "id": t["id"],
            "name": t["name"],
            "desc": t["desc"],
            "output": t["output"],
            "icon": t["icon"],
            "fields": t["fields"],
        })
    return out


def get_task(task_id: str) -> dict:
    if task_id not in TASKS:
        raise KeyError(f"未知任务类型: {task_id}")
    return TASKS[task_id]


def build_messages(task_id: str, params: dict, context: str) -> list:
    """构造发给 LLM 的 messages。

    注意：这里刻意用 `string.Template`（`$field` 占位符）而不是 `str.format`。
    因为提示词里内嵌了 JSON 示例（含 `{"title": "..."}` 这样的花括号），
    用 `.format()` 会把 JSON 的键误当成占位符，直接抛 KeyError。
    """
    import string

    task = get_task(task_id)
    # 用空串填充未提供的可选参数，避免模板里留下 $xxx 占位符
    safe = {f["key"]: (params.get(f["key"]) or "").strip() for f in task["fields"]}
    for f in task["fields"]:
        if not safe.get(f["key"]):
            safe[f["key"]] = f.get("default", "") or "未指定"

    user = string.Template(task["prompt"]).safe_substitute(
        context=context or "（未提供参考资料）", **safe
    )
    return [
        {"role": "system", "content": task["system"]},
        {"role": "user", "content": user},
    ]
