"""文档加载与切分。支持 .md / .txt / .pdf。"""
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


def load_documents(data_dir: str):
    """返回 [(文件路径, 文本), ...]。"""
    docs = []
    for path in Path(data_dir).rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in (".md", ".txt"):
            try:
                text = path.read_text(encoding="utf-8")
                docs.append((str(path), text))
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 读取失败 {path}: {e}")
        elif suffix == ".pdf":
            text = _load_pdf(path)
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
