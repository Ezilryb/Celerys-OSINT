"""
cogs/wallet.py
Commandes utilisateur : /balance, /leaderboard, /history, /burn
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database.connection import acquire
from database.repositories.users import UserRepository
from database.repositories.transactions import TransactionRepository
from core.economy import burn_wkd, InsufficientFundsError, BlockchainLockedError, ValidationError
from core.security import rate_limiter
from utils import embeds as E

logger = logging.getLogger("wkd.cog.wallet")


class WalletCog(commands.Cog, name="Wallet"):
    """Commandes de portefeuille WKD."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /balance
    # ------------------------------------------------------------------

    @app_commands.command(name="balance", description="Voir votre solde WKD (ou celui d'un autre membre)")
    @app_commands.describe(member="Membre dont vous souhaitez voir le solde (optionnel)")
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user

        async with acquire() as conn:
            repo = UserRepository(conn)
            user = await repo.get_by_id(target.id)

        if not user:
            await interaction.followup.send(
                embed=E.embed_error("Compte introuvable", "Ce membre n'a pas encore de compte WKD."),
                ephemeral=True,
            )
            return

        # Calculer le rang
        async with acquire() as conn:
            repo = UserRepository(conn)
            leaders = await repo.get_leaderboard(limit=9)

        rank = None
        for i, row in enumerate(leaders):
            if row["user_id"] == target.id:
                rank = i + 1
                break

        em = E.embed_balance(
            user=target,
            balance=user["balance"],
            total_earned=user["total_earned"],
            total_spent=user["total_spent"],
            total_burned=user["total_burned"],
            rank=rank,
        )
        await interaction.followup.send(embed=em, ephemeral=(member is None))

    # ------------------------------------------------------------------
    # /leaderboard
    # ------------------------------------------------------------------

    @app_commands.command(name="leaderboard", description="Top 9 des plus riches WKD")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with acquire() as conn:
            repo = UserRepository(conn)
            leaders = await repo.get_leaderboard(limit=9)

        entries = [dict(row) for row in leaders]
        em = E.embed_leaderboard(entries)
        await interaction.followup.send(embed=em)

    # ------------------------------------------------------------------
    # /history
    # ------------------------------------------------------------------

    @app_commands.command(name="history", description="Voir votre historique de transactions")
    @app_commands.describe(
        type="Filtrer par type (contract, bet_win, tax_inactive, airdrop...)",
        limit="Nombre de transactions à afficher (max 20)",
    )
    async def history(
        self,
        interaction: discord.Interaction,
        type: Optional[str] = None,
        limit: app_commands.Range[int, 1, 20] = 15,
    ):
        await interaction.response.defer(ephemeral=True)

        async with acquire() as conn:
            repo = TransactionRepository(conn)
            txs = await repo.get_user_history(interaction.user.id, limit=limit, tx_type=type)

        em = E.embed_history(interaction.user, txs)
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------
    # /burn
    # ------------------------------------------------------------------

    @app_commands.command(name="burn", description="Brûler des WKD (destruction volontaire)")
    @app_commands.describe(
        amount="Montant de WKD à brûler",
        reason="Raison du brûlage (optionnel)",
    )
    async def burn(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 999999],
        reason: Optional[str] = "",
    ):
        await interaction.response.defer(ephemeral=True)

        # Rate limit : 1 burn par 5 minutes
        if await rate_limiter.is_limited(interaction.user.id, "burn"):
            remaining = await rate_limiter.remaining(interaction.user.id, "burn")
            await interaction.followup.send(
                embed=E.embed_error("Cooldown", f"Attendez encore {remaining:.0f}s avant de brûler à nouveau."),
                ephemeral=True,
            )
            return

        try:
            result = await burn_wkd(interaction.user.id, amount, reason or "Brûlage volontaire")
            await rate_limiter.set_limit(interaction.user.id, "burn", 300)

            em = discord.Embed(
                title=f"🔥 {amount} WKD Brûlés",
                color=E.COLOR_BURN,
            )
            em.add_field(name="Montant détruit", value=f"**{amount} WKD**", inline=True)
            em.add_field(name="Nouveau solde", value=f"**{result['new_balance']:,} WKD**", inline=True)
            if reason:
                em.add_field(name="Raison", value=reason, inline=False)
            em.set_footer(**E._footer())

            await interaction.followup.send(embed=em, ephemeral=True)

            # Post public dans #transactions
            pub_em = discord.Embed(
                title=f"🔥 Brûlage WKD",
                color=E.COLOR_BURN,
            )
            pub_em.add_field(name="Utilisateur", value=interaction.user.mention, inline=True)
            pub_em.add_field(name="Montant", value=f"{amount} WKD", inline=True)
            if reason:
                pub_em.add_field(name="Raison", value=reason, inline=False)
            pub_em.set_footer(**E._footer("Blockchain WKD"))

            from utils.notifications import NotificationService
            notif = NotificationService(self.bot)
            if notif.ch_transactions:
                await notif.ch_transactions.send(embed=pub_em)

        except (InsufficientFundsError, BlockchainLockedError, ValidationError) as e:
            await interaction.followup.send(
                embed=E.embed_error("Impossible", str(e)),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Erreur burn {interaction.user.id}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=E.embed_error("Erreur", "Une erreur interne s'est produite."),
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(WalletCog(bot))
