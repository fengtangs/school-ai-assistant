"""大模型调用（OpenAI 兼容接口）。

支持两种后端，通过环境变量 LLM_PROVIDER 切换（默认 ollama）：

  - ollama  ：调用本地/局域网 Ollama 的 OpenAI 兼容接口（无需 API Key）
              默认地址 http://192.168.1.103:11434/v1，默认模型 deepseek-r1:14b
  - deepseek：调用 DeepSeek 官方 API（https://api.deepseek.com），需 DEEPSEEK_API_KEY

DeepSeek 官方接口与 Ollama 的 OpenAI 兼容接口协议一致，
因此这里统一用同一个封装，仅 base_url 与密钥不同。
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
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=_build_messages(system_prompt, _build_user_content(context, question)),
            temperature=0.3,
            max_tokens=int(_env("LLM_MAX_TOKENS", "2048")),
        )
        return resp.choices[0].message.content.strip()

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


def get_llm():
    """按 LLM_PROVIDER 返回对应 LLM 实例；未配置可用后端则返回 None。

    默认使用 Ollama 本地模型。设置 LLM_PROVIDER=deepseek 时回退到官方 API
    （需同时提供 DEEPSEEK_API_KEY，否则同样返回 None）。
    """
    provider = _env("LLM_PROVIDER", "ollama").lower()
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
