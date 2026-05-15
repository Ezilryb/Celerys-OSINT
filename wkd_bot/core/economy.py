"""
core/economy.py
Couche service : toute la logique métier de l'économie WKD.
Chaque opération critique est atomique (transaction PostgreSQL).
"""

from __future__ import annotations

import math
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from database.connection import transaction, acquire
from database.repositories.users import UserRepository
from database.repositories.transactions import TransactionRepository, ContractRepository, BetRepository
from database.repositories.config import ConfigRepository, AuditRepository
from database.repositories.admin_fund import AdminFundRepository

logger = logging.getLogger("wkd.economy")


class InsufficientFundsError(Exception):
    """Solde insuffisant pour l'opération."""


class BlockchainLockedError(Exception):
    """La blockchain est verrouillée par un admin."""


class CooldownError(Exception):
    """Cooldown non écoulé."""


class EligibilityError(Exception):
    """Utilisateur non éligible (banni, trop récent, etc.)."""


class ValidationError(Exception):
    """Paramètres invalides."""


# ======================================================================
# GUARDS COMMUNS
# ======================================================================

async def _check_blockchain_not_locked(conn) -> None:
    cfg = ConfigRepository(conn)
    locked, reason = await cfg.is_blockchain_locked()
    if locked:
        raise BlockchainLockedError(
            f"Blockchain verrouillée : {reason or 'maintenance en cours'}"
        )


async def _check_user_not_banned(conn, user_id: int) -> None:
    users = UserRepository(conn)
    if await users.is_banned(user_id):
        raise EligibilityError("Compte WKD suspendu.")


# ======================================================================
# ONBOARDING
# ======================================================================

async def register_new_member(
    user_id: int,
    username: str,
    account_created_at: datetime,
    server_joined_at: datetime,
) -> dict:
    """
    Crée le compte d'un nouveau membre.
    Retourne les infos du compte créé.
    """
    async with transaction() as conn:
        users = UserRepository(conn)

        if await users.exists(user_id):
            return {"already_exists": True}

        cfg = ConfigRepository(conn)
        delay_days = await cfg.get_int("airdrop_delay_days", 15)
        airdrop_date = server_joined_at + timedelta(days=delay_days)

        await users.create(
            user_id=user_id,
            username=username,
            account_created_date=account_created_at,
            server_join_date=server_joined_at,
            airdrop_eligible_date=airdrop_date,
        )

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=user_id,
            action="member_registered",
            target_id=user_id,
            details={"airdrop_date": airdrop_date.isoformat()},
        )

        return {
            "already_exists": False,
            "airdrop_date": airdrop_date,
            "delay_days": delay_days,
        }


async def process_pending_airdrops() -> list[dict]:
    """
    Distribue les airdrops aux membres éligibles.
    Appelé par le scheduler.
    Retourne la liste des utilisateurs qui ont reçu leur airdrop.
    """
    distributed = []

    async with acquire() as conn:
        users = UserRepository(conn)
        pending = await users.get_pending_airdrops()

    for row in pending:
        try:
            async with transaction() as conn:
                users = UserRepository(conn)
                cfg = ConfigRepository(conn)
                tx_repo = TransactionRepository(conn)

                airdrop_amount = await cfg.get_int("airdrop_amount", 111)

                await users.mark_airdrop_received(row["user_id"], airdrop_amount)
                await tx_repo.create(
                    from_user=None,
                    to_user=row["user_id"],
                    amount=airdrop_amount,
                    tx_type="airdrop",
                    reason="Airdrop bienvenue (15 jours)",
                )
                distributed.append(
                    {"user_id": row["user_id"], "amount": airdrop_amount}
                )
        except Exception as e:
            logger.error(f"Airdrop échoué pour {row['user_id']}: {e}")

    return distributed


# ======================================================================
# GAINS PAR MESSAGES
# ======================================================================

