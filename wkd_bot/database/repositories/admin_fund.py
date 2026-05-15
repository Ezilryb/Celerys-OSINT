"""
database/repositories/admin_fund.py
Requêtes SQL pour le fond administrateur.
"""

from __future__ import annotations
import asyncpg


class AdminFundRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def get(self) -> asyncpg.Record:
        return await self.conn.fetchrow("SELECT * FROM admin_fund ORDER BY id LIMIT 1")

    async def get_locked(self) -> asyncpg.Record:
        """Avec verrou FOR UPDATE — utiliser dans une transaction."""
        return await self.conn.fetchrow(
            "SELECT * FROM admin_fund ORDER BY id LIMIT 1 FOR UPDATE"
        )

    async def adjust_balance(self, delta: int) -> int:
        """Modifie le solde du fond. Retourne le nouveau solde."""
        row = await self.conn.fetchrow(
            """
            UPDATE admin_fund
            SET balance = balance + $1,
                total_distributed = CASE WHEN $1 < 0 THEN total_distributed + (-$1) ELSE total_distributed END,
                total_created     = CASE WHEN $1 > 0 THEN total_created + $1 ELSE total_created END,
                last_updated = NOW()
            RETURNING balance
            """,
            delta,
        )
        if not row:
            raise RuntimeError("Fond admin introuvable")
        if row["balance"] < 0:
            raise ValueError("Solde fond admin insuffisant")
        return row["balance"]

    async def log_distribution(
        self, amount: int, to_user: int, reason: str, conn: asyncpg.Connection
    ) -> None:
        """Enregistre une distribution dans l'audit (appelé dans la même transaction)."""
        pass  # Géré via audit_log dans la couche service
