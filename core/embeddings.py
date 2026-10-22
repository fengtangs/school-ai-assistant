"""文本向量化。

提供两种实现：
- MockEmbedding：基于字符 n-gram 的确定性向量，无需下载模型，开箱即用（演示用）。
- HFEmbedding ：基于 sentence-transformers 的真实中文向量模型（生产用）。
两者实现同一接口，便于在 config 中切换。
"""
import hashlib
import math
import re


class EmbeddingProvider:
    def embed_documents(self, texts):
        raise NotImplementedError

    def embed_query(self, text):
        return self.embed_documents([text])[0]


class MockEmbedding(EmbeddingProvider):
    """字符级 n-gram + TF-IDF 风格的轻量向量，适合中文检索演示。"""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _vectorize(self, text: str):
        vec = [0.0] * self.dim
        text = re.sub(r"\s+", "", text or "")
        grams = list(text)
        for i in range(len(text) - 1):
            grams.append(text[i:i + 2])
        counts = {}
        for g in grams:
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16) % self.dim
            counts[h] = counts.get(h, 0) + 1
        norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
        for h, c in counts.items():
            vec[h] = c / norm
        return vec

    def embed_documents(self, texts):
        return [self._vectorize(t) for t in texts]


class HFEmbedding(EmbeddingProvider):
    """sentence-transformers 真实向量模型。首次使用会下载权重。"""

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)

    def embed_documents(self, texts):
        return self.model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        ).tolist()


def get_embedding_provider(provider: str, model_name: str) -> EmbeddingProvider:
    if provider == "hf":
        return HFEmbedding(model_name)
    return MockEmbedding()
