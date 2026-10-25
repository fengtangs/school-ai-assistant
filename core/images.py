"""课件配图生成：调用 OpenAI 兼容网关的 /images/generations，下载并本地缓存。

设计说明：
- 只依赖环境变量（CUSTOM_API_KEY / CUSTOM_BASE_URL），不引入新的 SDK 依赖，
  直接用标准库 urllib，避免给项目增加 requirements。
- 网关返回的是带签名的临时 URL（约 24h 过期），**必须当场下载落盘**，
  不能把 URL 直接塞进 PPT，否则用户隔天打开课件图片就裂了。
- 同一提示词按 hash 缓存，重复生成课件不会重复计费。
- 任何失败都返回 None，绝不让配图问题导致整个课件生成失败。
"""
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


# 网关实测可用的图片模型（按优先级）：seedream 5.0 flash 最快最省
DEFAULT_IMAGE_MODEL = "ds-doubao-seedream-5-0-flash-260915"
FALLBACK_IMAGE_MODELS = (
    "ds-doubao-seedream-5-0-flash-260915",
    "ds-doubao-seedream-5-0-pro-260628",
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def image_model() -> str:
    return _env("PPT_IMAGE_MODEL", "") or _env("CUSTOM_IMAGE_MODEL", "") or DEFAULT_IMAGE_MODEL


def _cache_dir() -> Path:
    """配图缓存目录：默认放在生成产物同级，便于随产物一起清理。"""
    d = Path(_env("PPT_IMAGE_CACHE", "") or "data/ppt_images")
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_available() -> bool:
    """是否具备配图能力（需要 custom 网关的密钥）。"""
    provider = _env("LLM_PROVIDER", "ollama").lower()
    has_key = bool(_env("CUSTOM_API_KEY", ""))
    return bool(has_key and provider in ("custom", "tokensd"))


def _post_json(url: str, payload: dict, api_key: str, timeout: int) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str, dest: Path, timeout: int) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
        if not blob:
            return False
        dest.write_bytes(blob)
        return True
    except Exception:  # noqa: BLE001  网络/权限等任何异常都不致命
        return False


def generate_image(prompt: str, size: str = "1024x1024", timeout: int = 120) -> str | None:
    """根据提示词生成一张配图，返回本地文件路径；失败返回 None。

    prompt 为空时直接返回 None，由调用方回退到纯文字版式。
    """
    prompt = (prompt or "").strip()
    if not prompt or not is_available():
        return None

    api_key = _env("CUSTOM_API_KEY", "")
    base = _env("CUSTOM_BASE_URL", "https://www.tokensd.com.cn/v1").rstrip("/")
    model = image_model()

    key = hashlib.md5(f"{model}|{size}|{prompt}".encode("utf-8")).hexdigest()[:20]
    dest = _cache_dir() / f"{key}.jpg"
    if dest.is_file() and dest.stat().st_size > 1024:
        return str(dest)

    # 依次尝试候选模型：某个模型偶发 500 时自动换下一个，提高成图率
    candidates = [model] + [m for m in FALLBACK_IMAGE_MODELS if m != model]
    for m in candidates:
        payload = {"model": m, "prompt": prompt}
        # 部分模型对 size 校验严格，放在 try 里，失败则去掉 size 重试
        attempts = [dict(payload, size=size), dict(payload)]
        for body in attempts:
            try:
                data = _post_json(f"{base}/images/generations", body, api_key, timeout)
            except urllib.error.HTTPError:
                continue
            except Exception:  # noqa: BLE001
                continue

            items = data.get("data") or []
            if not items or not isinstance(items[0], dict):
                continue
            first = items[0]

            # 情况一：网关直接给 b64（部分部署会这样返回）
            b64 = first.get("b64_json")
            if b64:
                import base64

                try:
                    dest.write_bytes(base64.b64decode(b64))
                    if dest.stat().st_size > 1024:
                        return str(dest)
                except Exception:  # noqa: BLE001
                    pass

            # 情况二：给临时 URL，必须立刻下载
            url = first.get("url")
            if url and _download(url, dest, timeout):
                return str(dest)
    return None


def generate_many(prompts: list, limit: int = 6, size: str = "1024x1024") -> dict:
    """批量生成配图，返回 {下标: 本地路径}。

    limit 用于控制成本与耗时（每张约 3-10 秒），超出部分不生成。
    """
    out = {}
    for i, p in enumerate(prompts or []):
        if len(out) >= limit:
            break
        path = generate_image(p, size=size)
        if path:
            out[i] = path
    return out
