"""好感度插件的本地持久化实现。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import json
import os
import sqlite3
import time


@dataclass(frozen=True, slots=True)
class PersonRecord:
    """跨群共享的用户好感度记录。"""

    platform: str
    user_id: str
    person_sid: str
    qq_nickname: str
    display_name: str
    score_cents: int
    group_name: str
    first_seen_at: float
    last_seen_at: float
    last_delta_cents: int
    last_reason: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PersonRecord":
        return cls(
            platform=str(row["platform"]),
            user_id=str(row["user_id"]),
            person_sid=str(row["person_sid"] or ""),
            qq_nickname=str(row["qq_nickname"] or ""),
            display_name=str(row["display_name"] or ""),
            score_cents=int(row["score_cents"]),
            group_name=str(row["group_name"] or ""),
            first_seen_at=float(row["first_seen_at"]),
            last_seen_at=float(row["last_seen_at"]),
            last_delta_cents=int(row["last_delta_cents"]),
            last_reason=str(row["last_reason"] or ""),
        )


@dataclass(frozen=True, slots=True)
class MessageIdentity:
    """一条群消息与真实发送者之间的不可变映射。"""

    message_id: str
    platform: str
    user_id: str
    person_sid: str
    group_id: str
    group_name: str
    session_id: str
    qq_nickname: str
    group_card: str
    person: PersonRecord


class AffectionStore:
    """使用 SQLite 保存全局人物、群成员资料和消息身份索引。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS persons (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    person_sid TEXT NOT NULL DEFAULT '',
                    qq_nickname TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    score_cents INTEGER NOT NULL,
                    group_name TEXT NOT NULL,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    last_delta_cents INTEGER NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (platform, user_id)
                );

                DROP INDEX IF EXISTS idx_person_sid;
                CREATE INDEX IF NOT EXISTS idx_person_sid
                ON persons(person_sid);

                CREATE TABLE IF NOT EXISTS group_memberships (
                    platform TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    group_card TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    PRIMARY KEY (platform, group_id, user_id),
                    FOREIGN KEY (platform, user_id)
                        REFERENCES persons(platform, user_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS message_identity (
                    message_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    qq_nickname TEXT NOT NULL DEFAULT '',
                    group_card TEXT NOT NULL DEFAULT '',
                    seen_at REAL NOT NULL,
                    PRIMARY KEY (platform, message_id),
                    FOREIGN KEY (platform, user_id)
                        REFERENCES persons(platform, user_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_message_identity_message_id
                ON message_identity(message_id, seen_at DESC);

                CREATE INDEX IF NOT EXISTS idx_message_identity_session
                ON message_identity(session_id, seen_at DESC);

                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    processed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_platform TEXT NOT NULL,
                    operator_user_id TEXT NOT NULL,
                    target_platform TEXT NOT NULL,
                    target_user_id TEXT NOT NULL,
                    old_score_cents INTEGER NOT NULL,
                    new_score_cents INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    def upsert_identity(
        self,
        *,
        message_id: str,
        platform: str,
        user_id: str,
        person_sid: str,
        qq_nickname: str,
        group_id: str,
        group_name: str,
        group_card: str,
        session_id: str,
        initial_score_cents: int,
        initial_group_name: str,
        seen_at: float,
    ) -> PersonRecord:
        display_name = group_card or qq_nickname or user_id
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO persons (
                    platform, user_id, person_sid, qq_nickname, display_name,
                    score_cents, group_name, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    person_sid = CASE
                        WHEN excluded.person_sid <> '' THEN excluded.person_sid
                        ELSE persons.person_sid
                    END,
                    qq_nickname = CASE
                        WHEN excluded.qq_nickname <> '' THEN excluded.qq_nickname
                        ELSE persons.qq_nickname
                    END,
                    display_name = CASE
                        WHEN excluded.display_name <> '' THEN excluded.display_name
                        ELSE persons.display_name
                    END,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    platform,
                    user_id,
                    person_sid,
                    qq_nickname,
                    display_name,
                    initial_score_cents,
                    initial_group_name,
                    seen_at,
                    seen_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO group_memberships (
                    platform, group_id, user_id, group_name, group_card,
                    session_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, group_id, user_id) DO UPDATE SET
                    group_name = CASE
                        WHEN excluded.group_name <> '' THEN excluded.group_name
                        ELSE group_memberships.group_name
                    END,
                    group_card = excluded.group_card,
                    session_id = excluded.session_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (platform, group_id, user_id, group_name, group_card, session_id, seen_at, seen_at),
            )
            if message_id:
                connection.execute(
                    """
                    INSERT INTO message_identity (
                        message_id, platform, user_id, group_id, group_name,
                        session_id, qq_nickname, group_card, seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, message_id) DO UPDATE SET
                        user_id = excluded.user_id,
                        group_id = excluded.group_id,
                        group_name = excluded.group_name,
                        session_id = excluded.session_id,
                        qq_nickname = excluded.qq_nickname,
                        group_card = excluded.group_card,
                        seen_at = excluded.seen_at
                    """,
                    (
                        message_id,
                        platform,
                        user_id,
                        group_id,
                        group_name,
                        session_id,
                        qq_nickname,
                        group_card,
                        seen_at,
                    ),
                )
        person = self.get_person(platform, user_id)
        if person is None:
            raise RuntimeError(f"写入用户后无法重新读取: platform={platform}, user_id={user_id}")
        return person

    def get_person(self, platform: str, user_id: str) -> PersonRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM persons WHERE platform = ? AND user_id = ?",
                (platform, user_id),
            ).fetchone()
        return PersonRecord.from_row(row) if row is not None else None

    def find_person(self, platform: str, identifier: str) -> PersonRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT * FROM persons
                WHERE (platform = ? AND user_id = ?) OR person_sid = ?
                ORDER BY CASE WHEN platform = ? AND user_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (platform, identifier, identifier, platform, identifier),
            ).fetchone()
        return PersonRecord.from_row(row) if row is not None else None

    def get_message_identity(self, message_id: str, session_id: str = "") -> MessageIdentity | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT
                    mi.message_id,
                    mi.platform,
                    mi.user_id,
                    mi.group_id,
                    mi.group_name AS message_group_name,
                    mi.session_id,
                    mi.qq_nickname AS message_qq_nickname,
                    mi.group_card,
                    p.person_sid,
                    p.qq_nickname,
                    p.display_name,
                    p.score_cents,
                    p.group_name,
                    p.first_seen_at,
                    p.last_seen_at,
                    p.last_delta_cents,
                    p.last_reason
                FROM message_identity AS mi
                JOIN persons AS p
                  ON p.platform = mi.platform AND p.user_id = mi.user_id
                WHERE mi.message_id = ?
                ORDER BY CASE WHEN mi.session_id = ? AND ? <> '' THEN 0 ELSE 1 END,
                         mi.seen_at DESC
                LIMIT 1
                """,
                (message_id, session_id, session_id),
            ).fetchone()
        if row is None:
            return None
        person = PersonRecord(
            platform=str(row["platform"]),
            user_id=str(row["user_id"]),
            person_sid=str(row["person_sid"] or ""),
            qq_nickname=str(row["qq_nickname"] or ""),
            display_name=str(row["display_name"] or ""),
            score_cents=int(row["score_cents"]),
            group_name=str(row["group_name"] or ""),
            first_seen_at=float(row["first_seen_at"]),
            last_seen_at=float(row["last_seen_at"]),
            last_delta_cents=int(row["last_delta_cents"]),
            last_reason=str(row["last_reason"] or ""),
        )
        return MessageIdentity(
            message_id=str(row["message_id"]),
            platform=str(row["platform"]),
            user_id=str(row["user_id"]),
            person_sid=str(row["person_sid"] or ""),
            group_id=str(row["group_id"]),
            group_name=str(row["message_group_name"] or ""),
            session_id=str(row["session_id"] or ""),
            qq_nickname=str(row["message_qq_nickname"] or row["qq_nickname"] or ""),
            group_card=str(row["group_card"] or ""),
            person=person,
        )

    def list_persons(self) -> list[PersonRecord]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT * FROM persons ORDER BY last_seen_at DESC").fetchall()
        return [PersonRecord.from_row(row) for row in rows]

    def count_memberships(self, platform: str, user_id: str) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM group_memberships WHERE platform = ? AND user_id = ?",
                (platform, user_id),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def get_processed_event_ids(self, event_ids: Iterable[str]) -> set[str]:
        normalized_ids = [event_id for event_id in event_ids if event_id]
        if not normalized_ids:
            return set()
        placeholders = ",".join("?" for _ in normalized_ids)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"SELECT event_id FROM processed_events WHERE event_id IN ({placeholders})",
                normalized_ids,
            ).fetchall()
        return {str(row["event_id"]) for row in rows}

    def apply_settlement(self, updates: list[dict[str, Any]]) -> None:
        processed_at = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for update in updates:
                connection.execute(
                    """
                    UPDATE persons
                    SET score_cents = ?, group_name = ?, last_delta_cents = ?, last_reason = ?
                    WHERE platform = ? AND user_id = ?
                    """,
                    (
                        int(update["new_score_cents"]),
                        str(update["group_name"]),
                        int(update["delta_cents"]),
                        str(update["reason"]),
                        str(update["platform"]),
                        str(update["user_id"]),
                    ),
                )
                for event_id in update["event_ids"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO processed_events(event_id, processed_at) VALUES (?, ?)",
                        (str(event_id), processed_at),
                    )
            connection.commit()

    def set_score(
        self,
        *,
        person: PersonRecord,
        new_score_cents: int,
        group_name: str,
        operator_platform: str,
        operator_user_id: str,
        reason: str,
    ) -> PersonRecord:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE persons
                SET score_cents = ?, group_name = ?,
                    last_delta_cents = ?, last_reason = ?
                WHERE platform = ? AND user_id = ?
                """,
                (
                    new_score_cents,
                    group_name,
                    new_score_cents - person.score_cents,
                    reason,
                    person.platform,
                    person.user_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO manual_changes (
                    operator_platform, operator_user_id, target_platform,
                    target_user_id, old_score_cents, new_score_cents,
                    reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operator_platform,
                    operator_user_id,
                    person.platform,
                    person.user_id,
                    person.score_cents,
                    new_score_cents,
                    reason,
                    time.time(),
                ),
            )
            connection.commit()
        updated = self.get_person(person.platform, person.user_id)
        if updated is None:
            raise RuntimeError("管理员修改好感度后无法重新读取目标用户")
        return updated

    def recalculate_group_names(self, resolver: Any) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT platform, user_id, score_cents FROM persons").fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE persons SET group_name = ? WHERE platform = ? AND user_id = ?",
                    (resolver(int(row["score_cents"])), str(row["platform"]), str(row["user_id"])),
                )
            connection.commit()

    def prune_message_identity(self, keep_after: float) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM message_identity WHERE seen_at < ?", (keep_after,))
            return int(cursor.rowcount or 0)


class PendingEventJournal:
    """追加式 JSONL 待结算缓存。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8", newline="\n") as file_obj:
            file_obj.write(serialized + "\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as file_obj:
            for line_number, raw_line in enumerate(file_obj, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"待结算缓存第 {line_number} 行损坏: {exc}") from exc
                if not isinstance(event, dict) or not str(event.get("event_id") or ""):
                    raise ValueError(f"待结算缓存第 {line_number} 行缺少 event_id")
                events.append(event)
                if limit is not None and len(events) >= limit:
                    break
        return events

    def remove(self, event_ids: set[str]) -> int:
        if not event_ids:
            return 0
        remaining: list[str] = []
        removed = 0
        with self.path.open("r", encoding="utf-8") as file_obj:
            for line_number, raw_line in enumerate(file_obj, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"待结算缓存第 {line_number} 行损坏: {exc}") from exc
                if str(event.get("event_id") or "") in event_ids:
                    removed += 1
                    continue
                remaining.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8", newline="\n") as file_obj:
            if remaining:
                file_obj.write("\n".join(remaining) + "\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_path, self.path)
        return removed

    def count(self) -> int:
        return len(self.read())