async def process_message_reward(user_id: int) -> Optional[dict]:
    """
    Traite le gain de WKD par message.
    Retourne un dict si un WKD est gagné, None sinon.
    Thread-safe via transaction atomique.
    """
    async with transaction() as conn:
        users = UserRepository(conn)
        cfg = ConfigRepository(conn)

        # Vérifs préliminaires
        user = await users.get_by_id_locked(user_id)
        if not user or user["banned"]:
            return None

        # Limite journalière
        daily_limit = await cfg.get_int("daily_limit", 3)
        reward_amount = await cfg.get_int("message_reward_amount", 1)
        reward_count = await cfg.get_int("message_reward_count", 222)

        today = datetime.now(timezone.utc).date()
        daily_earned = user["daily_earned"]
        last_reset = user["last_daily_reset"]

        # Reset journalier si nécessaire
        if last_reset != today:
            daily_earned = 0

        if daily_earned >= daily_limit:
            # Incrémenter quand même le compteur de messages
            await users.increment_message_count(user_id)
            return None

        # Incrémenter et vérifier le seuil
        new_count = await users.increment_message_count(user_id)

        if new_count % reward_count == 0:
            # Gagner 1 WKD !
            await users.add_daily_earned(user_id, reward_amount)

            tx_repo = TransactionRepository(conn)
            await tx_repo.create(
                from_user=None,
                to_user=user_id,
                amount=reward_amount,
                tx_type="message_reward",
                reason=f"Récompense activité ({reward_count} messages)",
            )
            return {"amount": reward_amount, "new_balance": user["balance"] + reward_amount}

        return None


# ======================================================================
# CONTRATS P2P
# ======================================================================

async def create_contract(
    creator_id: int,
    acceptor_id: int,
    amount_sent: int,
    amount_received: int,
    note: str = "",
) -> dict:
    """
    Crée un contrat P2P.
    Validations : soldes, cooldown, montants, blockchain.
    """
    # Validation des paramètres AVANT la transaction
    if amount_sent < 1:
        raise ValidationError("Montant envoyé minimum : 1 WKD")
    if amount_received <= amount_sent:
        raise ValidationError(
            f"Le montant reçu ({amount_received}) doit être supérieur à "
            f"l'envoyé ({amount_sent}) + 1 minimum."
        )
    if creator_id == acceptor_id:
        raise ValidationError("Impossible de créer un contrat avec soi-même.")

    async with transaction() as conn:
        await _check_blockchain_not_locked(conn)
        await _check_user_not_banned(conn, creator_id)
        await _check_user_not_banned(conn, acceptor_id)

        users = UserRepository(conn)
        contracts = ContractRepository(conn)

        # Vérifier solde du créateur
        creator = await users.get_by_id_locked(creator_id)
        if not creator:
            raise EligibilityError("Créateur introuvable.")
        if creator["balance"] < amount_sent:
            raise InsufficientFundsError(
                f"Solde insuffisant : {creator['balance']} WKD disponible, {amount_sent} requis."
            )

        # Cooldown pair
        if await contracts.has_active_pair_contract(creator_id, acceptor_id):
            raise CooldownError("Un contrat avec cet utilisateur est déjà en attente (cooldown 24h).")

        # Créer le contrat
        contract = await contracts.create(
            creator_id=creator_id,
            acceptor_id=acceptor_id,
            amount_sent=amount_sent,
            amount_received=amount_received,
            note=note,
        )

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=creator_id,
            action="contract_created",
            target_id=acceptor_id,
            details={"contract_id": contract["contract_id"], "sent": amount_sent, "received": amount_received},
        )

        return dict(contract)


