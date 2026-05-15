"""
cogs/bets.py
Commandes paris : /bet_create, /bet_accept, /bet_refuse,
                  /bet_claim, /bet_vote, /bet_active, /bet_mine,
                  /bet_history, /bet_status
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.economy import (
    create_bet,
    accept_bet,
    claim_bet_victory,
    assign_jury,
    record_jury_vote,
    InsufficientFundsError,
    BlockchainLockedError,
    CooldownError,
    EligibilityError,
    ValidationError,
)
from database.connection import acquire
from database.repositories.transactions import BetRepository
from utils import embeds as E
from utils.notifications import NotificationService

logger = logging.getLogger("wkd.cog.bets")


class BetsCog(commands.Cog, name="Bets"):
    """Commandes de paris WKD."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /bet_create
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_create", description="Créer un pari avec un autre membre")
    @app_commands.describe(
        member="Membre avec qui parier",
        amount="Mise de chaque parieur (WKD)",
        condition="Condition du pari (ex: BTC > 100k au 01/06)",
    )
    async def bet_create(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 999999],
        condition: str,
    ):
        await interaction.response.defer(ephemeral=True)

        if member.bot or member.id == interaction.user.id:
            await interaction.followup.send(
                embed=E.embed_error("Impossible", "Cible invalide."), ephemeral=True
            )
            return

        try:
            bet = await create_bet(
                bettor_a=interaction.user.id,
                bettor_b=member.id,
                amount=amount,
                condition=condition,
            )

            em = E.embed_bet_created(bet, interaction.user, member)
            await interaction.followup.send(embed=em, ephemeral=True)

            # DM au destinataire
            notif = NotificationService(self.bot)
            await notif.send_bet_received_dm(bet, interaction.user.display_name, member.id)

            logger.info(f"Pari créé {bet['bet_id']}: {interaction.user.id}↔{member.id} {amount} WKD")

        except ValidationError as e:
            await interaction.followup.send(embed=E.embed_error("Invalide", str(e)), ephemeral=True)
        except InsufficientFundsError as e:
            await interaction.followup.send(embed=E.embed_error("Solde insuffisant", str(e)), ephemeral=True)
        except CooldownError as e:
            await interaction.followup.send(embed=E.embed_error("Cooldown", str(e)), ephemeral=True)
        except (BlockchainLockedError, EligibilityError) as e:
            await interaction.followup.send(embed=E.embed_error("Action impossible", str(e)), ephemeral=True)
        except Exception as e:
            logger.error(f"Erreur bet_create: {e}", exc_info=True)
            await interaction.followup.send(embed=E.embed_error("Erreur interne", "Contactez un admin."), ephemeral=True)

    # ------------------------------------------------------------------
    # /bet_accept
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_accept", description="Accepter un pari")
    @app_commands.describe(bet_id="ID du pari (ex: bet_abc123)")
    async def bet_accept(self, interaction: discord.Interaction, bet_id: str):
        await interaction.response.defer(ephemeral=True)
        bet_id = bet_id.strip().lower()

        try:
            result = await accept_bet(bet_id, interaction.user.id)

            await interaction.followup.send(
                embed=E.embed_success(
                    "Pari Accepté !",
                    f"Pari `{bet_id}` accepté. **{result['amount']} WKD** bloqués en escrow.\n"
                    f"Le jury va être désigné...",
                ),
                ephemeral=True,
            )

            # Assigner le jury
            notif = NotificationService(self.bot)
            try:
                juror_ids = await assign_jury(bet_id, "validation")

                # DMs aux jurés
                async with acquire() as conn:
                    b_repo = BetRepository(conn)
                    bet_full = await b_repo.get_bet(bet_id)

                for juror_id in juror_ids:
                    await notif.send_jury_assignment(
                        user_id=juror_id,
                        bet_id=bet_id,
                        deadline_hours=24,
                        condition=bet_full["condition"] if bet_full else "",
                    )

                # Post dans #validation-paris
                await notif.post_bet_to_validation_channel(result, juror_ids)

            except ValidationError as e:
                # Pool jury insuffisant
                logger.warning(f"Impossible d'assigner jury pour {bet_id}: {e}")
                await notif._safe_channel_send(
                    notif.ch_validation_paris,
                    content=f"⚠️ Pool jury insuffisant pour le pari `{bet_id}` — intervention admin requise.",
                )

            # Post dans #paris
            async with acquire() as conn:
                b_repo = BetRepository(conn)
                bet_data = await b_repo.get_bet(bet_id)
            if bet_data:
                await notif.post_bet_to_paris_channel(bet_data)

            # DM au créateur du pari
            dm_em = discord.Embed(
                title=f"🎲 Votre pari a été accepté !",
                description=f"**{interaction.user.display_name}** a accepté votre pari `{bet_id}`.\nLe jury va valider sous 24h.",
                color=E.COLOR_BET,
            )
            await notif._safe_dm(result["bettor_a"], embed=dm_em)

        except ValidationError as e:
            await interaction.followup.send(embed=E.embed_error("Impossible", str(e)), ephemeral=True)
        except InsufficientFundsError as e:
            await interaction.followup.send(embed=E.embed_error("Solde insuffisant", str(e)), ephemeral=True)
        except (BlockchainLockedError, EligibilityError) as e:
            await interaction.followup.send(embed=E.embed_error("Action impossible", str(e)), ephemeral=True)
        except Exception as e:
            logger.error(f"Erreur bet_accept {bet_id}: {e}", exc_info=True)
            await interaction.followup.send(embed=E.embed_error("Erreur interne", "Contactez un admin."), ephemeral=True)

    # ------------------------------------------------------------------
    # /bet_refuse
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_refuse", description="Refuser un pari")
    @app_commands.describe(bet_id="ID du pari")
    async def bet_refuse(self, interaction: discord.Interaction, bet_id: str):
        await interaction.response.defer(ephemeral=True)
        bet_id = bet_id.strip().lower()

        async with acquire() as conn:
            repo = BetRepository(conn)
            bet = await repo.get_bet(bet_id)

        if not bet:
            await interaction.followup.send(embed=E.embed_error("Introuvable", "Pari introuvable."), ephemeral=True)
            return
        if bet["bettor_b"] != interaction.user.id:
            await interaction.followup.send(embed=E.embed_error("Interdit", "Ce pari ne vous est pas destiné."), ephemeral=True)
            return
        if bet["status"] != "pending_acceptance":
            await interaction.followup.send(embed=E.embed_error("Impossible", f"Statut: {bet['status']}"), ephemeral=True)
            return

        async with acquire() as conn:
            repo = BetRepository(conn)
            await repo.update_bet_status(bet_id, "refused")

        await interaction.followup.send(
            embed=E.embed_success("Pari Refusé", f"Vous avez refusé le pari `{bet_id}`."),
            ephemeral=True,
        )

        notif = NotificationService(self.bot)
        dm_em = discord.Embed(
            title=f"{E.EMOJI_ERR} Pari Refusé",
            description=f"**{interaction.user.display_name}** a refusé votre pari `{bet_id}`.",
            color=E.COLOR_ERROR,
        )
        await notif._safe_dm(bet["bettor_a"], embed=dm_em)

    # ------------------------------------------------------------------
    # /bet_claim
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_claim", description="Réclamer la victoire d'un pari")
    @app_commands.describe(
        bet_id="ID du pari",
        note="Preuve / explication de votre victoire",
    )
    async def bet_claim(
        self,
        interaction: discord.Interaction,
        bet_id: str,
        note: Optional[str] = "",
    ):
        await interaction.response.defer(ephemeral=True)
        bet_id = bet_id.strip().lower()

        try:
            result = await claim_bet_victory(bet_id, interaction.user.id, note or "")

            await interaction.followup.send(
                embed=E.embed_success(
                    "Réclamation Envoyée",
                    f"Votre réclamation pour le pari `{bet_id}` a été soumise au jury.",
                ),
                ephemeral=True,
            )

            # Assigner jury pour résolution
            notif = NotificationService(self.bot)
            try:
                juror_ids = await assign_jury(bet_id, "resolution")
                async with acquire() as conn:
                    b_repo = BetRepository(conn)
                    bet_full = await b_repo.get_bet(bet_id)

                for juror_id in juror_ids:
                    await notif.send_jury_assignment(
                        user_id=juror_id,
                        bet_id=bet_id,
                        deadline_hours=24,
                        condition=f"Réclamation : {note or 'pas de note'}\n\nCondition originale: {bet_full['condition'] if bet_full else '?'}",
                    )
            except ValidationError as e:
                logger.warning(f"Jury insuffisant pour résolution {bet_id}: {e}")

        except ValidationError as e:
            await interaction.followup.send(embed=E.embed_error("Impossible", str(e)), ephemeral=True)
        except Exception as e:
            logger.error(f"Erreur bet_claim {bet_id}: {e}", exc_info=True)
            await interaction.followup.send(embed=E.embed_error("Erreur interne", "Contactez un admin."), ephemeral=True)

    # ------------------------------------------------------------------
    # /bet_vote
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_vote", description="Voter en tant que juré")
    @app_commands.describe(
        bet_id="ID du pari",
        decision="Votre décision",
        phase="Phase du vote",
    )
    @app_commands.choices(
        decision=[
            app_commands.Choice(name="✅ Approuver", value="approve"),
            app_commands.Choice(name="❌ Rejeter", value="reject"),
            app_commands.Choice(name="🔄 Continuer", value="continue"),
        ],
        phase=[
            app_commands.Choice(name="Validation", value="validation"),
            app_commands.Choice(name="Résolution", value="resolution"),
        ],
    )
    async def bet_vote(
        self,
        interaction: discord.Interaction,
        bet_id: str,
        decision: str,
        phase: str = "validation",
    ):
        await interaction.response.defer(ephemeral=True)
        bet_id = bet_id.strip().lower()

        try:
            result = await record_jury_vote(bet_id, interaction.user.id, decision, phase)

            em = E.embed_success(
                "Vote Enregistré",
                f"Vote **{decision}** pour le pari `{bet_id}` (phase: {phase}).",
            )

            if result["majority_decision"]:
                em.add_field(
                    name="🏁 Majorité atteinte !",
                    value=f"Décision : **{result['majority_decision']}**\nUn admin peut maintenant finaliser.",
                    inline=False,
                )

            await interaction.followup.send(embed=em, ephemeral=True)

            # Notifier le salon de validation
            notif = NotificationService(self.bot)
            if notif.ch_validation_paris and result["majority_decision"]:
                notif_em = discord.Embed(
                    title=f"⚖️ Majorité Jury Atteinte",
                    description=f"Pari `{bet_id}` — Décision : **{result['majority_decision']}**",
                    color=E.COLOR_BET,
                )
                notif_em.add_field(
                    name="Votes",
                    value="\n".join([f"{v}: {c}" for v, c in result["vote_counts"].items()]),
                )
                await notif.ch_validation_paris.send(embed=notif_em)

        except ValidationError as e:
            await interaction.followup.send(embed=E.embed_error("Impossible", str(e)), ephemeral=True)
        except Exception as e:
            logger.error(f"Erreur bet_vote {bet_id}: {e}", exc_info=True)
            await interaction.followup.send(embed=E.embed_error("Erreur interne", "Contactez un admin."), ephemeral=True)

    # ------------------------------------------------------------------
    # /bet_active
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_active", description="Voir les paris actifs")
    async def bet_active(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with acquire() as conn:
            repo = BetRepository(conn)
            bets = await repo.get_active_bets()

        if not bets:
            await interaction.followup.send(
                embed=E.embed_warning("Aucun pari", "Aucun pari actif en ce moment."),
            )
            return

        em = discord.Embed(
            title=f"{E.EMOJI_BET} Paris Actifs ({len(bets)})",
            color=E.COLOR_BET,
        )
        for b in bets[:10]:
            status_map = {"active": "🟢", "pending_resolution": "🔶", "pending_jury": "⚖️"}
            icon = status_map.get(b["status"], "❓")
            em.add_field(
                name=f"`{b['bet_id']}` {icon}",
                value=(
                    f"**{b['bettor_a_name']}** vs **{b['bettor_b_name']}**\n"
                    f"Mise: {b['amount']} WKD chacun | "
                    f"Condition: {b['condition'][:60]}{'...' if len(b['condition']) > 60 else ''}"
                ),
                inline=False,
            )
        em.set_footer(**E._footer())
        await interaction.followup.send(embed=em)

    # ------------------------------------------------------------------
    # /bet_mine
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_mine", description="Voir vos paris")
    async def bet_mine(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with acquire() as conn:
            repo = BetRepository(conn)
            bets = await repo.get_user_bets(interaction.user.id)

        if not bets:
            await interaction.followup.send(
                embed=E.embed_warning("Aucun pari", "Vous n'avez aucun pari."), ephemeral=True
            )
            return

        em = discord.Embed(
            title=f"{E.EMOJI_BET} Mes Paris",
            color=E.COLOR_BET,
        )
        for b in bets[:10]:
            other = b["bettor_b_name"] if b["bettor_a"] == interaction.user.id else b["bettor_a_name"]
            em.add_field(
                name=f"`{b['bet_id']}` — {b['status']}",
                value=f"vs **{other}** | {b['amount']} WKD | <t:{int(b['created_at'].timestamp())}:d>",
                inline=False,
            )
        em.set_footer(**E._footer())
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------
    # /bet_status
    # ------------------------------------------------------------------

    @app_commands.command(name="bet_status", description="Détails d'un pari")
    @app_commands.describe(bet_id="ID du pari")
    async def bet_status(self, interaction: discord.Interaction, bet_id: str):
        await interaction.response.defer(ephemeral=True)
        bet_id = bet_id.strip().lower()

        async with acquire() as conn:
            repo = BetRepository(conn)
            bet = await repo.get_bet(bet_id)
            votes = await repo.get_active_votes(bet_id, "validation")

        if not bet:
            await interaction.followup.send(
                embed=E.embed_error("Introuvable", f"Pari `{bet_id}` introuvable."), ephemeral=True
            )
            return

        em = discord.Embed(
            title=f"{E.EMOJI_BET} Détails Pari `{bet_id}`",
            color=E.COLOR_BET,
        )
        em.add_field(name="Parieur A", value=f"<@{bet['bettor_a']}>", inline=True)
        em.add_field(name="Parieur B", value=f"<@{bet['bettor_b']}>", inline=True)
        em.add_field(name="Mise", value=f"{bet['amount']} WKD chacun", inline=True)
        em.add_field(name="Condition", value=bet["condition"], inline=False)
        em.add_field(name="Statut", value=bet["status"].replace("_", " ").title(), inline=True)
        if bet.get("winner"):
            em.add_field(name="Gagnant", value=f"<@{bet['winner']}>", inline=True)
        if votes:
            jury_info = "\n".join([
                f"• <@{v['juror_id']}> — {'✅ Voté' if v['voted_at'] else '⏳ En attente'}"
                for v in votes
            ])
            em.add_field(name=f"Jury ({len(votes)} jurés)", value=jury_info, inline=False)
        em.set_footer(**E._footer())
        await interaction.followup.send(embed=em, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BetsCog(bot))
