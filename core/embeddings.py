"""文本向量化。

提供三种实现，通过环境变量 EMBEDDING_PROVIDER 切换：

- mock  ：基于字符 n-gram 的确定性向量，无需下载模型/无需联网（演示用，检索质量一般）
- custom：调用 OpenAI 兼容的 /embeddings 接口（推荐，语义检索准确）
          默认对接智算服务专区 lucion-qwen3-embedding-8b（4096 维）
- hf    ：基于 sentence-transformers 的本地中文向量模型（需下载权重）

三者实现同一接口，便于在 config 中切换。

⚠️ 换 provider 会改变向量维度（mock=512 / custom=4096 / hf=512），
   **必须重建索引**（python scripts/build_index.py），否则旧索引无法与新查询向量对齐。
   VectorStore 已做维度校验，不匹配时会给出明确提示。
"""
import hashlib
import math
import re


class EmbeddingProvider:
    """向量化接口。子类需实现 embed_documents。"""

    #: 提供给 VectorStore 做维度校验的元信息
    name = "base"
    model = ""

    def embed_documents(self, texts):
        raise NotImplementedError

    def embed_query(self, text):
        return self.embed_documents([text])[0]


class MockEmbedding(EmbeddingProvider):
    """字符级 n-gram + TF-IDF 风格的轻量向量，适合中文检索演示。

    注意：这不是语义检索，只是字符重合度匹配，实际效果有限；
    生产环境请改用 custom（远程 embedding）或 hf（本地模型）。
    """

    name = "mock"
    model = "char-ngram"

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


class CustomEmbedding(EmbeddingProvider):
    """OpenAI 兼容的远程 embedding 接口（推荐）。

    默认对接「山东省综合金融服务平台智算服务专区」的
    lucion-qwen3-embedding-8b（4096 维，已归一化）。

    设计上不写死厂商：只要对方实现 OpenAI 的 /v1/embeddings 协议，
    改 CUSTOM_EMBEDDING_BASE_URL / CUSTOM_EMBEDDING_MODEL 即可复用。
    """

    name = "custom"

    def __init__(self, api_key: str, base_url: str, model: str, batch_size: int = 32):
        if not api_key:
            raise ValueError("缺少 CUSTOM_API_KEY（custom embedding 需要密钥）")
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.batch_size = max(1, batch_size)

    def _embed_batch(self, texts):
        out = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i:i + self.batch_size]
            resp = self.client.embeddings.create(model=self.model, input=chunk)
            # 按 index 排序，避免网关返回乱序导致向量与原文错位
            items = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
            out.extend(d.embedding for d in items)
        return out

    def embed_documents(self, texts):
        texts = list(texts)
        if not texts:
            return []
        vecs = self._embed_batch(texts)
        if len(vecs) != len(texts):
            raise RuntimeError(
                f"embedding 返回数量不匹配：请求 {len(texts)} 条，返回 {len(vecs)} 条"
            )
        return vecs


class HFEmbedding(EmbeddingProvider):
    """sentence-transformers 真实向量模型。首次使用会下载权重。"""

    name = "hf"
    model = "BAAI/bge-small-zh-v1.5"

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.model = SentenceTransformer(model_name)

    def embed_documents(self, texts):
        return self.model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        ).tolist()


def get_embedding_provider(provider: str, model_name: str) -> EmbeddingProvider:
    """按 provider 名构造 embedding 实例。

    custom 后端复用 CUSTOM_API_KEY / CUSTOM_BASE_URL，
    模型名默认 CUSTOM_EMBEDDING_MODEL（留空则用传入的 model_name）。
    """
    import os

    p = (provider or "mock").lower()
    if p in ("custom", "tokensd"):
        api_key = os.getenv("CUSTOM_API_KEY", "")
        base_url = os.getenv(
            "CUSTOM_EMBEDDING_BASE_URL",
            os.getenv("CUSTOM_BASE_URL", "https://www.tokensd.com.cn/v1"),
        )
        model = os.getenv("CUSTOM_EMBEDDING_MODEL", "") or model_name
        if not model or "/" in model:
            # 避免把 hf 的 HF 仓库名（含 /）误传给远程网关
            model = "lucion-qwen3-embedding-8b"
        try:
            batch = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
        except (TypeError, ValueError):
            batch = 32
        return CustomEmbedding(api_key, base_url, model, batch_size=batch)
    if p == "hf":
        return HFEmbedding(model_name)
    return MockEmbedding()