async def accept_contract(contract_id: str, acceptor_id: int) -> dict:
    """
    Accepte et exécute un contrat P2P.
    Transaction atomique : tout réussit ou tout échoue.
    """
    async with transaction() as conn:
        await _check_blockchain_not_locked(conn)
        await _check_user_not_banned(conn, acceptor_id)

        contracts = ContractRepository(conn)
        contract = await contracts.get_by_id(contract_id)

        # Validations
        if not contract:
            raise ValidationError("Contrat introuvable.")
        if contract["status"] != "pending":
            raise ValidationError(f"Contrat {contract_id} n'est plus en attente (statut: {contract['status']}).")
        if contract["acceptor_id"] != acceptor_id:
            raise ValidationError("Ce contrat ne vous est pas destiné.")
        if contract["expires_at"] < datetime.now(timezone.utc):
            await contracts.update_status(contract_id, "expired")
            raise ValidationError("Ce contrat a expiré.")

        creator_id = contract["creator_id"]
        amount_sent = contract["amount_sent"]       # A envoie X à B
        amount_received = contract["amount_received"]  # B renvoie Y à A

        users = UserRepository(conn)

        # Verrouiller les deux comptes (ordre croissant pour éviter deadlock)
        uid_low, uid_high = sorted([creator_id, acceptor_id])
        await users.get_by_id_locked(uid_low)
        await users.get_by_id_locked(uid_high)

        creator = await users.get_by_id(creator_id)
        acceptor_user = await users.get_by_id(acceptor_id)

        if creator["balance"] < amount_sent:
            raise InsufficientFundsError(f"Le créateur n'a plus assez de WKD ({creator['balance']} < {amount_sent}).")
        if acceptor_user["balance"] < amount_received:
            raise InsufficientFundsError(f"Vous n'avez pas assez de WKD ({acceptor_user['balance']} < {amount_received}).")

        # === EXÉCUTION ATOMIQUE ===
        # A envoie amount_sent à B
        await users.update_balance(creator_id, -amount_sent)
        await users.update_balance(acceptor_id, +amount_sent)
        # B renvoie amount_received à A
        await users.update_balance(acceptor_id, -amount_received)
        await users.update_balance(creator_id, +amount_received)

        # Mise à jour stats
        await users.update_stats_spent(creator_id, amount_sent)
        await users.update_stats_spent(acceptor_id, amount_received)

        # Statut contrat
        await contracts.update_status(contract_id, "completed")

        # Log transaction
        tx_repo = TransactionRepository(conn)
        await tx_repo.create(
            from_user=creator_id,
            to_user=acceptor_id,
            amount=amount_sent,
            tx_type="contract",
            reason=f"Contrat P2P {contract_id}",
            reference_id=contract_id,
        )
        await tx_repo.create(
            from_user=acceptor_id,
            to_user=creator_id,
            amount=amount_received,
            tx_type="contract",
            reason=f"Contrat P2P {contract_id} (retour)",
            reference_id=contract_id,
        )

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=acceptor_id,
            action="contract_accepted",
            target_id=creator_id,
            details={"contract_id": contract_id},
        )

        return {
            "contract_id": contract_id,
            "creator_id": creator_id,
            "acceptor_id": acceptor_id,
            "amount_sent": amount_sent,
            "amount_received": amount_received,
        }


async def refuse_contract(contract_id: str, user_id: int) -> None:
    """Refuse un contrat en attente."""
    async with transaction() as conn:
        contracts = ContractRepository(conn)
        contract = await contracts.get_by_id(contract_id)

        if not contract:
            raise ValidationError("Contrat introuvable.")
        if contract["acceptor_id"] != user_id:
            raise ValidationError("Ce contrat ne vous est pas destiné.")
        if contract["status"] != "pending":
            raise ValidationError("Ce contrat n'est plus en attente.")

        await contracts.update_status(contract_id, "refused")

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=user_id,
            action="contract_refused",
            details={"contract_id": contract_id},
        )


# ======================================================================
# PARIS
# ======================================================================

async def create_bet(
    bettor_a: int,
    bettor_b: int,
    amount: int,
    condition: str,
) -> dict:
    """Crée un pari entre deux membres."""
    if amount < 1:
        raise ValidationError("Montant minimum : 1 WKD")
    if bettor_a == bettor_b:
        raise ValidationError("Impossible de parier avec soi-même.")
    if not condition.strip():
        raise ValidationError("La condition du pari est obligatoire.")

    async with transaction() as conn:
        await _check_blockchain_not_locked(conn)
        await _check_user_not_banned(conn, bettor_a)
        await _check_user_not_banned(conn, bettor_b)

        bets = BetRepository(conn)
        users = UserRepository(conn)

        # Vérifier solde
        user_a = await users.get_by_id_locked(bettor_a)
        if not user_a:
            raise EligibilityError("Utilisateur introuvable.")
        if user_a["balance"] < amount:
            raise InsufficientFundsError(f"Solde insuffisant : {user_a['balance']} WKD disponible.")

        # Cooldown
        if await bets.has_bet_cooldown(bettor_a):
            raise CooldownError("Vous avez déjà un pari actif ou récent (cooldown 48h).")

        bet = await bets.create_bet(bettor_a, bettor_b, amount, condition.strip())

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=bettor_a,
            action="bet_created",
            target_id=bettor_b,
            details={"bet_id": bet["bet_id"], "amount": amount},
        )

        return dict(bet)


