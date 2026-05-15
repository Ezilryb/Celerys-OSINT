"""
database/repositories/config.py
Requêtes SQL pour la configuration dynamique et l'audit log.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

logger = logging.getLogger("wkd.db.config")


class ConfigRepository:
    """Gestion de la table `config`."""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def get(self, key: str) -> Optional[str]:
        row = await self.conn.fetchrow(
            "SELECT value FROM config WHERE key = $1", key
        )
        return row["value"] if row else None

    async def get_int(self, key: str, default: int = 0) -> int:
        val = await self.get(key)
        try:
            return int(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    async def get_float(self, key: str, default: float = 0.0) -> float:
        val = await self.get(key)
        try:
            return float(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    async def get_bool(self, key: str, default: bool = False) -> bool:
        val = await self.get(key)
        if val is None:
            return default
        return val.lower() in ("true", "1", "yes")

    async def set(self, key: str, value: Any, updated_by: Optional[int] = None) -> None:
        await self.conn.execute(
            """
            INSERT INTO config (key, value, updated_at, updated_by)
            VALUES ($1, $2, NOW(), $3)
            ON CONFLICT (key) DO UPDATE
            SET value = $2, updated_at = NOW(), updated_by = $3
            """,
            key,
            str(value),
            updated_by,
        )

    async def get_all(self) -> dict[str, str]:
        rows = await self.conn.fetch("SELECT key, value FROM config ORDER BY key")
        return {row["key"]: row["value"] for row in rows}

    async def is_blockchain_locked(self) -> tuple[bool, str]:
        """Retourne (locked, reason)."""
        locked = await self.get_bool("blockchain_locked", False)
        reason = await self.get("blockchain_lock_reason") or ""
        return locked, reason


class AuditRepository:
    """Requêtes SQL pour `audit_log` et `user_flags`."""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def log_action(
        self,
        actor_id: int,
        action: str,
        target_id: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO audit_log (actor_id, action, target_id, details)
            VALUES ($1, $2, $3, $4)
            """,
            actor_id,
            action,
            target_id,
            json.dumps(details or {}),
        )

    async def get_recent_logs(
        self, limit: int = 50, log_type: Optional[str] = None
    ) -> list[asyncpg.Record]:
        if log_type:
            return await self.conn.fetch(
                """
                SELECT al.*, u.username as actor_name
                FROM audit_log al
                LEFT JOIN users u ON al.actor_id = u.user_id
                WHERE al.action LIKE $1
                ORDER BY al.created_at DESC LIMIT $2
                """,
                f"%{log_type}%",
                limit,
            )
        return await self.conn.fetch(
            """
            SELECT al.*, u.username as actor_name
            FROM audit_log al
            LEFT JOIN users u ON al.actor_id = u.user_id
            ORDER BY al.created_at DESC LIMIT $1
            """,
            limit,
        )

    async def add_flag(
        self,
        user_id: int,
        flag_type: str,
        details: str = "",
        severity: str = "low",
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO user_flags (user_id, flag_type, details, severity)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            flag_type,
            details,
            severity,
        )

    async def get_user_flags(self, user_id: int) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            "SELECT * FROM user_flags WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )

    async def get_unresolved_flags(self) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            """
            SELECT uf.*, u.username
            FROM user_flags uf
            JOIN users u ON uf.user_id = u.user_id
            WHERE uf.resolved = FALSE
            ORDER BY uf.severity DESC, uf.created_at DESC
            """
        )
