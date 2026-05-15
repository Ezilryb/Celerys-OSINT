"""
database/repositories/transactions.py
Requêtes SQL pour : transactions, contrats, paris, jury.
"""

from __future__ import annotations

import logging
from typing import Optional

import asyncpg

logger = logging.getLogger("wkd.db.tx")


class TransactionRepository:
    """Requêtes SQL pour la table `transactions`."""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def create(
        self,
        from_user: Optional[int],
        to_user: Optional[int],
        amount: int,
        tx_type: str,
        reason: str = "",
        reference_id: str = "",
    ) -> asyncpg.Record:
        """Enregistre une transaction. Retourne la ligne créée."""
        return await self.conn.fetchrow(
            """
            INSERT INTO transactions (from_user, to_user, amount, type, reason, reference_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            from_user,
            to_user,
            amount,
            tx_type,
            reason,
            reference_id,
        )

    async def get_by_id(self, tx_id: str) -> Optional[asyncpg.Record]:
        return await self.conn.fetchrow(
            "SELECT * FROM transactions WHERE tx_id = $1", tx_id
        )

    async def get_user_history(
        self, user_id: int, limit: int = 20, tx_type: Optional[str] = None
    ) -> list[asyncpg.Record]:
        if tx_type:
            return await self.conn.fetch(
                """
                SELECT * FROM public_transactions
                WHERE (from_username = (SELECT username FROM users WHERE user_id = $1)
                    OR to_username = (SELECT username FROM users WHERE user_id = $1))
                  AND type = $2
                ORDER BY created_at DESC LIMIT $3
                """,
                user_id,
                tx_type,
                limit,
            )
        return await self.conn.fetch(
            """
            SELECT t.tx_id, t.type, fu.username as from_username, tu.username as to_username,
                   t.amount, t.reason, t.created_at
            FROM transactions t
            LEFT JOIN users fu ON t.from_user = fu.user_id
            LEFT JOIN users tu ON t.to_user = tu.user_id
            WHERE t.from_user = $1 OR t.to_user = $1
            ORDER BY t.created_at DESC LIMIT $2
            """,
            user_id,
            limit,
        )

    async def get_all_recent(self, limit: int = 50) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            "SELECT * FROM public_transactions LIMIT $1", limit
        )

    async def create_rollback(
        self, tx_id: str, rolled_back_by: int, reason: str
    ) -> None:
        await self.conn.execute(
            "INSERT INTO rollbacks (original_tx_id, rolled_back_by, reason) VALUES ($1, $2, $3)",
            tx_id,
            rolled_back_by,
            reason,
        )


class ContractRepository:
    """Requêtes SQL pour la table `contracts`."""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def create(
        self,
        creator_id: int,
        acceptor_id: int,
        amount_sent: int,
        amount_received: int,
        note: str = "",
    ) -> asyncpg.Record:
        return await self.conn.fetchrow(
            """
            INSERT INTO contracts (creator_id, acceptor_id, amount_sent, amount_received, note)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            creator_id,
            acceptor_id,
            amount_sent,
            amount_received,
            note,
        )

    async def get_by_id(self, contract_id: str) -> Optional[asyncpg.Record]:
        return await self.conn.fetchrow(
            "SELECT * FROM contracts WHERE contract_id = $1", contract_id
        )

    async def get_pending_for_user(self, user_id: int) -> list[asyncpg.Record]:
        """Contrats en attente reçus ou envoyés par cet utilisateur."""
        return await self.conn.fetch(
            """
            SELECT c.*, 
                   u1.username as creator_name, u2.username as acceptor_name
            FROM contracts c
            JOIN users u1 ON c.creator_id = u1.user_id
            JOIN users u2 ON c.acceptor_id = u2.user_id
            WHERE (c.creator_id = $1 OR c.acceptor_id = $1)
              AND c.status = 'pending'
              AND c.expires_at > NOW()
            ORDER BY c.created_at DESC
            """,
            user_id,
        )

    async def get_history_for_user(
        self, user_id: int, limit: int = 20
    ) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            """
            SELECT c.*,
                   u1.username as creator_name, u2.username as acceptor_name
            FROM contracts c
            JOIN users u1 ON c.creator_id = u1.user_id
            JOIN users u2 ON c.acceptor_id = u2.user_id
            WHERE c.creator_id = $1 OR c.acceptor_id = $1
            ORDER BY c.created_at DESC LIMIT $2
            """,
            user_id,
            limit,
        )

    async def has_active_pair_contract(
        self, user_a: int, user_b: int
    ) -> bool:
        """Vérifie si une paire a un contrat en attente (cooldown 24h)."""
        row = await self.conn.fetchrow(
            """
            SELECT 1 FROM contracts
            WHERE (
                (creator_id = $1 AND acceptor_id = $2)
                OR (creator_id = $2 AND acceptor_id = $1)
            )
            AND status = 'pending'
            AND created_at > NOW() - INTERVAL '24 hours'
            """,
            user_a,
            user_b,
        )
        return row is not None

    async def update_status(
        self, contract_id: str, status: str, **kwargs
    ) -> None:
        """Met à jour le statut d'un contrat."""
        valid_statuses = {"pending", "accepted", "completed", "refused", "expired", "cancelled"}
        if status not in valid_statuses:
            raise ValueError(f"Statut invalide : {status}")

        set_clauses = ["status = $1"]
        params = [status, contract_id]

        if status == "accepted":
            set_clauses.append("accepted_at = NOW()")
        elif status == "completed":
            set_clauses.append("completed_at = NOW()")

        await self.conn.execute(
            f"UPDATE contracts SET {', '.join(set_clauses)} WHERE contract_id = ${len(params)}",
            *params,
        )

    async def expire_old_contracts(self) -> int:
        """Marque les contrats expirés. Retourne le nombre traité."""
        result = await self.conn.execute(
            """
            UPDATE contracts
            SET status = 'expired'
            WHERE status = 'pending' AND expires_at < NOW()
            """
        )
        count = int(result.split()[-1])
        return count


