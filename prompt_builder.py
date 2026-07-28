"""Planner 好感度元数据注入。"""
from __future__ import annotations

from typing import Any

from .scoring import Group, group_for_score


MARKER = "[affinity_users]"


def build_affinity_block(users: list[dict[str, Any]], groups: tuple[Group, ...], max_length: int = 2000) -> str:
    lines = [MARKER]
    for user in users:
        qq_id = str(user.get("qq_id", "")).strip()
        if not qq_id.isdigit():
            continue
        group = group_for_score(float(user.get("score", 0)), groups)
        lines.append(f"- qq_id: {qq_id}, score_group: {group.name}")
    lines.append("[/affinity_users]")
    return "\n".join(lines)[:max_length]


def append_block(messages: list[dict[str, Any]], block: str) -> list[dict[str, Any]]:
    if not block or MARKER in "\n".join(str(item.get("content", "")) for item in messages):
        return messages
    copied = [dict(item) for item in messages]
    index = next((i for i, item in enumerate(copied) if item.get("role") == "system"), None)
    if index is None:
        copied.insert(0, {"role": "system", "content": block})
    else:
        copied[index]["content"] = f"{copied[index].get('content', '')}\n\n{block}".strip()
    return copied
