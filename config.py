"""全局配置：从 .env 读取，带合理默认值。"""
import os
from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    # 大模型后端：ollama（本地）/ deepseek（官方 API）/ custom（自定义 OpenAI 兼容网关）
    llm_provider: str = os.getenv("LLM_PROVIDER", "ollama")
    llm_max_tokens: int = _get_int("LLM_MAX_TOKENS", 2048)

    # Ollama（本地/局域网部署，OpenAI 兼容接口，无需密钥）
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://192.168.1.103:11434/v1")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "deepseek-r1:14b")

    # DeepSeek（密钥只从 .env 读取，源码中不留存明文；仅当 LLM_PROVIDER=deepseek 时使用）
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # 自定义 OpenAI 兼容网关（仅当 LLM_PROVIDER=custom 时使用）
    # 默认对接「山东省综合金融服务平台智算服务专区」
    custom_api_key: str = os.getenv("CUSTOM_API_KEY", "")
    custom_base_url: str = os.getenv("CUSTOM_BASE_URL", "https://www.tokensd.com.cn/v1")
    custom_model: str = os.getenv("CUSTOM_MODEL", "deepseek-v4-flash")

    # 嵌入
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "mock")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

    # 学校
    school_name: str = os.getenv("SCHOOL_NAME", "德州工程职业学院")

    # 检索参数
    chunk_size: int = _get_int("CHUNK_SIZE", 500)
    chunk_overlap: int = _get_int("CHUNK_OVERLAP", 100)
    top_k: int = _get_int("TOP_K", 4)

    # 路径
    chroma_path: str = os.getenv("CHROMA_PATH", "data/processed")
    data_dir: str = os.getenv("DATA_DIR", "data/raw")


config = Config()
