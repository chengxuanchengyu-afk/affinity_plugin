"""好感度分组与评分纯逻辑。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    min_score: int
    max_score: int


@dataclass(frozen=True)
class ScoreWeights:
    normal_message: float = 0.02
    mention_bot: float = 0.3
    reply_bot: float = 0.4
    continued_conversation: float = 0.2
    helpful_interaction: float = 0.5
    duplicate_message: float = -0.2
    spam: float = -0.3


def build_groups(raw_groups: Iterable[dict[str, Any]]) -> tuple[Group, ...]:
    groups = []
    ids: set[str] = set()
    for raw in raw_groups:
        group = Group(
            id=str(raw.get("id", "")).strip(),
            name=str(raw.get("name", "")).strip(),
            min_score=int(raw.get("min_score", -1)),
            max_score=int(raw.get("max_score", -1)),
        )
        if not group.id or not group.name:
            raise ValueError("好感度分组的 id 和 name 不能为空")
        if group.id in ids:
            raise ValueError(f"好感度分组 ID 重复: {group.id}")
        if not 0 <= group.min_score <= group.max_score <= 100:
            raise ValueError(f"好感度分组范围非法: {group.id}")
        ids.add(group.id)
        groups.append(group)
    groups.sort(key=lambda item: item.min_score)
    if not groups or groups[0].min_score != 0 or groups[-1].max_score != 100:
        raise ValueError("好感度分组必须完整覆盖 0-100")
    expected = 0
    for group in groups:
        if group.min_score != expected:
            raise ValueError("好感度分组存在缺口或重叠")
        expected = group.max_score + 1
    return tuple(groups)


def clamp_score(score: float) -> float:
    if not isfinite(float(score)):
        return 0.0
    return round(max(0.0, min(100.0, float(score))), 2)


def group_for_score(score: float, groups: Iterable[Group]) -> Group:
    value = clamp_score(score)
    for group in groups:
        if group.min_score <= value <= group.max_score:
            return group
    raise ValueError(f"没有匹配的好感度分组: {value}")


def local_delta(event: dict[str, Any], weights: ScoreWeights) -> float:
    return (
        int(event.get("message_count", 0)) * weights.normal_message
        + int(event.get("mention_bot_count", 0)) * weights.mention_bot
        + int(event.get("reply_bot_count", 0)) * weights.reply_bot
        + int(event.get("continued_count", 0)) * weights.continued_conversation
        + int(event.get("helpful_count", 0)) * weights.helpful_interaction
        + int(event.get("duplicate_count", 0)) * weights.duplicate_message
        + int(event.get("spam_count", 0)) * weights.spam
    )


def capped_delta(delta: float, positive_limit: float, negative_limit: float) -> float:
    return max(float(negative_limit), min(float(positive_limit), float(delta)))


def apply_daily_limit(delta: float, used_today: float, positive_limit: float, negative_limit: float) -> float:
    if delta >= 0:
        return min(delta, max(0.0, positive_limit - max(0.0, used_today)))
    used_negative = min(0.0, used_today)
    return max(delta, min(0.0, negative_limit - used_negative))


def decay_delta(last_seen_at: str | None, now: datetime, enabled: bool, inactive_days: int, daily_decay: float, max_decay: float) -> float:
    if not enabled or not last_seen_at:
        return 0.0
    try:
        previous = datetime.fromisoformat(last_seen_at.replace("Z", "+00:00"))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        days = max(0, (now - previous).days - int(inactive_days))
        return -min(float(max_decay), days * float(daily_decay)) if days else 0.0
    except (TypeError, ValueError):
        return 0.0
