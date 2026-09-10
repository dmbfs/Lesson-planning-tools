"""备课导师 · LLM 网关（唯一出网处）
- 运行时模型配置：builtin 模式用 .env 内置模型（tokendance）；custom 模式用用户在设置页自接的模型
- 无 key 时自动 mock，调用方 fallback 到内置示例（契约同构）
- 每次真实调用记账到 usage_ledger（成本护栏）
- 调用留痕由调用方写入 traces（阶段/任务/障碍命中上下文在调用方手里）
"""
import json
import time

import httpx

from . import db
from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT, LLM_RETRIES

class LLMError(RuntimeError):
    """LLM 调用失败（MOCK 模式或无 key 时的统一异常，调用方 catch 后 fallback）"""


def _effective_config() -> tuple[str, str, str]:
    """当前生效的模型配置 (api_key, base_url, model)。
    custom 模式取设置页保存的自接配置；builtin 取 .env 内置配置。"""
    mc = db.get_model_config()
    if mc.get("mode") == "custom" and mc.get("api_key"):
        return mc["api_key"], mc.get("base_url") or LLM_BASE_URL, mc.get("model") or LLM_MODEL
    return LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


def is_mock() -> bool:
    """无有效 key → mock（内置演示）。动态求值，切换模型即时生效。"""
    key, _, _ = _effective_config()
    return not key


def __getattr__(name: str):
    """PEP 562：让外部 `llm.MOCK_MODE` 动态求值（老引用零改动）"""
    if name == "MOCK_MODE":
        return is_mock()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _completions_url(base: str) -> str:
    """OpenAI 兼容端点拼接：兼容 base 已含 /v1 或完整 /chat/completions 的情况"""
    base = (base or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def chat(system: str, user: str, temperature: float = 0.4) -> str:
    """调用 OpenAI 兼容 chat 接口。无 key（mock）抛 LLMError，由调用方 fallback。"""
    api_key, base_url, model = _effective_config()
    if not api_key:
        raise LLMError("mock mode: no api key")
    t0 = time.time()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "stream": False,
    }
    # 网络失败（超时/连接异常/5xx）时按 LLM_RETRIES 有限重试，带固定小退避；仅成功后记账一次
    data = None
    last_err = None
    for attempt in range(LLM_RETRIES + 1):
        try:
            resp = httpx.post(
                _completions_url(base_url),
                headers=headers,
                json=payload,
                timeout=LLM_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            if attempt < LLM_RETRIES:
                time.sleep(0.3)
    if data is None:
        raise LLMError(f"LLM 调用失败: {last_err}")
    content = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage") or {}
    db.add_usage("chat", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    return content


def chat_json(system: str, user: str, temperature: float = 0.3) -> dict:
    """要求模型返回纯 JSON，返回 dict。解析失败抛 LLMError。"""
    text = chat(system, user, temperature)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 返回非 JSON: {e}")


def test_connection(api_key: str, base_url: str, model: str) -> dict:
    """连通性测试：发一次最小请求（1 token），返回 {ok, latency_ms, error?}。网络失败按 LLM_RETRIES 有限重试。"""
    t0 = time.time()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    last_err = None
    for attempt in range(LLM_RETRIES + 1):
        try:
            resp = httpx.post(
                _completions_url(base_url),
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return {"ok": True, "latency_ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            last_err = e
            if attempt < LLM_RETRIES:
                time.sleep(0.3)
    return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(last_err)}


def latency_of(fn):
    """装饰器：返回 (result, latency_ms)，供 trace 留痕使用"""
    t0 = time.time()
    r = fn()
    return r, int((time.time() - t0) * 1000)
