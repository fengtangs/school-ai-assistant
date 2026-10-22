"""可复用的学生答疑智能体（Agent）。

设计目标：把“基于自有文档的 RAG 问答”封装成一个**参数化、可移植**的单元。
只要给它一个知识库目录 + 一段系统提示词，它就能基于这些资料回答学生问题。

复用方式（复制到别的 Python 项目）：
  1) 把本目录 `agentkit/` 以及 `core/`（引擎：embeddings/vector_store/document_loader/llm）一起复制过去；
  2) 准备你的知识库目录（放 .md/.txt/.pdf），并在 agents.json 里登记；
  3) 配置环境变量（LLM_PROVIDER + 对应后端的 CUSTOM_* / OLLAMA_* / DEEPSEEK_*，
     以及 EMBEDDING_PROVIDER 等，参见 .env.example）；
  4) from agentkit import get_agent; a = get_agent("你的agent_id"); print(a.ask("问题"))

本类只依赖 core 引擎与标准库，不直接依赖项目专属的 config.py，方便移植。
"""
import os
import sys

# 让 agentkit 能导入同项目的 core 引擎（复制到其他项目时确保 core 也在路径内）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 加载 .env（若存在），保证 DEEPSEEK_API_KEY 等环境变量可用
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

from core.embeddings import get_embedding_provider          # noqa: E402
from core.vector_store import VectorStore                  # noqa: E402
from core.document_loader import load_documents, build_chunks  # noqa: E402
from core.llm import get_llm                               # noqa: E402


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


class Agent:
    """一个基于 RAG 的答疑智能体。"""

    def __init__(
        self,
        agent_id: str,
        name: str,
        knowledge_dir: str,
        system_prompt: str,
        icon: str = "🤖",
        index_dir: str = None,
        school_name: str = "",
        use_rag: bool = True,
    ):
        self.agent_id = agent_id
        self.name = name
        self.knowledge_dir = knowledge_dir
        self.system_prompt = system_prompt
        self.icon = icon
        self.school_name = school_name
        self.use_rag = use_rag

        # 大模型：按 LLM_PROVIDER 自动选择（默认 Ollama 本地模型，无需密钥）
        self.llm = get_llm()

        # —— 知识库 / 向量检索：仅 RAG 模式需要 ——
        if use_rag:
            provider = _env("EMBEDDING_PROVIDER", "mock")
            model = _env("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
            self.embedding_provider = get_embedding_provider(provider, model)
            base = index_dir or os.path.join(_env("CHROMA_PATH", "data/processed"), "agents", agent_id)
            self.store = VectorStore(base, self.embedding_provider)
            self.store.load_or_init()
        else:
            # 无限制模式（如“原始 DeepSeek”）：不建索引、不检索，直接对话
            self.embedding_provider = None
            self.store = None

        self._chunk_size = int(_env("CHUNK_SIZE", "500"))
        self._chunk_overlap = int(_env("CHUNK_OVERLAP", "100"))
        self._top_k = int(_env("TOP_K", "4"))

    # ————————————————————————————————————————————————
    def build_index(self) -> int:
        """重建本智能体的知识库索引（覆盖式，幂等）。"""
        if not self.use_rag or self.store is None:
            # 无 RAG 模式没有知识库可建
            return 0
        self.store.reset()
        docs = load_documents(self.knowledge_dir)
        if not docs:
            return 0
        chunks, metadatas, ids = build_chunks(docs, self._chunk_size, self._chunk_overlap)
        if not chunks:
            return 0
        self.store.add_documents(chunks, metadatas, ids)
        return len(chunks)

    def ask(self, question: str) -> dict:
        sources = []
        retrieved = []
        if self.use_rag and self.store is not None:
            retrieved = self.store.similarity_search(question, self._top_k)
            context = "\n\n".join(f"[来源: {m['source']}]\n{t}" for t, m in retrieved)
            for _, m in retrieved:
                if m["source"] not in sources:
                    sources.append(m["source"])
        else:
            # 无 RAG：不检索，直接以空上下文对话
            context = ""
        if self.llm:
            answer = self.llm.generate(self.system_prompt, context, question)
        else:
            answer = self._fallback(question, retrieved)
        return {
            "answer": answer,
            "sources": sources,
            "llm_ready": self.llm is not None,
            "agent_id": self.agent_id,
            "agent_name": self.name,
        }

    def ask_stream(self, question: str):
        """流式问答：返回一个生成器，依次产出事件字典：

          {"type": "meta",     "sources": [...], "agent_id", "agent_name", "llm_ready"}
          {"type": "thinking", "text": "..."}     # 多次，模型思考过程增量
          {"type": "delta",    "text": "..."}     # 多次，最终回答增量
          {"type": "done",     "answer": "完整回答", "thinking": "完整思考"}  # 结束

        前端可用 Server-Sent Events 逐字渲染，思考过程与回答均实时显示。
        """
        sources = []
        retrieved = []
        if self.use_rag and self.store is not None:
            retrieved = self.store.similarity_search(question, self._top_k)
            context = "\n\n".join(f"[来源: {m['source']}]\n{t}" for t, m in retrieved)
            for _, m in retrieved:
                if m["source"] not in sources:
                    sources.append(m["source"])
        else:
            # 无 RAG：不检索，直接以空上下文对话
            context = ""

        yield {
            "type": "meta",
            "sources": sources,
            "llm_ready": self.llm is not None,
            "agent_id": self.agent_id,
            "agent_name": self.name,
        }

        if self.llm:
            full = []
            full_thinking = []
            for kind, piece in self.llm.stream_generate(self.system_prompt, context, question):
                if kind == "thinking":
                    full_thinking.append(piece)
                    yield {"type": "thinking", "text": piece}
                else:
                    full.append(piece)
                    yield {"type": "delta", "text": piece}
            yield {
                "type": "done",
                "answer": "".join(full),
                "thinking": "".join(full_thinking),
            }
        else:
            text = self._fallback(question, retrieved)
            yield {"type": "delta", "text": text}
            yield {"type": "done", "answer": text}

    def _fallback(self, question: str, retrieved) -> str:
        hint = (
            "请在 .env 中配置大模型后端（三选一）：\n"
            "  · LLM_PROVIDER=custom   并填 CUSTOM_API_KEY（推荐，OpenAI 兼容网关）\n"
            "  · LLM_PROVIDER=ollama   并确认 Ollama 服务可达（默认 http://192.168.1.103:11434）\n"
            "  · LLM_PROVIDER=deepseek 并填 DEEPSEEK_API_KEY\n"
            "详见 .env.example。"
        )
        if not retrieved:
            return (
                "（尚未配置可用的大模型（LLM），无法生成智能回答，且知识库为空。\n"
                f"{hint}）"
            )
        snippets = "\n".join(f"· {t[:180]}…" for t, _ in retrieved)
        return (
            "（尚未配置可用的大模型（LLM），无法生成智能回答，"
            f"仅展示检索到的相关资料。\n{hint}）\n\n"
            f"你的问题：{question}\n\n相关资料片段：\n{snippets}"
        )

    def info(self) -> dict:
        return {
            "id": self.agent_id,
            "name": self.name,
            "icon": self.icon,
            "knowledge_dir": self.knowledge_dir,
            "indexed_chunks": self.store.count() if self.store else 0,
            "llm_ready": self.llm is not None,
            "rag": self.use_rag,
        }
