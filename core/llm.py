"""大模型调用（OpenAI 兼容接口）。

支持三种后端，通过环境变量 LLM_PROVIDER 切换（默认 ollama）：

  - ollama  ：调用本地/局域网 Ollama 的 OpenAI 兼容接口（无需 API Key）
              默认地址 http://192.168.1.103:11434/v1，默认模型 deepseek-r1:14b
  - deepseek：调用 DeepSeek 官方 API（https://api.deepseek.com），需 DEEPSEEK_API_KEY
  - custom  ：调用任意 OpenAI 兼容的第三方网关，需 CUSTOM_API_KEY
              默认对接「山东省综合金融服务平台智算服务专区」
              （https://www.tokensd.com.cn/v1），默认模型 deepseek-v4-flash

三者协议一致（都是 OpenAI Chat Completions），因此统一用一个封装，
仅 base_url 与密钥不同。custom 后端不写死任何厂商信息，
只要对方兼容 OpenAI 协议，改 CUSTOM_BASE_URL / CUSTOM_MODEL 即可复用。
"""
import os
import json

import httpx
from openai import OpenAI


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _build_user_content(context: str, question: str) -> str:
    """构造用户消息。

    - 有参考上下文（RAG）：套用“参考信息 + 请据此回答”的校园问答模板；
    - 无上下文（如“原始 DeepSeek”这类无限制智能体）：直接返回问题，
      不再塞入“参考信息”等无关引导，保持模型原汁原味。
    """
    ctx = (context or "").strip()
    if not ctx:
        return question
    return (
        f"以下是学校提供的参考信息：\n\n{ctx}\n\n"
        f"请根据参考信息回答用户的问题。\n用户问题：{question}"
    )


def _build_messages(system_prompt: str, user_content: str) -> list:
    """构造 messages 列表；系统提示为空时省略 system 角色，做到“不加任何限制”。"""
    messages = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})
    return messages


class _OpenAILikeLLM:
    """统一封装：DeepSeek 与 Ollama 都是 OpenAI 兼容接口，仅 base_url / 密钥不同。"""

    def __init__(self, base_url: str, api_key: str, model: str, label: str = "llm"):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.label = label

    def generate(self, system_prompt: str, context: str, question: str) -> str:
        max_tokens = int(_env("LLM_MAX_TOKENS", "2048"))
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=_build_messages(system_prompt, _build_user_content(context, question)),
            temperature=0.3,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        if content:
            return content

        # —— 空回答兜底 ——
        # 思考型模型（deepseek-r1 / deepseek-v4-* 等）会先输出 reasoning_content，
        # 若 max_tokens 偏小，额度会被思考过程全部吃掉，导致 content 为空且
        # finish_reason=length。这里给出可读提示，而不是让前端显示一片空白。
        reasoning = getattr(choice.message, "reasoning_content", "") or ""
        if choice.finish_reason == "length" and reasoning:
            return (
                f"（模型思考内容已达上限，正式回答被截断，未能产出结果。\n"
                f"当前 LLM_MAX_TOKENS={max_tokens}，思考过程已占用约 "
                f"{len(reasoning)} 字。请在 .env 中调大 LLM_MAX_TOKENS"
                f"（建议 4096 以上）后重试。）"
            )
        return content

    def stream_generate(self, system_prompt: str, context: str, question: str):
        """流式生成：逐块 yield (kind, text) 二元组。

        kind 取值：
          "thinking" —— 推理/思维链内容（DeepSeek 官方接口走 reasoning_content）
          "content"  —— 最终回答内容

        Ollama 后端由子类重写为原生 /api/chat 接口（OpenAI 兼容接口会剥离
        thinking，拿不到模型的思考过程），同样遵循该 (kind, text) 协议。
        """
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=_build_messages(system_prompt, _build_user_content(context, question)),
            temperature=0.3,
            max_tokens=int(_env("LLM_MAX_TOKENS", "2048")),
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            rc = getattr(delta, "reasoning_content", None)
            ct = getattr(delta, "content", None)
            if rc:
                yield ("thinking", rc)
            if ct:
                yield ("content", ct)


