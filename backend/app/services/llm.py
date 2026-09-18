"""LLM 调用统一入口。

之前 nodes.py 里直接建 OpenAI 客户端，知识库抽取也要用，
两处各写一份迟早会分叉（换模型、改超时、加重试要改两遍），所以抽到这里。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.config import settings
from app.services import trace


def _client(timeout: float = 120.0):
    if not settings.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")

    from openai import OpenAI

    return OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL,
                  timeout=timeout)


def chat_completion(messages: list[dict], temperature: float | None = None,
                    json_mode: bool = False, timeout: float = 120.0) -> str:
    """底层调用，返回文本。messages 用 OpenAI 协议的原生格式。"""
    kwargs: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE if temperature is None else temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    # 从最后一条用户消息里取个简短摘要，方便追踪面板辨识这次调用在干嘛
    summary = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            summary = (m.get("content") or "").strip().replace("\n", " ")[:40]
            break

    t0 = time.time()
    trace.emit("llm", f"LLM · {settings.LLM_MODEL}", f"输入: {summary}…" if summary else "调用模型")
    resp = _client(timeout).chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    cost = time.time() - t0
    usage = getattr(resp, "usage", None)
    tokens = f"{usage.total_tokens} tok" if usage and usage.total_tokens else "—"
    trace.emit("llm", f"LLM 完成 · {cost:.2f}s", f"{tokens} · 输出 {len(content)} 字")
    return content


def json_completion(system: str, user: str, temperature: float | None = None,
                    timeout: float = 120.0) -> dict:
    """要求模型返回 JSON 并解析。"""
    raw = chat_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature, json_mode=True, timeout=timeout,
    )
    return _loads(raw)


def chat_completion_stream(messages: list[dict], temperature: float | None = None,
                           timeout: float = 120.0):
    """流式调用，逐个产出文本片段。"""
    kwargs: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE if temperature is None else temperature,
    }
    t0 = time.time()
    trace.emit("llm", f"LLM · {settings.LLM_MODEL}", "流式输出开始")
    resp = _client(timeout).chat.completions.create(**kwargs, stream=True)
    n = 0
    for chunk in resp:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            n += len(delta)
            yield delta
    cost = time.time() - t0
    trace.emit("llm", f"LLM 完成 · {cost:.2f}s", f"流式 {n} 字")


def _loads(raw: str) -> dict:
    """模型偶尔会在 JSON 外面裹一层 ```json，这里兜一下。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip().strip("`").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 退一步：截取第一个 { 到最后一个 }
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                pass
        raise
