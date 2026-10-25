"""轻量持久化向量库（基于 numpy 余弦相似度）。

相比 chromadb 更轻、无额外重型依赖，对校园级文档量（数千片段）完全够用。
索引以 JSON 形式持久化到磁盘，重启后自动加载。

索引里记录了向量维度与 embedding 模型名。切换 EMBEDDING_PROVIDER 后维度会变
（mock=512 / custom=4096），此时旧索引无法与新查询向量对齐，会抛出
StaleIndexError 提示重建索引，而不是让 numpy 报晦涩的形状错误。
"""
import json
import os

import numpy as np


class StaleIndexError(RuntimeError):
    """索引与当前 embedding 配置不匹配（需重建索引）。"""


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化，保证点积等价于余弦相似度。

    远程 embedding 网关未必保证输出已归一化，这里统一兜一层；
    对已归一化的向量再归一化是幂等的，不影响 mock/hf 的原有行为。
    """
    if mat.size == 0:
        return mat
    norms = np.linalg.norm(mat, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class VectorStore:
    def __init__(self, path: str, embedding_provider):
        self.path = path
        self.embedding_provider = embedding_provider
        self.index_file = os.path.join(path, "index.json")
        self.documents = []
        self.metadatas = []
        self.embeddings = None  # np.ndarray
        # 索引元信息，用于检测 provider/维度漂移
        self.meta = {}

    @property
    def dim(self):
        return None if self.embeddings is None else self.embeddings.shape[1]

    def is_stale(self) -> bool:
        """索引是否与当前 embedding 配置不匹配（换过 provider/模型/维度）。

        只做静态判断，不调用远程接口：优先比 provider 名与模型名（custom 的维度
        要真跑一次才知道，只能靠模型名区分）；mock/hf 这类本地实现还带 dim，
        可以再比一次维度，多一层保险。

        用途：启动/取用前先发现漂移，直接重建，而不是等用户点生成时才抛
        StaleIndexError（那个报错对老师毫无意义）。
        """
        if self.embeddings is None or len(self.embeddings) == 0:
            return False
        if not self.meta:
            return True   # 老索引没写元信息，无法确认来源，按需要重建处理

        old_name = self.meta.get("embedding_provider") or ""
        new_name = getattr(self.embedding_provider, "name", "") or ""
        if old_name and new_name and old_name != new_name:
            return True

        old_model = self.meta.get("embedding_model") or ""
        new_model = getattr(self.embedding_provider, "model", "") or ""
        if old_model and new_model and old_model != new_model:
            return True

        cur_dim = getattr(self.embedding_provider, "dim", None)
        if isinstance(cur_dim, int) and cur_dim > 0 and self.embeddings.shape[1] != cur_dim:
            return True
        return False

    def load_or_init(self):
        if os.path.exists(self.index_file):
            self._load()

    def reset(self):
        """清空内存中的索引（重建前调用，保证幂等、不累积重复）。"""
        self.documents = []
        self.metadatas = []
        self.embeddings = None
        self.meta = {}

    def _load(self):
        with open(self.index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.documents = data["documents"]
        self.metadatas = data["metadatas"]
        embs = data.get("embeddings") or []
        self.embeddings = (
            np.array(embs, dtype=np.float32) if embs else np.zeros((0, 0), dtype=np.float32)
        )
        self.meta = data.get("meta") or {}

    def _save(self):
        os.makedirs(self.path, exist_ok=True)
        self.meta = {
            "embedding_provider": getattr(self.embedding_provider, "name", "unknown"),
            "embedding_model": getattr(self.embedding_provider, "model", ""),
            "dim": self.dim,
            "count": len(self.documents),
        }
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "documents": self.documents,
                    "metadatas": self.metadatas,
                    "embeddings": self.embeddings.tolist(),
                    "meta": self.meta,
                },
                f,
                ensure_ascii=False,
            )

    def add_documents(self, docs, metadatas, ids=None):
        embs = np.array(
            self.embedding_provider.embed_documents(docs), dtype=np.float32
        )
        embs = _l2_normalize(embs)
        if self.embeddings is not None and len(self.documents) > 0:
            if embs.shape[1] != self.embeddings.shape[1]:
                raise StaleIndexError(
                    f"新向量维度 {embs.shape[1]} 与已有索引维度 "
                    f"{self.embeddings.shape[1]} 不一致，请先重建索引。"
                )
            self.documents.extend(docs)
            self.metadatas.extend(metadatas)
            self.embeddings = np.vstack([self.embeddings, embs])
        else:
            self.documents = docs
            self.metadatas = metadatas
            self.embeddings = embs
        self._save()

    def similarity_search(self, query: str, top_k: int = 4):
        if self.embeddings is None or len(self.documents) == 0:
            return []
        q = np.array(self.embedding_provider.embed_query(query), dtype=np.float32)
        q = _l2_normalize(q.reshape(1, -1)).reshape(-1)

        # 维度校验：切换 embedding 后旧索引必须重建
        if q.shape[0] != self.embeddings.shape[1]:
            old_model = self.meta.get("embedding_model") or "未知"
            new_model = getattr(self.embedding_provider, "model", "未知")
            raise StaleIndexError(
                f"索引向量维度 {self.embeddings.shape[1]}（{old_model}）与当前 "
                f"embedding 维度 {q.shape[0]}（{new_model}）不一致。\n"
                f"请重建索引：python scripts/build_index.py"
            )

        sims = self.embeddings @ q  # 双方都已 L2 归一化，点积即余弦相似度
        k = min(top_k, len(self.documents))
        idx = np.argsort(-sims)[:k]
        return [(self.documents[i], self.metadatas[i]) for i in idx]

    def count(self) -> int:
        return len(self.documents)
