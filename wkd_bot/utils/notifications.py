"""
utils/notifications.py
Service centralisé pour tous les envois DM et posts dans les salons Discord.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, TYPE_CHECKING

import discord

from utils import embeds as E

if TYPE_CHECKING:
    pass

logger = logging.getLogger("wkd.notifications")


class NotificationService:
    """
    Envoie tous les messages (DM + salons) depuis un seul endroit.
    Les IDs de salon sont lus depuis les variables d'environnement.
    """

    def __init__(self, bot: discord.ext.commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Salons
    # ------------------------------------------------------------------

    def _channel(self, env_key: str) -> Optional[discord.TextChannel]:
        channel_id = os.getenv(env_key)
        if not channel_id:
            logger.warning(f"Variable {env_key} non définie")
            return None
        ch = self.bot.get_channel(int(channel_id))
        if not ch:
            logger.warning(f"Salon {channel_id} introuvable")
        return ch

    @property
    def ch_transactions(self):
        return self._channel("CHANNEL_TRANSACTIONS_ID")

    @property
    def ch_paris(self):
        return self._channel("CHANNEL_PARIS_ID")

    @property
    def ch_validation_paris(self):
        return self._channel("CHANNEL_VALIDATION_PARIS_ID")

    @property
    def ch_verification_tx(self):
        return self._channel("CHANNEL_VERIFICATION_TX_ID")

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    async def _safe_dm(self, user_id: int, **kwargs) -> bool:
        """Envoie un DM de façon silencieuse (ne lève pas d'exception)."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(**kwargs)
            return True
        except discord.Forbidden:
            logger.debug(f"DM impossible pour {user_id} (DMs fermés)")
        except discord.HTTPException as e:
            logger.warning(f"Erreur envoi DM {user_id}: {e}")
        return False

    async def _safe_channel_send(self, channel: Optional[discord.TextChannel], **kwargs) -> bool:
        if not channel:
            return False
        try:
            await channel.send(**kwargs)
            return True
        except discord.HTTPException as e:
            logger.warning(f"Erreur envoi salon {getattr(channel, 'id', '?')}: {e}")
        return False

    # ------------------------------------------------------------------
    # Contrats
    # ------------------------------------------------------------------

    async def send_contract_received_dm(
        self, contract: dict, creator_name: str, acceptor_id: int
    ) -> None:
        em = E.embed_contract_dm(contract, creator_name)
        await self._safe_dm(acceptor_id, embed=em)

    async def post_contract_completed(
        self, contract: dict, creator_name: str, acceptor_name: str
    ) -> None:
        em = E.embed_contract_completed(contract, creator_name, acceptor_name)
        await self._safe_channel_send(self.ch_transactions, embed=em)

    # ------------------------------------------------------------------
    # Paris
    # ------------------------------------------------------------------

    async def send_bet_received_dm(
        self, bet: dict, bettor_a_name: str, bettor_b_id: int
    ) -> None:
        em = discord.Embed(
            title=f"🎲 Pari Reçu",
            description=f"**{bettor_a_name}** vous propose un pari :",
            color=E.COLOR_BET,
        )
        em.add_field(name="Mise", value=f"**{bet['amount']} WKD** chacun", inline=True)
        em.add_field(name="ID", value=f"`{bet['bet_id']}`", inline=True)
        em.add_field(name="Condition", value=bet["condition"], inline=False)
        em.add_field(
            name="Commandes",
            value=(
                f"`/bet_accept {bet['bet_id']}`\n"
                f"`/bet_refuse {bet['bet_id']}`"
            ),
            inline=False,
        )
        await self._safe_dm(bettor_b_id, embed=em)

    async def post_bet_to_paris_channel(self, bet: dict) -> None:
        em = E.embed_bet_active(bet)
        await self._safe_channel_send(self.ch_paris, embed=em)

    async def post_bet_to_validation_channel(self, bet: dict, juror_ids: list[int]) -> None:
        em = discord.Embed(
            title="⚖️ Pari à Valider",
            color=E.COLOR_BET,
        )
        em.add_field(name="ID", value=f"`{bet['bet_id']}`", inline=True)
        em.add_field(name="Mise", value=f"{bet['amount']} WKD chacun", inline=True)
        em.add_field(name="Condition", value=bet["condition"], inline=False)
        em.add_field(
            name="Jurés désignés",
            value="\n".join([f"<@{j}>" for j in juror_ids]),
            inline=False,
        )
        await self._safe_channel_send(self.ch_validation_paris, embed=em)

    async def send_jury_assignment(
        self,
        user_id: int,
        bet_id: str,
        deadline_hours: int,
        is_replacement: bool = False,
        condition: str = "",
    ) -> None:
        em = E.embed_jury_dm(bet_id, condition, deadline_hours, is_replacement)
        await self._safe_dm(user_id, embed=em)

    async def send_jury_timeout_notification(self, juror_id: int) -> None:
        em = discord.Embed(
            title="⏱️ Vote Jury Expiré",
            description=(
                "Vous n'avez pas voté dans le délai imparti.\n"
                "Vous avez été remplacé par un autre membre du jury."
            ),
            color=E.COLOR_WARNING,
        )
        await self._safe_dm(juror_id, embed=em)

    async def send_bet_won_dm(self, winner_id: int, bet_id: str, amount: int) -> None:
        em = discord.Embed(
            title="🏆 Vous avez gagné votre pari !",
            color=E.COLOR_SUCCESS,
        )
        em.add_field(name="Pari", value=f"`{bet_id}`", inline=True)
        em.add_field(name="Gain", value=f"**+{amount} WKD**", inline=True)
        await self._safe_dm(winner_id, embed=em)

    async def post_bet_resolved(self, bet: dict, winner_id: Optional[int], total: int) -> None:
        winner_user = None
        if winner_id:
            try:
                winner_user = self.bot.get_user(winner_id) or await self.bot.fetch_user(winner_id)
            except Exception:
                pass
        em = E.embed_bet_resolved(bet["bet_id"], winner_user, total)
        await self._safe_channel_send(self.ch_paris, embed=em)
        await self._safe_channel_send(self.ch_transactions, embed=em)

    # ------------------------------------------------------------------
    # Taxes
    # ------------------------------------------------------------------

    async def send_tax_notification(
        self, user_id: int, tax_amount: int, tax_type: str
    ) -> None:
        from database.connection import acquire
        from database.repositories.users import UserRepository

        async with acquire() as conn:
            repo = UserRepository(conn)
            user = await repo.get_by_id(user_id)

        if not user:
            return

        new_balance = user["balance"]  # Déjà mis à jour dans la DB

        if tax_type == "inactive":
            em = E.embed_tax_inactive(
                tax_amount=tax_amount,
                new_balance=new_balance,
                inactive_days=7,
            )
        else:
            em = E.embed_tax_rich(tax_amount=tax_amount, new_balance=new_balance)

        await self._safe_dm(user_id, embed=em)

    async def post_tax_summary(self, results: dict) -> None:
        total_inactive = sum(e["tax"] for e in results["inactive"])
        total_rich = sum(e["tax"] for e in results["rich"])

        em = discord.Embed(
            title="📊 Taxes Hebdomadaires Appliquées",
            color=E.COLOR_TAX,
        )
        em.add_field(name="Taxes inactivité", value=f"{len(results['inactive'])} utilisateurs — {total_inactive} WKD", inline=False)
        em.add_field(name="Taxes riches", value=f"{len(results['rich'])} utilisateurs — {total_rich} WKD", inline=False)
        if results["errors"]:
            em.add_field(name="⚠️ Erreurs", value=str(len(results["errors"])), inline=True)
        await self._safe_channel_send(self.ch_transactions, embed=em)

    # ------------------------------------------------------------------
    # Airdrop
    # ------------------------------------------------------------------

    async def send_welcome_dm(self, user_id: int, airdrop_date, delay_days: int) -> None:
        em = E.embed_welcome_new_member(airdrop_date, delay_days)
        await self._safe_dm(user_id, embed=em)

    async def send_airdrop_received(self, user_id: int, amount: int) -> None:
        from database.connection import acquire
        from database.repositories.users import UserRepository

        async with acquire() as conn:
            repo = UserRepository(conn)
            user = await repo.get_by_id(user_id)

        new_balance = user["balance"] if user else amount
        em = E.embed_airdrop_received(amount, new_balance)
        await self._safe_dm(user_id, embed=em)

    # ------------------------------------------------------------------
    # Flags / Admin alerts
    # ------------------------------------------------------------------

    async def send_admin_flag_alert(self, admin_id: int, user_id: int, signals: list[str]) -> None:
        em = discord.Embed(
            title="⚠️ Comportement Suspect Détecté",
            color=E.COLOR_ADMIN,
        )
        em.add_field(name="Utilisateur", value=f"<@{user_id}>", inline=True)
        em.add_field(name="Signaux", value="\n".join(f"• {s}" for s in signals), inline=False)
        ch = self.ch_verification_tx
        if ch:
            await self._safe_channel_send(ch, embed=em)