async def accept_bet(bet_id: str, bettor_b: int) -> dict:
    """
    Accepte un pari et bloque les fonds en escrow.
    Les fonds sont déduits des deux comptes immédiatement.
    """
    async with transaction() as conn:
        await _check_blockchain_not_locked(conn)
        await _check_user_not_banned(conn, bettor_b)

        bets = BetRepository(conn)
        bet = await bets.get_bet(bet_id)

        if not bet:
            raise ValidationError("Pari introuvable.")
        if bet["bettor_b"] != bettor_b:
            raise ValidationError("Ce pari ne vous est pas destiné.")
        if bet["status"] != "pending_acceptance":
            raise ValidationError(f"Pari non disponible (statut: {bet['status']}).")
        if bet["expires_at"] < datetime.now(timezone.utc):
            await bets.update_bet_status(bet_id, "expired")
            raise ValidationError("Ce pari a expiré.")

        amount = bet["amount"]
        bettor_a = bet["bettor_a"]

        users = UserRepository(conn)

        # Lock dans l'ordre
        uid_low, uid_high = sorted([bettor_a, bettor_b])
        await users.get_by_id_locked(uid_low)
        await users.get_by_id_locked(uid_high)

        user_a = await users.get_by_id(bettor_a)
        user_b = await users.get_by_id(bettor_b)

        if user_a["balance"] < amount:
            raise InsufficientFundsError(f"Le créateur n'a plus assez de WKD.")
        if user_b["balance"] < amount:
            raise InsufficientFundsError(f"Vous n'avez pas assez de WKD ({user_b['balance']} < {amount}).")

        # Déduire les fonds (escrow)
        await users.update_balance(bettor_a, -amount)
        await users.update_balance(bettor_b, -amount)

        # Verrouiller en escrow
        await bets.lock_escrow(bet_id, amount, amount)
        await bets.update_bet_status(bet_id, "pending_jury")

        # Logs
        tx_repo = TransactionRepository(conn)
        await tx_repo.create(bettor_a, None, amount, "escrow_lock",
                             f"Escrow pari {bet_id}", bet_id)
        await tx_repo.create(bettor_b, None, amount, "escrow_lock",
                             f"Escrow pari {bet_id}", bet_id)

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=bettor_b,
            action="bet_accepted",
            target_id=bettor_a,
            details={"bet_id": bet_id, "amount": amount},
        )

        return dict(bet)


async def claim_bet_victory(bet_id: str, claimant_id: int, claim_note: str = "") -> dict:
    """Un parieur réclame la victoire."""
    async with transaction() as conn:
        bets = BetRepository(conn)
        bet = await bets.get_bet(bet_id)

        if not bet:
            raise ValidationError("Pari introuvable.")
        if bet["status"] != "active":
            raise ValidationError(f"Ce pari n'est pas actif (statut: {bet['status']}).")
        if claimant_id not in (bet["bettor_a"], bet["bettor_b"]):
            raise ValidationError("Vous ne participez pas à ce pari.")

        await bets.update_bet_status(bet_id, "pending_resolution", claim_note=claim_note)

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=claimant_id,
            action="bet_claimed",
            details={"bet_id": bet_id, "claim_note": claim_note},
        )

        return dict(bet)


