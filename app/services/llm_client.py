"""OpenAI 兼容大模型客户端。

只依赖 httpx，自己解析 SSE，避免额外 SDK。所有请求都支持外部取消信号，
方便用户中途结束面试时立刻断流。
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Iterator

import httpx

from ..config import config


class LLMError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, LLMError):
        if exc.status == 401:
            return "大模型鉴权失败，请检查 API Key"
        if exc.status == 402:
            return "大模型账户余额不足"
        if exc.status == 429:
            return "大模型请求过于频繁，请稍后重试"
        if exc.status and 500 <= exc.status < 600:
            return "大模型服务端异常，请稍后重试"
        return exc.message
    if isinstance(exc, httpx.TimeoutException):
        return "大模型请求超时"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接大模型服务，请检查网络或 Base URL"
    return f"请求失败：{exc}"


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def parse_json_loose(text: str) -> dict:
    """模型偶尔会带解释文字或代码块，这里尽量把 JSON 抠出来。"""
    cleaned = _strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    array_start = cleaned.find("[")
    array_end = cleaned.rfind("]")
    if array_start >= 0 and array_end > array_start:
        try:
            return {"items": json.loads(cleaned[array_start : array_end + 1])}
        except json.JSONDecodeError:
            pass
    raise LLMError("模型返回的内容不是合法 JSON")


class LLMClient:
    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or config

    # -- 基础 ------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return str(self.cfg.get("llm.base_url") or "").rstrip("/")

    @property
    def model(self) -> str:
        return str(self.cfg.get("llm.model") or "")

    @property
    def api_key(self) -> str:
        return str(self.cfg.get("llm.api_key") or "")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: list[dict],
        *,
        stream: bool,
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": self.cfg.get("llm.temperature", 0.7) if temperature is None else temperature,
            "max_tokens": max_tokens or int(self.cfg.get("llm.max_tokens", 1024)),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _endpoint(self) -> str:
        if not self.base_url:
            raise LLMError("未配置大模型 Base URL")
        if not self.api_key:
            raise LLMError("未配置大模型 API Key")
        return f"{self.base_url}/chat/completions"

    # -- 非流式 ----------------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        retries: int = 1,
    ) -> str:
        url = self._endpoint()
        attempt = 0
        use_json = json_mode
        while True:
            attempt += 1
            payload = self._payload(
                messages, stream=False, json_mode=use_json,
                temperature=temperature, max_tokens=max_tokens,
            )
            try:
                with httpx.Client(timeout=timeout or float(self.cfg.get("llm.timeout", 60))) as client:
                    response = client.post(url, headers=self._headers(), json=payload)
                if response.status_code == 400 and use_json:
                    # 部分服务商不支持 response_format，去掉重试一次
                    use_json = False
                    continue
                if response.status_code != 200:
                    raise LLMError(
                        f"大模型返回 {response.status_code}：{response.text[:200]}",
                        status=response.status_code,
                    )
                data = response.json()
                return data["choices"][0]["message"]["content"] or ""
            except (httpx.TimeoutException, httpx.TransportError, LLMError) as exc:
                if attempt > retries:
                    raise
                if isinstance(exc, LLMError) and exc.status in (400, 401, 402):
                    raise
                time.sleep(1.2 * attempt)

    def chat_json(self, messages: list[dict], **kwargs: Any) -> dict:
        text = self.chat(messages, json_mode=True, **kwargs)
        return parse_json_loose(text)

    # -- 流式 ------------------------------------------------------------
    def chat_stream(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancel: threading.Event | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        url = self._endpoint()
        payload = self._payload(
            messages, stream=True, json_mode=False,
            temperature=temperature, max_tokens=max_tokens,
        )
        read_timeout = timeout or float(self.cfg.get("llm.timeout", 60))
        try:
            with httpx.Client(timeout=httpx.Timeout(read_timeout, connect=10.0)) as client:
                with client.stream("POST", url, headers=self._headers(), json=payload) as response:
                    if response.status_code != 200:
                        body = response.read().decode("utf-8", errors="ignore")
                        raise LLMError(
                            f"大模型返回 {response.status_code}：{body[:200]}",
                            status=response.status_code,
                        )
                    for line in response.iter_lines():
                        if cancel is not None and cancel.is_set():
                            return
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk in ("", "[DONE]"):
                            if chunk == "[DONE]":
                                return
                            continue
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMError(friendly_error(exc)) from exc

    # -- 连通性 ----------------------------------------------------------
    def test_connection(self) -> tuple[bool, str, float]:
        started = time.perf_counter()
        try:
            reply = self.chat(
                [{"role": "user", "content": "只回复两个字：就绪"}],
                max_tokens=16, temperature=0.0, timeout=25,
            )
            elapsed = time.perf_counter() - started
            return True, f"连接成功（{reply.strip()[:20]}）", elapsed
        except Exception as exc:
            return False, friendly_error(exc), time.perf_counter() - started

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(f"{self.base_url}/models", headers=self._headers())
            if response.status_code != 200:
                return []
            data = response.json().get("data") or []
            return sorted({item.get("id", "") for item in data if item.get("id")})
        except Exception:
            return []
