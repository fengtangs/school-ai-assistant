"""教师助手的文档生成：把 LLM 产出的 JSON 渲染成 .docx / .pptx。

设计说明：
- LLM 只负责「内容」，排版权式由这里用 python-docx / python-pptx 控制，
  这样生成的文件格式稳定，不会因为模型措辞变化而崩掉。
- 中文字体需单独设置 w:eastAsia / a:ea，否则 Word/PPT 里中文会回退成默认字体。
- `parse_json_block` 做了多重容错，因为模型偶尔会用 ```json 包裹或加解释文字。
"""
import json
import re
from pathlib import Path


# ————————————————————————————————————————————————
# JSON 解析容错
# ————————————————————————————————————————————————
def parse_json_block(text: str) -> dict:
    """从模型输出中尽量提取出 JSON 对象。

    依次尝试：直接解析 → 去掉 ```json 包裹 → 截取首尾大括号 → 括号配平扫描。
    全部失败则抛 ValueError（由调用方决定降级方式）。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型未返回内容")

    # 1) 直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2) 去掉 markdown 代码块标记
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    if fenced != raw:
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass

    # 3) 截取第一个 { 到最后一个 }
    i, j = fenced.find("{"), fenced.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(fenced[i:j + 1])
        except json.JSONDecodeError:
            pass

    # 4) 括号配平扫描（应对尾部有多余文字）
    start = fenced.find("{")
    if start != -1:
        depth, in_str, esc = 0, False, False
        for k in range(start, len(fenced)):
            ch = fenced[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(fenced[start:k + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError("模型返回的内容不是有效 JSON，无法生成文件")


def _as_list(v) -> list:
    """把可能是 str / list / None 的字段统一成 list[str]。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[\n;；]+", v) if s.strip()]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            if isinstance(x, dict):
                # 模型有时把列表项写成 {"环节": "...", "内容": "..."}
                out.append("：".join(str(val) for val in x.values() if val))
            else:
                s = str(x).strip()
                if s:
                    out.append(s)
        return out
    return [str(v)]


# ————————————————————————————————————————————————
# DOCX（备课稿 / 教案 / 学情分析共用）
# ————————————————————————————————————————————————
def _docx_set_font(run, name="宋体", size=None, bold=None, color=None):
    """设置中英文字体（中文必须额外写 w:eastAsia）。"""
    from docx.oxml.ns import qn

    run.font.name = name
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        from docx.oxml import OxmlElement

        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), name)
    rFonts.set(qn("w:ascii"), name)
    rFonts.set(qn("w:hAnsi"), name)
    if size is not None:
        run.font.size = size
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def build_docx(spec: dict, out_path: str, doc_kind: str = "备课稿") -> str:
    """把统一结构的 JSON 渲染成 .docx。返回文件路径。"""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor, Cm

    doc = Document()

    # 页面与默认样式
    sec = doc.sections[0]
    sec.top_margin = Cm(2.2)
    sec.bottom_margin = Cm(2.2)
    sec.left_margin = Cm(2.6)
    sec.right_margin = Cm(2.6)

    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(10.5)
    # 让正文默认也带上东亚字体
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    rPr = normal.element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), "宋体")

    # —— 标题 ——
    title = str(spec.get("title") or f"{doc_kind}")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(6)
    _docx_set_font(p.add_run(title), "黑体", Pt(18), True, RGBColor(0x1F, 0x38, 0x64))

    # 副标题（文档类型）
    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p2.paragraph_format.space_after = Pt(12)
    _docx_set_font(p2.add_run(doc_kind), "楷体", Pt(11), False, RGBColor(0x59, 0x59, 0x59))

    # —— meta 信息栏（2 列表格） ——
    meta = spec.get("meta")
    if isinstance(meta, dict) and meta:
        items = [(str(k), str(v)) for k, v in meta.items() if str(v).strip()]
        if items:
            rows = (len(items) + 1) // 2
            table = doc.add_table(rows=rows, cols=4)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for idx, (k, v) in enumerate(items):
                r, c = idx // 2, (idx % 2) * 2
                cell_k = table.cell(r, c)
                cell_v = table.cell(r, c + 1)
                cell_k.text = ""
                cell_v.text = ""
                _docx_set_font(cell_k.paragraphs[0].add_run(k), "黑体", Pt(9.5), True)
                _docx_set_font(cell_v.paragraphs[0].add_run(v), "宋体", Pt(9.5))
            doc.add_paragraph()

    # —— 正文章节 ——
    sections = spec.get("sections") or []
    if not isinstance(sections, list):
        sections = []
    for n, s in enumerate(sections, 1):
        if not isinstance(s, dict):
            continue
        heading = str(s.get("heading") or f"第{n}部分").strip()
        hp = doc.add_paragraph()
        hp.paragraph_format.space_before = Pt(10)
        hp.paragraph_format.space_after = Pt(4)
        _docx_set_font(hp.add_run(f"{n}. {heading}"), "黑体", Pt(13), True, RGBColor(0x1F, 0x38, 0x64))

        body = str(s.get("body") or "").strip()
        for para in [t.strip() for t in body.split("\n") if t.strip()]:
            bp = doc.add_paragraph()
            bp.paragraph_format.first_line_indent = Pt(21)  # 首行缩进两字符
            bp.paragraph_format.line_spacing = 1.4
            bp.paragraph_format.space_after = Pt(3)
            _docx_set_font(bp.add_run(para), "宋体", Pt(10.5))

        for it in _as_list(s.get("items")):
            ip = doc.add_paragraph(style="List Bullet")
            ip.paragraph_format.line_spacing = 1.4
            ip.paragraph_format.space_after = Pt(2)
            _docx_set_font(ip.add_run(it), "宋体", Pt(10.5))

    # —— 页脚提示 ——
    doc.add_paragraph()
    foot = doc.add_paragraph()
    foot.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _docx_set_font(
        foot.add_run("本文件由校园 AI 助手根据教师上传资料生成，请教师复核后使用。"),
        "楷体", Pt(8.5), False, RGBColor(0x8C, 0x8C, 0x8C),
    )

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return out_path