async def resolve_bet(bet_id: str, winner_id: int, resolved_by: int) -> dict:
    """
    Résout un pari : libère l'escrow et attribue les gains au gagnant.
    winner_id = 0 pour remboursement (50/50 si burn impossible, ou admin annule).
    """
    async with transaction() as conn:
        bets = BetRepository(conn)
        bet = await bets.get_bet(bet_id)

        if not bet:
            raise ValidationError("Pari introuvable.")
        if bet["status"] not in ("pending_resolution", "active", "pending_jury"):
            raise ValidationError(f"Impossible de résoudre ce pari (statut: {bet['status']}).")

        escrow = await bets.get_escrow(bet_id)
        if not escrow:
            raise ValidationError("Escrow introuvable pour ce pari.")

        amount = bet["amount"]
        bettor_a = bet["bettor_a"]
        bettor_b = bet["bettor_b"]
        total = escrow["user_a_amount"] + escrow["user_b_amount"]

        users = UserRepository(conn)
        tx_repo = TransactionRepository(conn)

        if winner_id == 0:
            # Remboursement des deux parties
            await users.update_balance(bettor_a, escrow["user_a_amount"])
            await users.update_balance(bettor_b, escrow["user_b_amount"])
            await tx_repo.create(None, bettor_a, escrow["user_a_amount"], "escrow_release",
                                 f"Pari {bet_id} annulé - remboursement", bet_id)
            await tx_repo.create(None, bettor_b, escrow["user_b_amount"], "escrow_release",
                                 f"Pari {bet_id} annulé - remboursement", bet_id)
        else:
            if winner_id not in (bettor_a, bettor_b):
                raise ValidationError("Le gagnant doit être l'un des parieurs.")
            # Le gagnant reçoit le total
            await users.update_balance(winner_id, total)
            await tx_repo.create(None, winner_id, total, "bet_win",
                                 f"Gain pari {bet_id}", bet_id)

        await bets.release_escrow(bet_id, winner_id if winner_id else None)
        await bets.update_bet_status(bet_id, "resolved", winner=winner_id if winner_id else None)

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=resolved_by,
            action="bet_resolved",
            details={"bet_id": bet_id, "winner": winner_id, "total": total},
        )

        return {"bet_id": bet_id, "winner_id": winner_id, "total_paid": total}


# ======================================================================
# JURY
# ======================================================================

async def assign_jury(bet_id: str, phase: str = "validation") -> list[int]:
    """
    Tire aléatoirement 3 jurés depuis le pool (hors parieurs).
    Retourne la liste des user_ids sélectionnés.
    """
    async with transaction() as conn:
        bets = BetRepository(conn)
        bet = await bets.get_bet(bet_id)
        if not bet:
            raise ValidationError("Pari introuvable.")

        parieurs = {bet["bettor_a"], bet["bettor_b"]}
        pool = await bets.get_jury_pool()
        eligibles = [m for m in pool if m["user_id"] not in parieurs]

        if len(eligibles) < 3:
            raise ValidationError(
                f"Pool jury insuffisant : {len(eligibles)} éligibles, 3 requis."
            )

        cfg = ConfigRepository(conn)
        hours = await cfg.get_int("jury_vote_initial_hours", 24)
        deadline = datetime.now(timezone.utc) + timedelta(hours=hours)

        selected = random.sample(eligibles, 3)
        selected_ids = [m["user_id"] for m in selected]

        await bets.assign_jurors(bet_id, selected_ids, deadline, phase)

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=0,
            action="jury_assigned",
            details={"bet_id": bet_id, "jurors": selected_ids, "phase": phase},
        )

        return selected_ids


async def record_jury_vote(
    bet_id: str, juror_id: int, vote: str, phase: str
) -> dict:
    """Enregistre le vote d'un juré."""
    valid_votes = {"approve", "reject", "continue"}
    if vote not in valid_votes:
        raise ValidationError(f"Vote invalide. Options : {', '.join(valid_votes)}")

    async with transaction() as conn:
        bets = BetRepository(conn)
        success = await bets.record_vote(bet_id, juror_id, vote, phase)

        if not success:
            raise ValidationError("Vous avez déjà voté ou n'êtes pas juré pour ce pari.")

        # Vérifier si majorité atteinte (2/3)
        votes = await bets.get_active_votes(bet_id, phase)
        voted = [v for v in votes if v["voted_at"] is not None]
        vote_counts: dict[str, int] = {}
        for v in voted:
            vote_counts[v["vote"]] = vote_counts.get(v["vote"], 0) + 1

        majority_decision = None
        for decision, count in vote_counts.items():
            if count >= 2:
                majority_decision = decision
                break

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=juror_id,
            action="jury_vote",
            details={"bet_id": bet_id, "vote": vote, "phase": phase},
        )

        return {
            "vote_recorded": True,
            "majority_decision": majority_decision,
            "vote_counts": vote_counts,
        }


