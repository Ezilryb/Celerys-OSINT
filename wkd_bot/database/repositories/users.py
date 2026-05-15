"""
database/repositories/users.py
Toutes les requêtes relatives aux utilisateurs.
Aucune logique métier ici — uniquement du SQL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger("wkd.db.users")


class UserRepository:
    """Requêtes SQL pour la table `users`."""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    # ------------------------------------------------------------------
    # LECTURE
    # ------------------------------------------------------------------

    async def get_by_id(self, user_id: int) -> Optional[asyncpg.Record]:
        return await self.conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1",
            user_id,
        )

    async def get_by_id_locked(self, user_id: int) -> Optional[asyncpg.Record]:
        """Avec SELECT FOR UPDATE — utiliser dans une transaction atomique."""
        return await self.conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1 FOR UPDATE",
            user_id,
        )

    async def get_balance(self, user_id: int) -> Optional[int]:
        row = await self.conn.fetchrow(
            "SELECT balance FROM users WHERE user_id = $1",
            user_id,
        )
        return row["balance"] if row else None

    async def get_leaderboard(self, limit: int = 9) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            "SELECT user_id, username, balance FROM users "
            "WHERE banned = FALSE ORDER BY balance DESC LIMIT $1",
            limit,
        )

    async def get_top_n(self, n: int = 9) -> list[asyncpg.Record]:
        """Retourne les top N utilisateurs (pour taxe riches)."""
        return await self.conn.fetch(
            "SELECT user_id, username, balance FROM users "
            "WHERE banned = FALSE AND balance > 0 "
            "ORDER BY balance DESC LIMIT $1",
            n,
        )

    async def get_inactive_users(
        self, days_threshold: int = 7, min_balance: int = 222
    ) -> list[asyncpg.Record]:
        """Utilisateurs inactifs depuis N jours avec solde > min_balance."""
        return await self.conn.fetch(
            """
            SELECT user_id, username, balance
            FROM users
            WHERE banned = FALSE
              AND balance > $1
              AND (
                last_message IS NULL
                OR last_message < NOW() - ($2 || ' days')::INTERVAL
              )
            """,
            min_balance,
            str(days_threshold),
        )

    async def get_pending_airdrops(self) -> list[asyncpg.Record]:
        """Membres éligibles à l'airdrop (délai 15j écoulé)."""
        return await self.conn.fetch(
            """
            SELECT user_id, username, airdrop_eligible_date
            FROM users
            WHERE airdrop_received = FALSE
              AND airdrop_eligible_date IS NOT NULL
              AND airdrop_eligible_date <= NOW()
              AND banned = FALSE
            """
        )

    async def exists(self, user_id: int) -> bool:
        row = await self.conn.fetchrow(
            "SELECT 1 FROM users WHERE user_id = $1", user_id
        )
        return row is not None

    async def is_banned(self, user_id: int) -> bool:
        row = await self.conn.fetchrow(
            "SELECT banned FROM users WHERE user_id = $1", user_id
        )
        return row["banned"] if row else False

    async def get_daily_earned(self, user_id: int) -> tuple[int, Optional[datetime]]:
        """Retourne (daily_earned, last_daily_reset)."""
        row = await self.conn.fetchrow(
            "SELECT daily_earned, last_daily_reset FROM users WHERE user_id = $1",
            user_id,
        )
        if not row:
            return 0, None
        return row["daily_earned"], row["last_daily_reset"]

    # ------------------------------------------------------------------
    # ÉCRITURE
    # ------------------------------------------------------------------

    async def create(
        self,
        user_id: int,
        username: str,
        account_created_date: datetime,
        server_join_date: datetime,
        airdrop_eligible_date: datetime,
    ) -> asyncpg.Record:
        """Crée un nouveau compte utilisateur."""
        return await self.conn.fetchrow(
            """
            INSERT INTO users (
                user_id, username, balance, account_created_date,
                server_join_date, airdrop_eligible_date
            ) VALUES ($1, $2, 0, $3, $4, $5)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING *
            """,
            user_id,
            username,
            account_created_date,
            server_join_date,
            airdrop_eligible_date,
        )

    async def update_balance(self, user_id: int, delta: int) -> int:
        """
        Modifie le solde de manière atomique.
        Le trigger PostgreSQL empêche le passage en négatif.
        Retourne le nouveau solde.
        """
        row = await self.conn.fetchrow(
            """
            UPDATE users
            SET balance = balance + $1
            WHERE user_id = $2
            RETURNING balance
            """,
            delta,
            user_id,
        )
        if not row:
            raise ValueError(f"Utilisateur {user_id} introuvable")
        return row["balance"]

    async def update_username(self, user_id: int, username: str) -> None:
        await self.conn.execute(
            "UPDATE users SET username = $1 WHERE user_id = $2",
            username,
            user_id,
        )

    async def update_last_message(self, user_id: int) -> None:
        await self.conn.execute(
            "UPDATE users SET last_message = NOW() WHERE user_id = $1",
            user_id,
        )

    async def increment_message_count(self, user_id: int) -> int:
        """Incrémente le compteur de messages. Retourne le nouveau total."""
        row = await self.conn.fetchrow(
            """
            UPDATE users
            SET message_count = message_count + 1,
                last_message = NOW()
            WHERE user_id = $1
            RETURNING message_count
            """,
            user_id,
        )
        return row["message_count"] if row else 0

    async def add_daily_earned(self, user_id: int, amount: int) -> None:
        """Incrémente le compteur journalier et met à jour la date."""
        today = datetime.now(timezone.utc).date()
        await self.conn.execute(
            """
            UPDATE users
            SET daily_earned = CASE
                    WHEN last_daily_reset = $1 THEN daily_earned + $2
                    ELSE $2
                END,
                last_daily_reset = $1,
                total_earned = total_earned + $2,
                balance = balance + $2
            WHERE user_id = $3
            """,
            today,
            amount,
            user_id,
        )

    async def mark_airdrop_received(self, user_id: int, amount: int) -> None:
        await self.conn.execute(
            """
            UPDATE users
            SET airdrop_received = TRUE,
                balance = balance + $1,
                total_earned = total_earned + $1
            WHERE user_id = $2
            """,
            amount,
            user_id,
        )

    async def set_airdrop_eligible_now(self, user_id: int) -> None:
        """Rend l'utilisateur immédiatement éligible (exemption admin)."""
        await self.conn.execute(
            "UPDATE users SET airdrop_eligible_date = NOW() WHERE user_id = $1",
            user_id,
        )

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.conn.execute(
            "UPDATE users SET banned = $1 WHERE user_id = $2",
            banned,
            user_id,
        )

    async def add_flag(self, user_id: int, flag: str) -> None:
        await self.conn.execute(
            """
            UPDATE users
            SET flags = array_append(flags, $1)
            WHERE user_id = $2 AND NOT ($1 = ANY(flags))
            """,
            flag,
            user_id,
        )

    async def update_stats_spent(self, user_id: int, amount: int) -> None:
        await self.conn.execute(
            "UPDATE users SET total_spent = total_spent + $1 WHERE user_id = $2",
            amount,
            user_id,
        )

    async def update_stats_burned(self, user_id: int, amount: int) -> None:
        await self.conn.execute(
            "UPDATE users SET total_burned = total_burned + $1, total_spent = total_spent + $1 WHERE user_id = $2",
            amount,
            user_id,
        )
