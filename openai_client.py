"""轻量 OpenAI 兼容聊天接口客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

import asyncio
import json


@dataclass(frozen=True, slots=True)
class OpenAIResponse:
    """一次成功的 OpenAI 兼容响应。"""

    content: str
    model: str


class OpenAICompatibleError(RuntimeError):
    """OpenAI 兼容接口调用失败。"""


def normalize_chat_completions_url(base_url: str) -> str:
    """将基础地址规范化为 chat/completions 地址。"""

    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("接口地址不能为空")
    parsed = parse.urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口地址必须是有效的 http 或 https URL")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return normalized
    if parsed.path.rstrip("/").endswith("/v1"):
        return normalized + "/chat/completions"
    return normalized + "/v1/chat/completions"


class OpenAICompatibleClient:
    """只依赖标准库的 OpenAI 兼容客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        retry_count: int,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.endpoint = normalize_chat_completions_url(base_url)
        self.api_key = str(api_key or "").strip()
        self.model_name = str(model_name or "").strip()
        self.retry_count = max(0, int(retry_count))
        self.timeout_seconds = float(timeout_seconds)
        if not self.model_name:
            raise ValueError("模型名称不能为空")

    async def generate_json(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> OpenAIResponse:
        """调用兼容接口并返回文本内容。"""

        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                return await asyncio.to_thread(self._request_once, payload)
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_count or not self._is_retryable(exc):
                    break
                await asyncio.sleep(min(2**attempt, 8))
        if isinstance(last_error, OpenAICompatibleError):
            raise last_error
        raise OpenAICompatibleError(str(last_error or "未知模型调用错误"))

    def _request_once(self, payload: dict[str, Any]) -> OpenAIResponse:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        http_request = request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw_response = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            response_text = exc.read().decode("utf-8", errors="replace")
            detail = self._extract_error_detail(response_text)
            raise OpenAICompatibleError(f"HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise OpenAICompatibleError(f"网络连接失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OpenAICompatibleError("模型请求超时") from exc

        try:
            response_payload = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise OpenAICompatibleError("接口返回的不是有效 JSON") from exc
        try:
            choice = response_payload["choices"][0]
            message = choice["message"]
            content = self._normalize_content(message.get("content"))
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAICompatibleError("接口响应缺少 choices[0].message.content") from exc
        if not content.strip():
            raise OpenAICompatibleError("模型返回了空内容")
        return OpenAIResponse(
            content=content,
            model=str(response_payload.get("model") or self.model_name),
        )

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                    text_parts.append(str(item.get("text") or ""))
            return "".join(text_parts)
        return str(content or "")

    @staticmethod
    def _extract_error_detail(response_text: str) -> str:
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            return response_text[:300] or "请求失败"
        error_payload = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error_payload, dict):
            return str(error_payload.get("message") or error_payload)[:300]
        return str(payload)[:300]

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        message = str(exc)
        if message.startswith("HTTP "):
            try:
                status_code = int(message.split(":", 1)[0].split()[1])
            except (IndexError, ValueError):
                return False
            return status_code in {408, 409, 425, 429} or status_code >= 500
        return "网络连接失败" in message or "超时" in message