# ======================================================================
# TAXES AUTOMATIQUES (scheduler)
# ======================================================================

async def apply_weekly_taxes() -> dict:
    """
    Applique les deux taxes du samedi minuit :
    1. Taxe d'inactivité (-1%)
    2. Taxe des riches (-0.5% sur top 9)
    Retourne un résumé.
    """
    results = {"inactive": [], "rich": [], "errors": []}

    async with acquire() as read_conn:
        cfg = ConfigRepository(read_conn)
        inactive_rate = await cfg.get_float("inactive_tax_rate", 0.01)
        inactive_min_balance = await cfg.get_int("inactive_tax_min_balance", 222)
        inactive_days = await cfg.get_int("inactive_days_threshold", 7)
        rich_rate = await cfg.get_float("rich_tax_rate", 0.005)
        rich_top_n = await cfg.get_int("rich_tax_top_n", 9)

        users_repo = UserRepository(read_conn)
        inactive_users = await users_repo.get_inactive_users(inactive_days, inactive_min_balance)
        rich_users = await users_repo.get_top_n(rich_top_n)

    # --- Taxe inactivité ---
    for user in inactive_users:
        try:
            tax = math.floor(user["balance"] * inactive_rate)
            if tax <= 0:
                continue
            async with transaction() as conn:
                u = UserRepository(conn)
                locked = await u.get_by_id_locked(user["user_id"])
                # Re-vérifier les conditions (données fraîches)
                if locked["balance"] <= inactive_min_balance:
                    continue
                actual_tax = math.floor(locked["balance"] * inactive_rate)
                if actual_tax <= 0:
                    continue
                await u.update_balance(user["user_id"], -actual_tax)
                await u.update_stats_spent(user["user_id"], actual_tax)
                tx = TransactionRepository(conn)
                await tx.create(user["user_id"], None, actual_tax, "tax_inactive",
                                "Taxe d'inactivité hebdomadaire (1%)")
                results["inactive"].append({"user_id": user["user_id"], "tax": actual_tax})
        except Exception as e:
            results["errors"].append({"user_id": user["user_id"], "error": str(e)})

    # --- Taxe riches ---
    for user in rich_users:
        try:
            tax = math.floor(user["balance"] * rich_rate)
            if tax <= 0:
                continue
            async with transaction() as conn:
                u = UserRepository(conn)
                locked = await u.get_by_id_locked(user["user_id"])
                actual_tax = math.floor(locked["balance"] * rich_rate)
                if actual_tax <= 0:
                    continue
                await u.update_balance(user["user_id"], -actual_tax)
                await u.update_stats_spent(user["user_id"], actual_tax)
                tx = TransactionRepository(conn)
                await tx.create(user["user_id"], None, actual_tax, "tax_rich",
                                "Taxe riches hebdomadaire (0.5%)")
                results["rich"].append({"user_id": user["user_id"], "tax": actual_tax})
        except Exception as e:
            results["errors"].append({"user_id": user["user_id"], "error": str(e)})

    logger.info(
        f"Taxes appliquées — Inactivité: {len(results['inactive'])}, "
        f"Riches: {len(results['rich'])}, Erreurs: {len(results['errors'])}"
    )
    return results


# ======================================================================
# BURN
# ======================================================================

async def burn_wkd(user_id: int, amount: int, reason: str = "") -> dict:
    """Brûle des WKD (destruction volontaire)."""
    if amount < 1:
        raise ValidationError("Montant minimum à brûler : 1 WKD")

    async with transaction() as conn:
        await _check_blockchain_not_locked(conn)
        await _check_user_not_banned(conn, user_id)

        users = UserRepository(conn)
        user = await users.get_by_id_locked(user_id)

        if not user:
            raise EligibilityError("Utilisateur introuvable.")
        if user["balance"] < amount:
            raise InsufficientFundsError(f"Solde insuffisant ({user['balance']} WKD).")

        await users.update_balance(user_id, -amount)
        await users.update_stats_burned(user_id, amount)

        tx = TransactionRepository(conn)
        await tx.create(user_id, None, amount, "burn", reason or "Brûlage volontaire")

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=user_id,
            action="burn",
            details={"amount": amount, "reason": reason},
        )

        return {"amount_burned": amount, "new_balance": user["balance"] - amount}