class DeepSeekLLM(_OpenAILikeLLM):
    """DeepSeek 官方 API 后端（需 DEEPSEEK_API_KEY）。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        if not api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY")
        super().__init__(base_url=base_url, api_key=api_key, model=model, label="deepseek")


class OllamaLLM(_OpenAILikeLLM):
    """本地/局域网 Ollama 后端。

    流式生成走 Ollama **原生 /api/chat** 接口：OpenAI 兼容接口会剥离 thinking，
    而原生接口在 message 里额外返回 thinking 字段，可把 deepseek-r1 的
    “思考过程”作为独立事件流出来。非流式 generate 仍走 OpenAI 兼容接口（逻辑不变）。
    """

    def __init__(self, base_url: str, model: str, api_key: str = "ollama"):
        # Ollama 默认不校验密钥，但 OpenAI SDK 需要一个非空值
        super().__init__(base_url=base_url, api_key=api_key, model=model, label="ollama")
        # 原生接口根地址：把 OpenAI 兼容地址（…/v1）回退到根（…）
        b = base_url.rstrip("/")
        self.native_base = b[:-3] if b.endswith("/v1") else b

    def stream_generate(self, system_prompt: str, context: str, question: str):
        """原生 /api/chat 流式：yield (kind, text)，kind 为 "thinking"/"content"。"""
        payload = {
            "model": self.model,
            "stream": True,
            "options": {
                "temperature": 0.3,
                "num_predict": int(_env("LLM_MAX_TOKENS", "2048")),
            },
            "messages": _build_messages(system_prompt, _build_user_content(context, question)),
        }
        # 连接 10s 超时，读取最长 600s（14B 模型思考可能较久）
        timeout = httpx.Timeout(10.0, read=600.0)
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST", self.native_base + "/api/chat", json=payload
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = obj.get("message") or {}
                    thinking = msg.get("thinking")
                    content = msg.get("content")
                    if thinking:
                        yield ("thinking", thinking)
                    if content:
                        yield ("content", content)


class CustomLLM(_OpenAILikeLLM):
    """自定义 OpenAI 兼容网关后端（需 CUSTOM_API_KEY）。

    默认对接「山东省综合金融服务平台智算服务专区」(tokensd.com.cn)，
    但故意不写死厂商逻辑：只要求对方兼容 OpenAI 的 /chat/completions，
    换 base_url + model 就能迁到别的网关（阿里百炼 / 硅基流动 / 火山方舟 等）。

    该网关的思考型模型（如 deepseek-v4-flash）会在响应里额外返回
    reasoning_content 字段，已由基类 stream_generate 识别并作为
    "thinking" 事件流式输出，前端可实时显示思考过程。
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        if not api_key:
            raise ValueError("缺少 CUSTOM_API_KEY")
        super().__init__(base_url=base_url, api_key=api_key, model=model, label="custom")

    def list_models(self) -> list:
        """拉取该网关支持的模型 id 列表（用于调试 / 换模型）。"""
        try:
            resp = self.client.models.list()
            return [m.id for m in resp.data]
        except Exception:  # noqa: BLE001  部分网关未实现 /models
            return []


def list_custom_models() -> list:
    """独立函数：不实例化智能体也能查自定义网关的可用模型。

    用法：python -c "from core.llm import list_custom_models; print(list_custom_models())"
    """
    api_key = _env("CUSTOM_API_KEY", "")
    if not api_key:
        return []
    try:
        llm = CustomLLM(
            api_key,
            _env("CUSTOM_BASE_URL", "https://www.tokensd.com.cn/v1"),
            _env("CUSTOM_MODEL", "deepseek-v4-flash"),
        )
        return llm.list_models()
    except Exception:  # noqa: BLE001
        return []


def get_llm():
    """按 LLM_PROVIDER 返回对应 LLM 实例；未配置可用后端则返回 None。

    默认使用 Ollama 本地模型。设置 LLM_PROVIDER=deepseek 时回退到官方 API
    （需同时提供 DEEPSEEK_API_KEY，否则同样返回 None）。
    LLM_PROVIDER=custom 时使用自定义 OpenAI 兼容网关（需 CUSTOM_API_KEY）。
    """
    provider = _env("LLM_PROVIDER", "ollama").lower()
    if provider in ("custom", "tokensd"):
        api_key = _env("CUSTOM_API_KEY", "")
        if not api_key:
            return None
        return CustomLLM(
            api_key,
            _env("CUSTOM_BASE_URL", "https://www.tokensd.com.cn/v1"),
            _env("CUSTOM_MODEL", "deepseek-v4-flash"),
        )
    if provider == "deepseek":
        api_key = _env("DEEPSEEK_API_KEY", "")
        if not api_key:
            return None
        return DeepSeekLLM(
            api_key,
            _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            _env("DEEPSEEK_MODEL", "deepseek-chat"),
        )
    # 默认 ollama（本地/局域网，无需密钥）
    return OllamaLLM(
        _env("OLLAMA_BASE_URL", "http://192.168.1.103:11434/v1"),
        _env("OLLAMA_MODEL", "deepseek-r1:14b"),
    )
