"""轻量 OpenAI 兼容聊天接口客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib import parse

import asyncio
import http.client
import ipaddress
import json
import socket


@dataclass(frozen=True, slots=True)
class OpenAIResponse:
    """一次成功的 OpenAI 兼容响应。"""

    content: str
    model: str


class OpenAICompatibleError(RuntimeError):
    """OpenAI 兼容接口调用失败。"""


def _parse_http_url(url: str) -> parse.ParseResult:
    """解析并校验 OpenAI 接口 URL 的基础结构。"""

    parsed = parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("接口地址必须是有效的 http 或 https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("接口地址不能包含用户名或密码")
    if parsed.fragment:
        raise ValueError("接口地址不能包含 URL 片段")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("接口地址端口无效") from exc
    return parsed


def _require_public_ip(address: str) -> str:
    """只允许可在公网路由的 IP，阻止本机、内网与保留地址。"""

    normalized_address = address.split("%", 1)[0]
    ip_address = ipaddress.ip_address(normalized_address)
    if isinstance(ip_address, ipaddress.IPv6Address) and ip_address.ipv4_mapped is not None:
        ip_address = ip_address.ipv4_mapped
    if not ip_address.is_global:
        raise ValueError(f"接口地址禁止指向本机、内网或非公网地址: {address}")
    return str(ip_address)


def resolve_public_endpoint(endpoint: str) -> tuple[parse.ParseResult, str, int]:
    """解析接口域名并返回一个经过公网校验的固定连接地址。"""

    parsed = _parse_http_url(endpoint)
    hostname = str(parsed.hostname)
    normalized_hostname = hostname.rstrip(".").lower()
    if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
        raise ValueError("接口地址禁止使用 localhost")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal_ip = ipaddress.ip_address(normalized_hostname.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        return parsed, _require_public_ip(str(literal_ip)), port

    try:
        address_infos = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise OpenAICompatibleError(f"网络连接失败: DNS 解析失败: {exc}") from exc

    resolved_addresses: list[str] = []
    for address_info in address_infos:
        address = str(address_info[4][0])
        normalized_address = _require_public_ip(address)
        if normalized_address not in resolved_addresses:
            resolved_addresses.append(normalized_address)
    if not resolved_addresses:
        raise OpenAICompatibleError("网络连接失败: 接口域名没有可用地址")
    return parsed, resolved_addresses[0], port


def normalize_chat_completions_url(base_url: str) -> str:
    """将基础地址规范化为 chat/completions 地址。"""

    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("接口地址不能为空")
    parsed = _parse_http_url(normalized)
    hostname = str(parsed.hostname)
    normalized_hostname = hostname.rstrip(".").lower()
    if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
        raise ValueError("接口地址禁止使用 localhost")
    try:
        literal_ip = ipaddress.ip_address(normalized_hostname.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        _require_public_ip(str(literal_ip))
    normalized_path = parsed.path.rstrip("/")
    if normalized_path.endswith("/chat/completions"):
        return normalized
    if normalized_path.endswith("/v1"):
        normalized_path += "/chat/completions"
    else:
        normalized_path += "/v1/chat/completions"
    return parse.urlunparse(parsed._replace(path=normalized_path or "/v1/chat/completions"))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接已校验 IP，同时使用原始域名进行 TLS SNI 与证书校验。"""

    def __init__(
        self,
        hostname: str,
        resolved_ip: str,
        port: int,
        *,
        timeout: float,
    ) -> None:
        self._resolved_ip = resolved_ip
        super().__init__(hostname, port=port, timeout=timeout)

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self._resolved_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


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
        parsed, resolved_ip, port = resolve_public_endpoint(self.endpoint)
        hostname = str(parsed.hostname)
        default_port = 443 if parsed.scheme == "https" else 80
        host_display = f"[{hostname}]" if ":" in hostname else hostname
        host_header = host_display if port == default_port else f"{host_display}:{port}"
        headers = {
            "Content-Type": "application/json",
            "Host": host_header,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                hostname,
                resolved_ip,
                port,
                timeout=self.timeout_seconds,
            )
        else:
            connection = http.client.HTTPConnection(
                resolved_ip,
                port=port,
                timeout=self.timeout_seconds,
            )
        try:
            connection.request("POST", request_target, body=body, headers=headers)
            response = connection.getresponse()
            raw_response = response.read().decode("utf-8", errors="replace")
            if 300 <= response.status < 400:
                raise OpenAICompatibleError(f"HTTP {response.status}: 出于安全原因不跟随接口重定向")
            if not 200 <= response.status < 300:
                detail = self._extract_error_detail(raw_response)
                raise OpenAICompatibleError(f"HTTP {response.status}: {detail}")
        except OpenAICompatibleError:
            raise
        except TimeoutError as exc:
            raise OpenAICompatibleError("模型请求超时") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise OpenAICompatibleError(f"网络连接失败: {exc}") from exc
        finally:
            connection.close()

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