# ======================================================================
# ADMIN : GESTION FOND
# ======================================================================

async def admin_distribute(
    admin_id: int,
    target_user_id: int,
    amount: int,
    reason: str,
) -> dict:
    """Distribue des WKD du fond admin vers un utilisateur."""
    if amount < 1:
        raise ValidationError("Montant minimum : 1 WKD")

    async with transaction() as conn:
        fund = AdminFundRepository(conn)
        f = await fund.get_locked()

        if f["balance"] < amount:
            raise InsufficientFundsError(
                f"Fond admin insuffisant ({f['balance']} WKD disponibles)."
            )
        if f["total_created"] + amount > f["max_supply"]:
            raise ValidationError(
                f"Limite maximale du fond ({f['max_supply']} WKD) atteinte."
            )

        await fund.adjust_balance(-amount)

        users = UserRepository(conn)
        await users.update_balance(target_user_id, +amount)

        tx = TransactionRepository(conn)
        await tx.create(None, target_user_id, amount, "admin_give", reason)

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=admin_id,
            action="admin_distribute",
            target_id=target_user_id,
            details={"amount": amount, "reason": reason},
        )

        new_balance = await users.get_balance(target_user_id)
        return {"amount": amount, "target": target_user_id, "new_balance": new_balance}


async def admin_rollback_transaction(
    tx_id: str, admin_id: int, reason: str
) -> dict:
    """
    Rollback d'une transaction.
    ATTENTION : Operation irréversible qui crée une transaction inverse.
    """
    if not reason.strip():
        raise ValidationError("Une raison est obligatoire pour le rollback.")

    async with transaction() as conn:
        tx_repo = TransactionRepository(conn)
        original = await tx_repo.get_by_id(tx_id)

        if not original:
            raise ValidationError(f"Transaction {tx_id} introuvable.")

        # Types non rollbackables
        non_rollbackable = {"tax_inactive", "tax_rich", "airdrop", "message_reward"}
        if original["type"] in non_rollbackable:
            raise ValidationError(
                f"Les transactions de type '{original['type']}' ne peuvent pas être rollbackées."
            )

        # Vérifier si déjà rollbacké
        rb = await conn.fetchrow(
            "SELECT 1 FROM rollbacks WHERE original_tx_id = $1", tx_id
        )
        if rb:
            raise ValidationError("Cette transaction a déjà été rollbackée.")

        users = UserRepository(conn)

        # Inverser la transaction
        if original["from_user"] and original["to_user"]:
            # Reprendre au destinataire, redonner à l'envoyeur
            dest_user = await users.get_by_id_locked(original["to_user"])
            if dest_user["balance"] < original["amount"]:
                raise InsufficientFundsError(
                    "Le destinataire n'a plus les fonds nécessaires au rollback."
                )
            await users.update_balance(original["to_user"], -original["amount"])
            await users.update_balance(original["from_user"], +original["amount"])
        elif original["to_user"] and not original["from_user"]:
            # Distribution admin / airdrop → reprendre au bénéficiaire
            dest = await users.get_by_id_locked(original["to_user"])
            if dest["balance"] < original["amount"]:
                raise InsufficientFundsError("Fonds insuffisants pour rollback.")
            await users.update_balance(original["to_user"], -original["amount"])

        # Enregistrer le rollback
        await tx_repo.create_rollback(tx_id, admin_id, reason)
        await tx_repo.create(
            from_user=original["to_user"],
            to_user=original["from_user"],
            amount=original["amount"],
            tx_type="rollback",
            reason=f"Rollback de {tx_id}: {reason}",
            reference_id=tx_id,
        )

        audit = AuditRepository(conn)
        await audit.log_action(
            actor_id=admin_id,
            action="admin_rollback",
            details={"tx_id": tx_id, "reason": reason},
        )

        return {"rolled_back": tx_id, "reason": reason}