class BetRepository:
    """Requêtes SQL pour les tables `bets`, `escrow`, `jury_votes`, `jury_pool`."""

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def create_bet(
        self,
        bettor_a: int,
        bettor_b: int,
        amount: int,
        condition: str,
    ) -> asyncpg.Record:
        return await self.conn.fetchrow(
            """
            INSERT INTO bets (bettor_a, bettor_b, amount, condition)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            bettor_a,
            bettor_b,
            amount,
            condition,
        )

    async def get_bet(self, bet_id: str) -> Optional[asyncpg.Record]:
        return await self.conn.fetchrow(
            """
            SELECT b.*,
                   ua.username as bettor_a_name, ub.username as bettor_b_name,
                   uw.username as winner_name
            FROM bets b
            JOIN users ua ON b.bettor_a = ua.user_id
            JOIN users ub ON b.bettor_b = ub.user_id
            LEFT JOIN users uw ON b.winner = uw.user_id
            WHERE b.bet_id = $1
            """,
            bet_id,
        )

    async def update_bet_status(self, bet_id: str, status: str, **kwargs) -> None:
        """Met à jour le statut d'un pari."""
        updates = {"status": status}

        set_parts = ["status = $1"]
        params = [status]

        if status == "active":
            set_parts.append("escrow_locked = TRUE")
            set_parts.append("accepted_at = NOW()")
        elif status == "pending_jury":
            set_parts.append("accepted_at = NOW()")
        elif status == "resolved":
            set_parts.append("resolved_at = NOW()")
            if "winner" in kwargs:
                set_parts.append(f"winner = ${len(params)+1}")
                params.append(kwargs["winner"])
        elif status == "pending_resolution":
            set_parts.append("claimed_at = NOW()")
            if "claim_note" in kwargs:
                set_parts.append(f"claim_note = ${len(params)+1}")
                params.append(kwargs["claim_note"])

        params.append(bet_id)
        await self.conn.execute(
            f"UPDATE bets SET {', '.join(set_parts)} WHERE bet_id = ${len(params)}",
            *params,
        )

    async def get_active_bets(self) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            """
            SELECT b.*, ua.username as bettor_a_name, ub.username as bettor_b_name
            FROM bets b
            JOIN users ua ON b.bettor_a = ua.user_id
            JOIN users ub ON b.bettor_b = ub.user_id
            WHERE b.status IN ('active', 'pending_resolution')
            ORDER BY b.created_at DESC
            """
        )

    async def get_user_bets(self, user_id: int) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            """
            SELECT b.*, ua.username as bettor_a_name, ub.username as bettor_b_name
            FROM bets b
            JOIN users ua ON b.bettor_a = ua.user_id
            JOIN users ub ON b.bettor_b = ub.user_id
            WHERE b.bettor_a = $1 OR b.bettor_b = $1
            ORDER BY b.created_at DESC
            """,
            user_id,
        )

    async def has_bet_cooldown(self, user_id: int) -> bool:
        """Vérifie cooldown 48h pour création de pari."""
        row = await self.conn.fetchrow(
            """
            SELECT 1 FROM bets
            WHERE (bettor_a = $1 OR bettor_b = $1)
              AND status NOT IN ('cancelled', 'refused', 'expired')
              AND created_at > NOW() - INTERVAL '48 hours'
            """,
            user_id,
        )
        return row is not None

    # --- Escrow ---

    async def lock_escrow(self, bet_id: str, amount_a: int, amount_b: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO escrow (bet_id, user_a_amount, user_b_amount)
            VALUES ($1, $2, $3)
            ON CONFLICT (bet_id) DO NOTHING
            """,
            bet_id,
            amount_a,
            amount_b,
        )

    async def get_escrow(self, bet_id: str) -> Optional[asyncpg.Record]:
        return await self.conn.fetchrow(
            "SELECT * FROM escrow WHERE bet_id = $1", bet_id
        )

    async def release_escrow(self, bet_id: str, released_to: int) -> None:
        await self.conn.execute(
            """
            UPDATE escrow
            SET released_at = NOW(), released_to = $1
            WHERE bet_id = $2
            """,
            released_to,
            bet_id,
        )

    # --- Pool Jury ---

    async def get_jury_pool(self) -> list[asyncpg.Record]:
        return await self.conn.fetch(
            """
            SELECT jp.*, u.username
            FROM jury_pool jp
            JOIN users u ON jp.user_id = u.user_id
            WHERE jp.active = TRUE
            ORDER BY jp.added_at
            """
        )

    async def add_to_jury_pool(self, user_id: int, added_by: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO jury_pool (user_id, added_by)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET active = TRUE
            """,
            user_id,
            added_by,
        )

    async def remove_from_jury_pool(self, user_id: int) -> None:
        await self.conn.execute(
            "UPDATE jury_pool SET active = FALSE WHERE user_id = $1",
            user_id,
        )

    async def jury_pool_size(self) -> int:
        row = await self.conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM jury_pool WHERE active = TRUE"
        )
        return row["cnt"]

    # --- Votes Jury ---

    async def assign_jurors(
        self, bet_id: str, juror_ids: list[int], deadline: "datetime", phase: str = "validation"
    ) -> None:
        for juror_id in juror_ids:
            await self.conn.execute(
                """
                INSERT INTO jury_votes (bet_id, juror_id, vote_deadline, vote_phase)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (bet_id, juror_id, vote_phase) DO NOTHING
                """,
                bet_id,
                juror_id,
                deadline,
                phase,
            )

    async def register_replacement_juror(
        self,
        bet_id: str,
        new_juror_id: int,
        replaced_juror_id: int,
        deadline: "datetime",
        phase: str,
    ) -> None:
        # Marquer l'ancien comme remplacé
        await self.conn.execute(
            """
            UPDATE jury_votes
            SET replaced = TRUE
            WHERE bet_id = $1 AND juror_id = $2 AND vote_phase = $3
            """,
            bet_id,
            replaced_juror_id,
            phase,
        )
        # Ajouter le nouveau
        await self.conn.execute(
            """
            INSERT INTO jury_votes (bet_id, juror_id, vote_deadline, vote_phase, is_replacement, replacement_of)
            VALUES ($1, $2, $3, $4, TRUE, $5)
            ON CONFLICT (bet_id, juror_id, vote_phase) DO NOTHING
            """,
            bet_id,
            new_juror_id,
            deadline,
            phase,
            replaced_juror_id,
        )

    async def get_active_votes(self, bet_id: str, phase: str) -> list[asyncpg.Record]:
        """Votes actifs (non remplacés) pour un pari et une phase."""
        return await self.conn.fetch(
            """
            SELECT jv.*, u.username
            FROM jury_votes jv
            JOIN users u ON jv.juror_id = u.user_id
            WHERE jv.bet_id = $1 AND jv.vote_phase = $2 AND jv.replaced = FALSE
            ORDER BY jv.notified_at
            """,
            bet_id,
            phase,
        )

    async def record_vote(self, bet_id: str, juror_id: int, vote: str, phase: str) -> bool:
        """Enregistre le vote d'un juré. Retourne False si déjà voté."""
        result = await self.conn.execute(
            """
            UPDATE jury_votes
            SET vote = $1, voted_at = NOW()
            WHERE bet_id = $2 AND juror_id = $3 AND vote_phase = $4
              AND voted_at IS NULL AND replaced = FALSE
            """,
            vote,
            bet_id,
            juror_id,
            phase,
        )
        return result == "UPDATE 1"

    async def get_overdue_jurors(self) -> list[asyncpg.Record]:
        """Jurés qui n'ont pas voté avant leur deadline."""
        return await self.conn.fetch(
            """
            SELECT jv.*, b.bet_id, b.status as bet_status
            FROM jury_votes jv
            JOIN bets b ON jv.bet_id = b.bet_id
            WHERE jv.voted_at IS NULL
              AND jv.replaced = FALSE
              AND jv.vote_deadline < NOW()
              AND b.status IN ('pending_jury', 'pending_resolution')
            """
        )
