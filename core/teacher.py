"""教师助手：每位教师独立的知识库。

目录结构：
    data/teachers/{teacher_id}/
        raw/                上传的原始资料（.md/.txt/.pdf/.csv/.xlsx）
        index/index.json    向量索引

设计要点：
- **按教师隔离**：不同教师的知识库互不影响，各自独立建索引。
- **路径安全**：`teacher_id` 与文件名都做净化，防止 `../` 之类的路径穿越。
- **复用统一引擎**：embedding 与 VectorStore 跟全局智能体完全一致，
  所以检索质量和维度校验规则（StaleIndexError）也一并复用。

上传后默认自动重建该教师的索引（资料量通常不大，全量重建保证幂等、
不会因为重复上传而累积重复片段）。
"""
import os
import re
import shutil
from pathlib import Path

from core.document_loader import build_chunks, load_documents, load_file_text
from core.embeddings import get_embedding_provider
from core.vector_store import VectorStore, StaleIndexError

#: 允许上传的资料类型
SUPPORTED_EXTS = {".md", ".txt", ".pdf", ".csv", ".xlsx"}
#: 单文件大小上限（MB）
MAX_FILE_MB = 20

_EMB_CACHE = {}


def teacher_root() -> Path:
    """教师数据根目录（可用 TEACHER_DATA_DIR 覆盖，便于迁移/备份）。"""
    return Path(os.getenv("TEACHER_DATA_DIR", "data/teachers")).resolve()


def safe_teacher_id(tid: str) -> str:
    """净化教师 id：只保留字母/数字/下划线/中文/连字符，杜绝路径穿越。"""
    raw = str(tid or "").strip()
    if not raw:
        raise ValueError("teacher_id 不能为空")
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]", "", raw)
    if not cleaned:
        raise ValueError("teacher_id 含非法字符")
    return cleaned[:64]


def safe_filename(name: str) -> str:
    """净化文件名：去掉目录部分与危险字符，保留扩展名。"""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", base).strip().strip(".")
    if not base:
        raise ValueError("文件名非法")
    return base[:120]


def _get_embedding():
    """按全局配置构造 embedding（带缓存，避免每次上传都新建客户端）。"""
    provider = os.getenv("EMBEDDING_PROVIDER", "mock")
    model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    key = f"{provider}::{model}"
    if key not in _EMB_CACHE:
        _EMB_CACHE[key] = get_embedding_provider(provider, model)
    return _EMB_CACHE[key]


