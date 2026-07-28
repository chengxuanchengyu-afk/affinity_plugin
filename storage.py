"""好感度持久化与临时事件缓冲。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class AffinityStorage:
    def __init__(self, data_dir: Path, runtime_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.runtime_dir = Path(runtime_dir) if runtime_dir else None
        self.path = self.data_dir / "affinity.json"
        self._lock = asyncio.Lock()
        self.users: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self._seen_message_ids: set[str] = set()
        self._pending: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            self.users = data.get("users", {}) if isinstance(data.get("users"), dict) else {}
            self.audit = data.get("audit", []) if isinstance(data.get("audit"), list) else []

    async def _save_unlocked(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="affinity-", suffix=".json", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "users": self.users, "audit": self.audit[-500:]}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    async def save(self) -> None:
        async with self._lock:
            await self._save_unlocked()

    def get_user(self, qq_id: str, initial_score: float = 20.0) -> dict[str, Any]:
        user = self.users.get(str(qq_id))
        if user is None:
            user = {"qq_id": str(qq_id), "score": float(initial_score), "total_messages": 0, "last_seen_at": ""}
            self.users[str(qq_id)] = user
        return user

    async def update_user(self, qq_id: str, score: float, *, message_count: int = 0, last_seen_at: str = "") -> dict[str, Any]:
        async with self._lock:
            user = self.get_user(qq_id)
            user["score"] = round(max(0.0, min(100.0, float(score))), 2)
            user["total_messages"] = int(user.get("total_messages", 0)) + int(message_count)
            if last_seen_at:
                user["last_seen_at"] = last_seen_at
            await self._save_unlocked()
            return dict(user)

    async def audit_change(self, operator: str, target: str, operation: str, old_score: float, new_score: float) -> None:
        async with self._lock:
            self.audit.append({"operator_qq_id": operator, "target_qq_id": target, "operation": operation, "old_score": old_score, "new_score": new_score})
            await self._save_unlocked()

    def mark_message_seen(self, message_id: str) -> bool:
        message_id = str(message_id or "").strip()
        if not message_id:
            return True
        if message_id in self._seen_message_ids:
            return False
        self._seen_message_ids.add(message_id)
        if len(self._seen_message_ids) > 10000:
            self._seen_message_ids = set(list(self._seen_message_ids)[-5000:])
        return True

    def add_event(self, qq_id: str, event: dict[str, Any]) -> None:
        current = self._pending.setdefault(str(qq_id), {"message_count": 0, "mention_bot_count": 0, "reply_bot_count": 0, "continued_count": 0, "helpful_count": 0, "duplicate_count": 0, "spam_count": 0, "groups": [], "summaries": []})
        for key in ("message_count", "mention_bot_count", "reply_bot_count", "continued_count", "helpful_count", "duplicate_count", "spam_count"):
            current[key] += int(event.get(key, 0) or 0)
        group_id = str(event.get("group_id", "") or "")
        if group_id and group_id not in current["groups"]:
            current["groups"].append(group_id)
        summary = str(event.get("summary", "") or "").strip()
        if summary and len(current["summaries"]) < 30:
            current["summaries"].append(summary[:300])

    def pending_count(self) -> int:
        return sum(int(item.get("message_count", 0)) for item in self._pending.values())

    def exchange_pending(self) -> dict[str, dict[str, Any]]:
        pending = self._pending
        self._pending = {}
        return pending
