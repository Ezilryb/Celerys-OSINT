"""
cogs/events.py
Événements Discord : on_member_join, on_message (récompenses).
"""

from __future__ import annotations

import logging
import os

import discord
from discord.ext import commands

from core.economy import register_new_member, process_message_reward
from core.security import message_tracker, fraud_detector, check_account_age
from database.connection import acquire
from database.repositories.users import UserRepository
from database.repositories.config import ConfigRepository
from utils.notifications import NotificationService

logger = logging.getLogger("wkd.cog.events")


class EventsCog(commands.Cog, name="Events"):
    """Gestionnaire des événements Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # on_member_join
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Crée le compte WKD et envoie le DM de bienvenue."""
        if member.bot:
            return

        # Vérification âge minimal du compte Discord
        account_age_ok, age_days = check_account_age(
            member.created_at, min_days=1  # Min très faible ici, la vraie vérif est pour airdrop
        )

        try:
            result = await register_new_member(
                user_id=member.id,
                username=member.display_name,
                account_created_at=member.created_at,
                server_joined_at=member.joined_at or discord.utils.utcnow(),
            )

            if result["already_exists"]:
                return  # Re-join, compte déjà existant

            # DM de bienvenue
            notif = NotificationService(self.bot)
            await notif.send_welcome_dm(
                user_id=member.id,
                airdrop_date=result["airdrop_date"],
                delay_days=result["delay_days"],
            )

            # Signaux fraude éventuels
            signals = await fraud_detector.detect_rapid_account_creation(
                new_user_id=member.id,
                account_created_at=member.created_at,
                server_joined_at=member.joined_at or discord.utils.utcnow(),
            )

            if signals:
                logger.warning(f"Signaux suspects pour nouveau membre {member.id}: {signals}")
                admin_id = self._get_primary_admin_id()
                if admin_id:
                    await notif.send_admin_flag_alert(admin_id, member.id, signals)

            logger.info(
                f"Nouveau membre enregistré : {member.display_name} ({member.id}), "
                f"airdrop le {result['airdrop_date'].date()}"
            )

        except Exception as e:
            logger.error(f"Erreur on_member_join {member.id}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # on_message
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Traite les messages pour les récompenses WKD."""
        if message.author.bot:
            return
        if not message.guild:
            return
        if not isinstance(message.author, discord.Member):
            return

        user_id = message.author.id

        # 1. S'assurer que le compte existe (peut rejoindre sans trigger on_member_join en cas de restart)
        async with acquire() as conn:
            users_repo = UserRepository(conn)
            exists = await users_repo.exists(user_id)

        if not exists:
            try:
                await register_new_member(
                    user_id=user_id,
                    username=message.author.display_name,
                    account_created_at=message.author.created_at,
                    server_joined_at=message.author.joined_at or discord.utils.utcnow(),
                )
            except Exception as e:
                logger.error(f"Erreur création compte on_message {user_id}: {e}")
                return

        # 2. Vérifier cooldown anti-spam (en RAM)
        async with acquire() as conn:
            cfg = ConfigRepository(conn)
            cooldown_seconds = await cfg.get_int("message_cooldown_seconds", 60)

        if not await message_tracker.can_count_message(user_id):
            return  # Message dans le cooldown — non comptabilisé

        # 3. Traiter la récompense (si applicable)
        try:
            reward = await process_message_reward(user_id)
            if reward:
                logger.debug(
                    f"Récompense message: {message.author.display_name} +{reward['amount']} WKD"
                )
                # Notification discrète (DM non intrusif)
                try:
                    await message.author.send(
                        embed=discord.Embed(
                            title=f"🔥 +{reward['amount']} WKD !",
                            description=(
                                f"Vous avez gagné **{reward['amount']} WKD** grâce à votre activité !\n"
                                f"Solde actuel : **{reward['new_balance']:,} WKD**"
                            ),
                            color=0xFF4500,
                        )
                    )
                except discord.Forbidden:
                    pass  # DMs fermés - silencieux

        except Exception as e:
            logger.error(f"Erreur process_message_reward {user_id}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _get_primary_admin_id(self) -> int | None:
        raw = os.getenv("ADMIN_IDS", "")
        parts = [p.strip() for p in raw.split(",") if p.strip().isdigit()]
        return int(parts[0]) if parts else None


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
