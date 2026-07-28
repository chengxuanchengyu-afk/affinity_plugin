"""QQ 身份提取与安全消息归一化。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Identity:
    qq_id: str
    source: str
    confidence: float
    message_id: str = ""
    session_id: str = ""
    group_id: str = ""


def _read_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
        if value is None:
            return None
    return value


def _valid_qq(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() and 5 <= len(text) <= 12 else None


def extract_identity(message: Any, *, kwargs: dict[str, Any] | None = None) -> Identity | None:
    payload = kwargs or {}
    candidates = [
        ("kwargs.user_id", payload.get("user_id"), 1.0),
        ("kwargs.sender_id", payload.get("sender_id"), 1.0),
        ("message.user_id", _read_path(message, "user_id"), 1.0),
        ("message.sender.user_id", _read_path(message, "sender.user_id"), 1.0),
        ("message.user_info.user_id", _read_path(message, "user_info.user_id"), 0.98),
        ("raw.user_id", _read_path(message, "raw.user_id"), 0.95),
        ("raw.sender.user_id", _read_path(message, "raw.sender.user_id"), 0.95),
    ]
    found: list[tuple[str, str, float]] = []
    for source, value, confidence in candidates:
        qq_id = _valid_qq(value)
        if qq_id and not any(item[1] == qq_id for item in found):
            found.append((source, qq_id, confidence))
    if not found or len({item[1] for item in found}) != 1:
        return None
    source, qq_id, confidence = found[0]
    return Identity(
        qq_id=qq_id,
        source=source,
        confidence=confidence,
        message_id=str(payload.get("message_id") or _read_path(message, "message_id") or ""),
        session_id=str(payload.get("session_id") or _read_path(message, "session_id") or ""),
        group_id=str(payload.get("group_id") or _read_path(message, "group_id") or ""),
    )


def identity_from_payload(payload: dict[str, Any]) -> Identity | None:
    return extract_identity(payload.get("message", payload), kwargs=payload)
