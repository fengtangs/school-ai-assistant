"""轻量持久化向量库（基于 numpy 余弦相似度）。

相比 chromadb 更轻、无额外重型依赖，对校园级文档量（数千片段）完全够用。
索引以 JSON 形式持久化到磁盘，重启后自动加载。
"""
import json
import os

import numpy as np


class VectorStore:
    def __init__(self, path: str, embedding_provider):
        self.path = path
        self.embedding_provider = embedding_provider
        self.index_file = os.path.join(path, "index.json")
        self.documents = []
        self.metadatas = []
        self.embeddings = None  # np.ndarray

    def load_or_init(self):
        if os.path.exists(self.index_file):
            self._load()

    def reset(self):
        """清空内存中的索引（重建前调用，保证幂等、不累积重复）。"""
        self.documents = []
        self.metadatas = []
        self.embeddings = None

    def _load(self):
        with open(self.index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.documents = data["documents"]
        self.metadatas = data["metadatas"]
        self.embeddings = np.array(data["embeddings"], dtype=np.float32)

    def _save(self):
        os.makedirs(self.path, exist_ok=True)
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "documents": self.documents,
                    "metadatas": self.metadatas,
                    "embeddings": self.embeddings.tolist(),
                },
                f,
                ensure_ascii=False,
            )

    def add_documents(self, docs, metadatas, ids=None):
        embs = np.array(
            self.embedding_provider.embed_documents(docs), dtype=np.float32
        )
        if self.embeddings is not None and len(self.documents) > 0:
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
        sims = self.embeddings @ q  # 已归一化，点积即余弦相似度
        k = min(top_k, len(self.documents))
        idx = np.argsort(-sims)[:k]
        return [(self.documents[i], self.metadatas[i]) for i in idx]

    def count(self) -> int:
        return len(self.documents)
