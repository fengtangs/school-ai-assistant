"""文档加载与切分。支持 .md / .txt / .pdf / .csv / .xlsx。

表格类文件（csv/xlsx）会被转换成 Markdown 表格文本再入库，
这样成绩表、名单等结构化资料也能被检索和引用。
"""
import os
from pathlib import Path


def _load_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 无法解析 PDF {path}: {e}")
        return ""


def _rows_to_markdown(rows, max_rows: int = 500) -> str:
    """把二维表格转成 Markdown 表格文本（跳过全空行，限制最大行数）。"""
    clean = [r for r in rows if any(str(c).strip() for c in (r or []))]
    if not clean:
        return ""
    header = [str(c).strip() for c in clean[0]]
    ncol = max(1, len(header))
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * ncol) + "|"]
    for r in clean[1:max_rows]:
        cells = [str(c).strip() for c in (r or [])]
        cells = (cells + [""] * ncol)[:ncol]
        lines.append("| " + " | ".join(cells) + " |")
    if len(clean) - 1 > max_rows:
        lines.append(f"\n（表格共 {len(clean) - 1} 行，此处仅展示前 {max_rows} 行）")
    return "\n".join(lines)


def _load_csv(path: Path) -> str:
    """CSV 读取：兼容 UTF-8(BOM) / UTF-8 / GBK（中文 Windows 导出的表格常为 GBK）。"""
    import csv

    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                rows = list(csv.reader(f))
            return _rows_to_markdown(rows)
        except UnicodeDecodeError:
            continue
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 无法解析 CSV {path}: {e}")
            return ""
    print(f"[warn] CSV 编码无法识别 {path}")
    return ""


def _load_xlsx(path: Path) -> str:
    try:
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            md = _rows_to_markdown(rows)
            if md.strip():
                parts.append(f"## 工作表：{ws.title}\n\n{md}")
        wb.close()
        return "\n\n".join(parts)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 无法解析 XLSX {path}: {e}")
        return ""


def load_file_text(path) -> str:
    """把单个文件解析成纯文本（与 load_documents 用同一套规则）。

    单独抽出来，是为了让「表格类资料需要完整内容」的场景（如学情分析要把
    整张成绩表交给模型算统计）能复用同一套解析逻辑，而不必重复实现。
    """
    p = Path(path)
    if not p.is_file():
        return ""
    suffix = p.suffix.lower()
    if suffix in (".md", ".txt"):
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
            try:
                return p.read_text(encoding=enc)
            except UnicodeDecodeError:
                continue
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 读取失败 {p}: {e}")
                return ""
        return ""
    if suffix == ".pdf":
        return _load_pdf(p)
    if suffix == ".csv":
        return _load_csv(p)
    if suffix in (".xlsx", ".xlsm"):
        return _load_xlsx(p)
    return ""


def load_documents(data_dir: str):
    """返回 [(文件路径, 文本), ...]。"""
    docs = []
    for path in Path(data_dir).rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in (".md", ".txt"):
            for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
                try:
                    text = path.read_text(encoding=enc)
                    docs.append((str(path), text))
                    break
                except UnicodeDecodeError:
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] 读取失败 {path}: {e}")
                    break
        elif suffix == ".pdf":
            text = _load_pdf(path)
            if text.strip():
                docs.append((str(path), text))
        elif suffix == ".csv":
            text = _load_csv(path)
            if text.strip():
                docs.append((str(path), text))
        elif suffix in (".xlsx", ".xlsm"):
            text = _load_xlsx(path)
            if text.strip():
                docs.append((str(path), text))
    return docs


def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 100):
    """按字符长度滑动切分，保留重叠以避免截断语义。"""
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        # 尽量在标点/换行处断开，保持句子完整
        if end < len(text):
            for sep in ["\n", "。", "；", "；", "！", "？", "，"]:
                pos = chunk.rfind(sep)
                if pos > chunk_size * 0.5:
                    chunk = chunk[: pos + 1]
                    break
        chunks.append(chunk.strip())
        if end >= len(text):
            break
        start = start + chunk_size - chunk_overlap
    return [c for c in chunks if c]


def build_chunks(docs, chunk_size: int, chunk_overlap: int):
    """把 [(path, text)] 展开成片段列表，并附带元数据。"""
    all_chunks, metadatas, ids = [], [], []
    for file_path, text in docs:
        name = os.path.basename(file_path)
        for i, c in enumerate(chunk_text(text, chunk_size, chunk_overlap)):
            all_chunks.append(c)
            metadatas.append({"source": name, "path": file_path, "chunk": i})
            ids.append(f"{abs(hash(file_path))}_{i}")
    return all_chunks, metadatas, ids
