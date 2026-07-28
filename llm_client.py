"""OpenAI 兼容好感度批量评估客户端。"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin


class LLMClientError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0, temperature: float = 0.2, max_tokens: int = 2000) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.temperature = float(temperature)
        self.max_tokens = max(1, int(max_tokens))

    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return urljoin(self.base_url + "/", "chat/completions")

    async def evaluate(self, users: list[dict[str, Any]], max_delta: float) -> dict[str, float]:
        if not self.base_url or not self.model or not users:
            return {}
        prompt = {
            "task": "根据每个用户本周期与机器人互动摘要，返回好感度变化。只返回 JSON，不要 markdown。",
            "rules": {"score_delta_min": -abs(max_delta), "score_delta_max": abs(max_delta), "output": {"users": [{"qq_id": "string", "score_delta": "number"}]}},
            "users": users,
        }
        payload = {"model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens, "messages": [{"role": "system", "content": "你是好感度批量评估器。必须只输出合法 JSON。"}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]}
        try:
            import aiohttp
        except ImportError as exc:
            raise LLMClientError("运行环境缺少 aiohttp") from exc
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.endpoint(), headers=headers, json=payload) as response:
                    if response.status < 200 or response.status >= 300:
                        raise LLMClientError(f"LLM HTTP 状态异常: {response.status}")
                    raw = await response.json(content_type=None)
        except LLMClientError:
            raise
        except Exception as exc:
            raise LLMClientError(f"LLM 请求失败: {type(exc).__name__}") from exc
        try:
            content = raw["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
            items = result.get("users", [])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMClientError("LLM 返回不是合法的好感度 JSON") from exc
        output: dict[str, float] = {}
        allowed = {str(item.get("qq_id")) for item in users}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or str(item.get("qq_id")) not in allowed:
                continue
            try:
                delta = float(item.get("score_delta"))
            except (TypeError, ValueError):
                continue
            if delta == delta and abs(delta) != float("inf"):
                output[str(item["qq_id"])] = max(-abs(max_delta), min(abs(max_delta), delta))
        return output