def _read_table_rows(path: Path) -> list:
    """把 csv/xlsx 读成二维字符串数组（原始单元格值）。"""
    suffix = str(path.suffix).lower()
    rows = []
    try:
        if suffix == ".csv":
            import csv

            for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
                try:
                    with open(path, "r", encoding=enc, newline="") as f:
                        rows = [list(r) for r in csv.reader(f)]
                    break
                except UnicodeDecodeError:
                    continue
        elif suffix in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook

            wb = load_workbook(str(path), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            wb.close()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 读取表格失败 {path}: {e}")
        return []
    return [[("" if c is None else str(c)) for c in r] for r in rows if r]


def _table_stats_text(path: Path, max_cols: int = 12) -> str:
    """对表格做基础统计，产出可直接引用的精确数字。

    为什么要程序算：让大模型「读表格心算」平均分/及格率并不可靠——
    实测 20 行成绩表也会出现平均分、及格率偏差。这里用 Python 算准，
    模型只负责解读与提建议。
    """
    rows = _read_table_rows(path)
    if len(rows) < 2:
        return ""
    header = [str(h).strip() or f"列{i + 1}" for i, h in enumerate(rows[0])]
    body = rows[1:]
    out = [f"表格《{path.name}》共 {len(body)} 行数据，列名：{'、'.join(header[:max_cols])}"]
    # 学号/编号/序号这类标识列没有统计意义，跳过（否则会算出「平均学号」这种废数据）
    id_like = re.compile(r"(学号|编号|序号|工号|考号|身份证|^id$|_id$)", re.I)
    for ci, name in enumerate(header[:max_cols]):
        if id_like.search(name):
            continue
        vals = []
        for r in body:
            if ci >= len(r):
                continue
            s = str(r[ci]).strip().replace("%", "")
            try:
                vals.append(float(s))
            except ValueError:
                continue
        if len(vals) < 2:
            continue
        n = len(vals)
        sv = sorted(vals)
        median = sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2
        out.append(
            f"- 列「{name}」：有效数值 {n} 个，平均 {sum(vals) / n:.2f}，"
            f"最高 {max(vals):.2f}，最低 {min(vals):.2f}，中位数 {median:.2f}"
        )
        # 看起来是百分制成绩时，补及格率与分数段分布
        if min(vals) >= 0 and max(vals) <= 100:
            pass_n = sum(1 for v in vals if v >= 60)
            out.append(f"  及格(≥60) {pass_n} 人，及格率 {100 * pass_n / n:.1f}%")
            bands = [(90, 101, "90分以上"), (80, 90, "80-89分"),
                     (70, 80, "70-79分"), (60, 70, "60-69分"), (-1, 60, "60分以下")]
            seg = []
            for lo, hi, lab in bands:
                c = sum(1 for v in vals if lo <= v < hi)
                if c:
                    seg.append(f"{lab} {c}人")
            if seg:
                out.append("  分数段：" + "，".join(seg))
    return "\n".join(out)


class TeacherStore:
    """某位教师的知识库。所有方法都限定在该教师自己的目录内。"""

    def __init__(self, teacher_id: str):
        self.teacher_id = safe_teacher_id(teacher_id)
        self.base = teacher_root() / self.teacher_id
        self.raw_dir = self.base / "raw"
        self.index_dir = self.base / "index"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.embedding_provider = _get_embedding()
        self.store = VectorStore(str(self.index_dir), self.embedding_provider)
        self.store.load_or_init()

        self._chunk_size = int(os.getenv("CHUNK_SIZE", "500"))
        self._chunk_overlap = int(os.getenv("CHUNK_OVERLAP", "100"))

        # 换过 embedding（例如 mock → 远程真向量模型）时，旧索引维度与新向量对不上。
        # 原始资料都还在 raw/ 里，直接按新维度重算即可 —— 不能让老师看到
        # 「请执行 python scripts/build_index.py」这种面向开发者的报错。
        # 注意：raw 为空时不重建，否则会把旧索引里的文本一起清掉（那是唯一副本）。
        if self.store.count() > 0 and self.store.is_stale() and self.list_files():
            try:
                n = self.rebuild()
                print(f"[teacher] {self.teacher_id} 索引与当前 embedding 不一致，已自动重建 {n} 个片段")
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 教师 {self.teacher_id} 索引自动重建失败：{e}")

    # —————————————————— 资料管理 ——————————————————
    def add_file(self, filename: str, data: bytes) -> dict:
        """保存一个上传文件到 raw/ 并重建索引。返回该文件的信息。"""
        name = safe_filename(filename)
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise ValueError(
                f"不支持的文件类型 {ext or '(无扩展名)'}，"
                f"仅支持：{'、'.join(sorted(SUPPORTED_EXTS))}"
            )
        size_mb = len(data) / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            raise ValueError(f"文件过大（{size_mb:.1f}MB），上限 {MAX_FILE_MB}MB")

        target = self.raw_dir / name
        # 重名则覆盖（教师重复上传同一份资料的意图通常是更新）
        target.write_bytes(data)
        return {
            "filename": name,
            "size_kb": round(len(data) / 1024, 1),
            "ext": ext,
        }

    def list_files(self) -> list:
        """列出已上传资料（按修改时间倒序）。"""
        out = []
        for p in sorted(self.raw_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file() or p.name.startswith("."):
                continue
            st = p.stat()
            out.append({
                "filename": p.name,
                "size_kb": round(st.st_size / 1024, 1),
                "ext": p.suffix.lower(),
                "mtime": int(st.st_mtime),
            })
        return out

    def delete_file(self, filename: str) -> bool:
        """删除一份资料（限定在本教师 raw 目录内）。"""
        name = safe_filename(filename)
        target = (self.raw_dir / name).resolve()
        # 双保险：确认最终路径确实在该教师的 raw 目录下
        if self.raw_dir.resolve() not in target.parents:
            raise ValueError("非法路径")
        if target.is_file():
            target.unlink()
            return True
        return False

    def clear_all(self) -> None:
        """清空该教师的全部资料与索引。"""
        if self.raw_dir.exists():
            shutil.rmtree(self.raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.store.reset()
        idx = self.index_dir / "index.json"
        if idx.exists():
            idx.unlink()

    # —————————————————— 索引 ——————————————————
    def rebuild(self) -> int:
        """全量重建索引，返回片段数。"""
        self.store.reset()
        docs = load_documents(str(self.raw_dir))
        if not docs:
            return 0
        chunks, metadatas, ids = build_chunks(docs, self._chunk_size, self._chunk_overlap)
        if not chunks:
            return 0
        self.store.add_documents(chunks, metadatas, ids)
        return len(chunks)

    def add_and_rebuild(self, filename: str, data: bytes) -> dict:
        """上传 + 重建，一步到位。返回 {file, indexed_chunks}。"""
        info = self.add_file(filename, data)
        info["indexed_chunks"] = self.rebuild()
        return info

    # —————————————————— 检索 ——————————————————
    def search(self, query: str, top_k: int = 6) -> list:
        if not query or self.store.count() == 0:
            return []
        try:
            return self.store.similarity_search(query, top_k)
        except StaleIndexError:
            # 兜底：__init__ 的预检可能因为并发/异常没生效，这里重建一次再试。
            # raw 为空则不做重建（避免清掉旧索引里唯一的文本副本），
            # 直接当作"没有可检索资料"，让生成流程继续 —— 少参考资料，
            # 总好过整个课件生成失败。
            if not self.list_files():
                print(f"[warn] 教师 {self.teacher_id} 索引与当前 embedding 不一致且无原始资料，已跳过检索")
                return []
            n = self.rebuild()
            if n == 0:
                return []
            return self.store.similarity_search(query, top_k)

    def build_context(self, query: str, top_k: int = 6, max_chars: int = 6000,
                      table_chars: int = 5000) -> tuple:
        """检索并拼装成供 LLM 使用的上下文。返回 (context_text, sources)。

        特别处理：如果命中来源是表格类资料（csv/xlsx），会额外把**完整表格**
        追加进上下文。否则一张成绩表被切成多个片段后，模型只看到部分行，
        算出来的平均分/及格率就会和真实值有偏差（实测踩过这个坑）。
        """
        hits = self.search(query, top_k)
        if not hits:
            return "", []
        sources, parts, total = [], [], 0
        for text, meta in hits:
            src = meta.get("source", "")
            if src and src not in sources:
                sources.append(src)
            piece = f"[来源: {src}]\n{text}"
            if total + len(piece) > max_chars:
                break
            parts.append(piece)
            total += len(piece)

        # 表格类资料：补齐完整表格 + 由程序精确算出的统计
        table_exts = {".csv", ".xlsx", ".xlsm"}
        for name in list(sources):
            if Path(name).suffix.lower() not in table_exts:
                continue
            p = self.raw_dir / name
            full = load_file_text(p)
            if full.strip():
                piece = f"[完整表格: {name}]\n{full[:table_chars]}"
                if len(full) > table_chars:
                    piece += f"\n（表格较长已截断，原文共 {len(full)} 字）"
                parts.append(piece)
                total += len(piece)
            stats = _table_stats_text(p)
            if stats.strip():
                parts.append(
                    "[已由程序精确计算的统计结果 —— 请直接引用这些数字，不要再自行估算]\n"
                    + stats
                )
                total += len(stats)

        return "\n\n".join(parts), sources

    def stats(self) -> dict:
        n = self.store.count()
        # 索引与当前 embedding 不一致时，这些片段其实检索不到，报真实可用数量更诚实
        usable = 0 if (n > 0 and self.store.is_stale()) else n
        return {
            "teacher_id": self.teacher_id,
            "files": len(self.list_files()),
            "indexed_chunks": usable,
            "stale_index": bool(n > 0 and self.store.is_stale()),
            "embedding_model": getattr(self.embedding_provider, "model", ""),
        }
