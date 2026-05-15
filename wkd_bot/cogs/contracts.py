"""
cogs/contracts.py
Commandes P2P : /contract_create, /contract_accept, /contract_refuse,
                /contract_pending, /contract_history
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.economy import (
    create_contract,
    accept_contract,
    refuse_contract,
    InsufficientFundsError,
    BlockchainLockedError,
    CooldownError,
    EligibilityError,
    ValidationError,
)
from database.connection import acquire
from database.repositories.transactions import ContractRepository
from utils import embeds as E
from utils.notifications import NotificationService

logger = logging.getLogger("wkd.cog.contracts")


class ContractsCog(commands.Cog, name="Contracts"):
    """Commandes de contrats P2P."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /contract_create
    # ------------------------------------------------------------------

    @app_commands.command(
        name="contract_create",
        description="Créer un contrat P2P avec un autre membre",
    )
    @app_commands.describe(
        member="Membre avec qui créer le contrat",
        send="WKD que vous envoyez",
        receive="WKD que l'autre vous renvoie (doit être > send + 1)",
        note="Note optionnelle (ex: Prêt 7 jours)",
    )
    async def contract_create(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        send: app_commands.Range[int, 1, 999999],
        receive: app_commands.Range[int, 2, 999999],
        note: Optional[str] = "",
    ):
        await interaction.response.defer(ephemeral=True)

        # Vérification préliminaire simple côté commande
        if member.id == interaction.user.id:
            await interaction.followup.send(
                embed=E.embed_error("Impossible", "Vous ne pouvez pas créer un contrat avec vous-même."),
                ephemeral=True,
            )
            return

        if member.bot:
            await interaction.followup.send(
                embed=E.embed_error("Impossible", "Vous ne pouvez pas créer un contrat avec un bot."),
                ephemeral=True,
            )
            return

        try:
            contract = await create_contract(
                creator_id=interaction.user.id,
                acceptor_id=member.id,
                amount_sent=send,
                amount_received=receive,
                note=note or "",
            )

            # Embed confirmation pour le créateur
            em = E.embed_contract_created(contract, interaction.user, member)
            await interaction.followup.send(embed=em, ephemeral=True)

            # DM au destinataire
            notif = NotificationService(self.bot)
            await notif.send_contract_received_dm(
                contract=contract,
                creator_name=interaction.user.display_name,
                acceptor_id=member.id,
            )

            logger.info(
                f"Contrat créé {contract['contract_id']}: "
                f"{interaction.user.id}→{member.id} "
                f"({send}/{receive} WKD)"
            )

        except ValidationError as e:
            await interaction.followup.send(
                embed=E.embed_error("Paramètres invalides", str(e)), ephemeral=True
            )
        except InsufficientFundsError as e:
            await interaction.followup.send(
                embed=E.embed_error("Solde insuffisant", str(e)), ephemeral=True
            )
        except CooldownError as e:
            await interaction.followup.send(
                embed=E.embed_error("Cooldown actif", str(e)), ephemeral=True
            )
        except (BlockchainLockedError, EligibilityError) as e:
            await interaction.followup.send(
                embed=E.embed_error("Action impossible", str(e)), ephemeral=True
            )
        except Exception as e:
            logger.error(f"Erreur contract_create: {e}", exc_info=True)
            await interaction.followup.send(
                embed=E.embed_error("Erreur interne", "Contactez un administrateur."),
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /contract_accept
    # ------------------------------------------------------------------

    @app_commands.command(name="contract_accept", description="Accepter un contrat P2P")
    @app_commands.describe(contract_id="ID du contrat (ex: ctr_abc123)")
    async def contract_accept(self, interaction: discord.Interaction, contract_id: str):
        await interaction.response.defer(ephemeral=True)

        # Nettoyage basique de l'input
        contract_id = contract_id.strip().lower()

        try:
            result = await accept_contract(
                contract_id=contract_id,
                acceptor_id=interaction.user.id,
            )

            em = E.embed_success(
                "Contrat Exécuté !",
                f"Le contrat `{contract_id}` a été exécuté avec succès.",
            )
            em.add_field(
                name="Vous avez renvoyé",
                value=f"**{result['amount_received']} WKD**",
                inline=True,
            )
            em.add_field(
                name="Vous avez reçu",
                value=f"**{result['amount_sent']} WKD**",
                inline=True,
            )
            await interaction.followup.send(embed=em, ephemeral=True)

            # Post dans #transactions
            notif = NotificationService(self.bot)
            creator = self.bot.get_user(result["creator_id"])
            await notif.post_contract_completed(
                contract=result,
                creator_name=creator.display_name if creator else f"<@{result['creator_id']}>",
                acceptor_name=interaction.user.display_name,
            )

            # DM au créateur
            creator_dm_em = discord.Embed(
                title=f"{E.EMOJI_OK} Contrat Accepté !",
                description=f"**{interaction.user.display_name}** a accepté votre contrat `{contract_id}`.",
                color=E.COLOR_SUCCESS,
            )
            creator_dm_em.add_field(
                name="Vous avez reçu", value=f"**{result['amount_received']} WKD**", inline=True
            )
            creator_dm_em.add_field(
                name="Vous avez envoyé", value=f"**{result['amount_sent']} WKD**", inline=True
            )
            await notif._safe_dm(result["creator_id"], embed=creator_dm_em)

        except ValidationError as e:
            await interaction.followup.send(
                embed=E.embed_error("Impossible", str(e)), ephemeral=True
            )
        except InsufficientFundsError as e:
            await interaction.followup.send(
                embed=E.embed_error("Solde insuffisant", str(e)), ephemeral=True
            )
        except (BlockchainLockedError, EligibilityError) as e:
            await interaction.followup.send(
                embed=E.embed_error("Action impossible", str(e)), ephemeral=True
            )
        except Exception as e:
            logger.error(f"Erreur contract_accept {contract_id}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=E.embed_error("Erreur interne", "Contactez un administrateur."),
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /contract_refuse
    # ------------------------------------------------------------------

    @app_commands.command(name="contract_refuse", description="Refuser un contrat P2P")
    @app_commands.describe(contract_id="ID du contrat")
    async def contract_refuse(self, interaction: discord.Interaction, contract_id: str):
        await interaction.response.defer(ephemeral=True)
        contract_id = contract_id.strip().lower()

        try:
            await refuse_contract(contract_id, interaction.user.id)

            await interaction.followup.send(
                embed=E.embed_success("Contrat Refusé", f"Vous avez refusé le contrat `{contract_id}`."),
                ephemeral=True,
            )

            # DM au créateur
            async with acquire() as conn:
                repo = ContractRepository(conn)
                contract = await repo.get_by_id(contract_id)

            if contract:
                notif = NotificationService(self.bot)
                dm_em = discord.Embed(
                    title=f"{E.EMOJI_ERR} Contrat Refusé",
                    description=(
                        f"**{interaction.user.display_name}** a refusé votre contrat `{contract_id}`."
                    ),
                    color=E.COLOR_ERROR,
                )
                await notif._safe_dm(contract["creator_id"], embed=dm_em)

        except ValidationError as e:
            await interaction.followup.send(
                embed=E.embed_error("Impossible", str(e)), ephemeral=True
            )
        except Exception as e:
            logger.error(f"Erreur contract_refuse {contract_id}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=E.embed_error("Erreur interne", "Contactez un administrateur."),
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /contract_pending
    # ------------------------------------------------------------------

    @app_commands.command(name="contract_pending", description="Voir vos contrats en attente")
    async def contract_pending(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with acquire() as conn:
            repo = ContractRepository(conn)
            contracts = await repo.get_pending_for_user(interaction.user.id)

        if not contracts:
            await interaction.followup.send(
                embed=E.embed_warning("Aucun contrat", "Vous n'avez aucun contrat en attente."),
                ephemeral=True,
            )
            return

        em = discord.Embed(
            title=f"{E.EMOJI_CTR} Contrats en attente ({len(contracts)})",
            color=E.COLOR_CONTRACT,
        )
        for c in contracts[:10]:
            direction = "📤 Envoyé" if c["creator_id"] == interaction.user.id else "📥 Reçu"
            value = (
                f"Envoi: **{c['amount_sent']} WKD** | Retour: **{c['amount_received']} WKD**\n"
                f"Expire <t:{int(c['expires_at'].timestamp())}:R>"
            )
            em.add_field(
                name=f"`{c['contract_id']}` — {direction}",
                value=value,
                inline=False,
            )
        em.set_footer(**E._footer())
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------
    # /contract_history
    # ------------------------------------------------------------------

    @app_commands.command(name="contract_history", description="Voir l'historique de vos contrats")
    async def contract_history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with acquire() as conn:
            repo = ContractRepository(conn)
            contracts = await repo.get_history_for_user(interaction.user.id, limit=10)

        if not contracts:
            await interaction.followup.send(
                embed=E.embed_warning("Aucun contrat", "Vous n'avez aucun contrat dans l'historique."),
                ephemeral=True,
            )
            return

        em = discord.Embed(
            title=f"{E.EMOJI_CTR} Historique Contrats",
            color=E.COLOR_CONTRACT,
        )
        status_emoji = {
            "completed": "✅",
            "refused": "❌",
            "expired": "⏰",
            "pending": "🕐",
            "cancelled": "🚫",
        }
        for c in contracts:
            emoji = status_emoji.get(c["status"], "❓")
            other = c["acceptor_name"] if c["creator_id"] == interaction.user.id else c["creator_name"]
            em.add_field(
                name=f"`{c['contract_id']}` {emoji}",
                value=f"Avec: **{other}** | {c['amount_sent']}/{c['amount_received']} WKD | <t:{int(c['created_at'].timestamp())}:d>",
                inline=False,
            )
        em.set_footer(**E._footer())
        await interaction.followup.send(embed=em, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ContractsCog(bot))