# ————————————————————————————————————————————————
# PPTX（课件）
# ————————————————————————————————————————————————


def _set_ea(run, name="微软雅黑"):
    """给 pptx 的 run 同时设置拉丁与东亚字体，保证中文不回退。"""
    from pptx.oxml.ns import qn
    from pptx.oxml.xmlchemy import OxmlElement

    run.font.name = name
    rPr = run._r.get_or_add_rPr()
    # 移除旧的 ea，避免重复节点
    for old in rPr.findall(qn("a:ea")):
        rPr.remove(old)
    ea = OxmlElement("a:ea")
    ea.set("typeface", name)
    rPr.append(ea)


def build_pptx(spec: dict, out_path: str) -> str:
    """把课件 JSON 渲染成 16:9 的 .pptx。

    设计规范参考 WorkBuddy-pptx skill 的「学术风」设计原则：
      · 画布 1280×720 px，三区母版：
          A 标题区 0–120px / B 内容区 120–660px / C 页脚区 660–720px
      · 左右安全边距 60px，上下 20px
      · 色彩面积：主蓝 ≤60%、辅色 ≤30%、强调 ≤10%，中性色补足
      · 字号阶梯：封面主标 72 / 章节大数字 160 / 页标题 34 / 正文 22–26 / 页脚 14
      · 每页至少一个「视觉锚点」（超大数字或大色块），避免所有元素大小趋同

    支持四类版式（由模型输出的 type 决定）：
      toc 目录 / section 章节扉页 / content 要点页 / data 数据页
    封面与结束页由程序自动生成，保证一致性。
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.shapes import MSO_SHAPE

    # —— 画布与单位换算（1280px 宽 = 13.333in，故 96px = 1in） ——
    PXIN = 96.0

    def I(px):
        return Inches(px / PXIN)

    def F(px):
        """px → pt（96dpi 下 1px = 0.75pt）"""
        return Pt(px * 0.75)

    # —— 学术风配色 ——
    MAIN = RGBColor(0x1E, 0x4F, 0xA8)
    DARK = RGBColor(0x0E, 0x3F, 0x8C)
    LIGHT = RGBColor(0xE8, 0xEF, 0xF8)
    LIGHT2 = RGBColor(0xF0, 0xF5, 0xFC)
    MID = RGBColor(0x3D, 0x7B, 0xD9)
    TXT = RGBColor(0x1A, 0x22, 0x30)
    SUB = RGBColor(0x4A, 0x55, 0x68)
    HINT = RGBColor(0x8B, 0x97, 0xA8)
    LINE = RGBColor(0xD6, 0xDC, 0xE5)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    BG = RGBColor(0xF7, 0xF9, 0xFC)

    PAD_X, PAD_Y = 60, 20
    A_H = 120          # A 标题区高度
    C_Y = 660          # C 页脚区起始
    CANVAS_W, CANVAS_H = 1280, 720

    prs = Presentation()
    prs.slide_width = I(CANVAS_W)
    prs.slide_height = I(CANVAS_H)
    blank = prs.slide_layouts[6]

    def rect(s, x, y, w, h, color, shape=MSO_SHAPE.RECTANGLE):
        sp = s.shapes.add_shape(shape, I(x), I(y), I(w), I(h))
        sp.fill.solid()
        sp.fill.fore_color.rgb = color
        sp.line.fill.background()
        sp.shadow.inherit = False
        return sp

    def text(s, x, y, w, h, content, size, color=TXT, bold=False,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.0):
        tb = s.shapes.add_textbox(I(x), I(y), I(w), I(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        tf.margin_left = 0
        tf.margin_right = 0
        tf.margin_top = 0
        tf.margin_bottom = 0
        p = tf.paragraphs[0]
        p.alignment = align
        p.line_spacing = spacing
        r = p.add_run()
        r.text = str(content)
        _set_ea(r)
        r.font.size = F(size)
        r.font.bold = bold
        r.font.color.rgb = color
        return tb

    # —— 元信息 ——
    course = str(spec.get("course") or "").strip()
    title_text = str(spec.get("title") or "教学课件").strip()
    subtitle = str(spec.get("subtitle") or "").strip()
    audience = str(spec.get("audience") or "").strip()

    raw_slides = spec.get("slides") or []
    if not isinstance(raw_slides, list):
        raw_slides = []
    slides = [x for x in raw_slides if isinstance(x, dict)]
    total_pages = len(slides) + 2  # 封面 + 内容 + 结束页
    page_no = 0

    def footer(s):
        """C 区页脚：左侧课程名，右侧页码。"""
        nonlocal page_no
        rect(s, 0, C_Y, CANVAS_W, 1, LINE)
        left = course or title_text
        text(s, PAD_X, C_Y + 18, 800, 24, left, 14, HINT)
        text(s, CANVAS_W - PAD_X - 200, C_Y + 18, 200, 24,
             f"{page_no:02d} / {total_pages:02d}", 14, MID, align=PP_ALIGN.RIGHT)

    def page_header(s, heading):
        """A 区标题：主标题 + 下方装饰线（长度与标题呼应，不做满宽）。"""
        text(s, PAD_X, 38, CANVAS_W - PAD_X * 2 - 120, 56, heading, 34, MAIN, bold=True)
        rect(s, PAD_X, 100, 56, 4, MID)

    # ==================== 封面 ====================
    s = prs.slides.add_slide(blank)
    page_no += 1
    rect(s, 0, 0, CANVAS_W, CANVAS_H, DARK)
    # 右侧装饰大色块（视觉锚点）
    rect(s, CANVAS_W - 300, 0, 300, CANVAS_H, MAIN)
    rect(s, CANVAS_W - 300, 0, 8, CANVAS_H, MID)

    if course:
        text(s, PAD_X + 20, 120, 700, 28, course, 20, RGBColor(0xA8, 0xC4, 0xE8))
    # 装饰短线
    rect(s, PAD_X + 20, 168, 72, 5, MID)

    tb = s.shapes.add_textbox(I(PAD_X + 20), I(210), I(CANVAS_W - 400), I(280))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.line_spacing = 1.15
    r = p.add_run()
    r.text = title_text
    _set_ea(r)
    r.font.size = F(72 if len(title_text) <= 14 else 56)
    r.font.bold = True
    r.font.color.rgb = WHITE

    if subtitle:
        text(s, PAD_X + 20, 458, CANVAS_W - 400, 40, subtitle, 26, RGBColor(0xA8, 0xC4, 0xE8))
    if audience:
        text(s, PAD_X + 20, 512, CANVAS_W - 400, 30, "适用对象：" + audience, 20, RGBColor(0x7F, 0xA0, 0xCC))

    # ==================== 内容页 ====================
    for item in slides:
        s = prs.slides.add_slide(blank)
        page_no += 1
        stype = str(item.get("type") or "content").strip().lower()
        title = str(item.get("title") or "").strip()
        bullets = _as_list(item.get("bullets"))
        note = str(item.get("note") or "").strip()

        # ---------------- 目录页 ----------------
        if stype == "toc":
            rect(s, 0, 0, CANVAS_W, CANVAS_H, WHITE)
            rect(s, 0, 0, 12, CANVAS_H, MAIN)
            page_header(s, title or "目录")
            n = len(bullets)
            if n:
                top = 168
                avail = C_Y - top - 24
                row_h = min(88, max(48, int(avail / max(n, 1))))
                size = 26 if n <= 5 else 22
                for i, b in enumerate(bullets):
                    y = top + i * row_h
                    # 大编号作为视觉节奏
                    text(s, PAD_X, y, 76, row_h - 8, f"{i + 1:02d}", size + 8, MID, bold=True)
                    text(s, PAD_X + 84, y + 4, CANVAS_W - PAD_X * 2 - 84, row_h - 8, b, size, TXT)
                    if i < n - 1:
                        rect(s, PAD_X, y + row_h - 10, CANVAS_W - PAD_X * 2, 1, LINE)
            footer(s)

        # ---------------- 章节扉页 ----------------
        elif stype == "section":
            num = str(item.get("number") or "").strip()
            rect(s, 0, 0, CANVAS_W, CANVAS_H, WHITE)
            # 左侧深蓝块承载超大数字（视觉锚点）
            rect(s, 0, 0, 430, CANVAS_H, DARK)
            if num:
                text(s, 52, 250, 340, 220, num, 160, RGBColor(0x4E, 0x7C, 0xC8), bold=True)
            text(s, 52, 470, 330, 40, "章节", 20, RGBColor(0x7F, 0xA0, 0xCC))

            tx = 500
            tw = CANVAS_W - tx - PAD_X
            text(s, tx, 250, tw, 90, title, 46, MAIN, bold=True)
            rect(s, tx, 352, 72, 5, MID)
            if bullets:
                top = 392
                size = 22 if len(bullets) <= 4 else 20
                for i, b in enumerate(bullets):
                    y = top + i * 42
                    rect(s, tx, y + 11, 7, 7, MID, MSO_SHAPE.OVAL)
                    text(s, tx + 22, y, tw - 22, 32, b, size, SUB)

        # ---------------- 数据页 ----------------
        elif stype == "data":
            rect(s, 0, 0, CANVAS_W, CANVAS_H, BG)
            page_header(s, title)
            stats = item.get("stats") or []
            if not isinstance(stats, list):
                stats = []
            stats = [x for x in stats if isinstance(x, dict)][:4]
            if stats:
                n = len(stats)
                gap = 24
                cw = int((CANVAS_W - PAD_X * 2 - gap * (n - 1)) / n)
                cy, ch = 160, 230
                for i, st in enumerate(stats):
                    cx = PAD_X + i * (cw + gap)
                    rect(s, cx, cy, cw, ch, WHITE)
                    rect(s, cx, cy, cw, 6, MAIN)
                    val = str(st.get("value") or "").strip()
                    lab = str(st.get("label") or "").strip()
                    # KPI 巨数字 = 该页视觉锚点
                    text(s, cx + 24, cy + 52, cw - 48, 96,
                         val, 72 if len(val) <= 5 else 52, MAIN, bold=True)
                    text(s, cx + 24, cy + 158, cw - 48, 56, lab, 20, SUB)
                btop = cy + ch + 40
            else:
                btop = 176
            if bullets:
                size = 24 if len(bullets) <= 4 else 21
                for i, b in enumerate(bullets):
                    y = btop + i * 46
                    if y + 40 > C_Y:
                        break
                    rect(s, PAD_X, y + 13, 8, 8, MID, MSO_SHAPE.OVAL)
                    text(s, PAD_X + 24, y, CANVAS_W - PAD_X * 2 - 24, 40, b, size, TXT)
            footer(s)

        # ---------------- 要点页（默认） ----------------
        else:
            rect(s, 0, 0, CANVAS_W, CANVAS_H, WHITE)
            rect(s, 0, 0, 12, CANVAS_H, MAIN)
            page_header(s, title)
            n = max(1, len(bullets))
            top = 168
            avail = C_Y - top - 20
            row_h = min(96, max(52, int(avail / n)))
            size = 26 if n <= 3 else (23 if n <= 5 else 20)
            for i, b in enumerate(bullets):
                y = top + i * row_h
                if y + 48 > C_Y:
                    break
                # 编号（主色）+ 正文，形成清晰层级
                text(s, PAD_X, y + 2, 62, 40, f"{i + 1:02d}", size + 2, MID, bold=True)
                text(s, PAD_X + 70, y, CANVAS_W - PAD_X * 2 - 70, row_h - 6, b, size, TXT,
                     spacing=1.35)
                if i < len(bullets) - 1 and y + row_h < C_Y - 10:
                    rect(s, PAD_X + 70, y + row_h - 12, CANVAS_W - PAD_X * 2 - 70, 1, LINE)
            footer(s)

        if note:
            s.notes_slide.notes_text_frame.text = note

    # ==================== 结束页 ====================
    s = prs.slides.add_slide(blank)
    page_no += 1
    rect(s, 0, 0, CANVAS_W, CANVAS_H, DARK)
    rect(s, 0, CANVAS_H - 96, CANVAS_W, 96, MAIN)
    text(s, 0, 268, CANVAS_W, 100, "谢谢！", 64, WHITE, bold=True, align=PP_ALIGN.CENTER)
    text(s, 0, 372, CANVAS_W, 40, title_text, 22, RGBColor(0xA8, 0xC4, 0xE8),
         align=PP_ALIGN.CENTER)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    prs.save(out_path)
    return out_path
