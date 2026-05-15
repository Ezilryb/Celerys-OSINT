"""
cogs/admin.py
Commandes admin : gestion fond, paris, jury, sécurité, config, monitoring.
Toutes les actions critiques nécessitent une confirmation 2FA.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.economy import (
    admin_distribute,
    admin_rollback_transaction,
    resolve_bet,
    InsufficientFundsError,
    ValidationError,
)
from core.security import admin_confirmation, CRITICAL_ADMIN_ACTIONS
from database.connection import acquire, transaction
from database.repositories.users import UserRepository
from database.repositories.transactions import BetRepository, ContractRepository
from database.repositories.config import ConfigRepository, AuditRepository
from database.repositories.admin_fund import AdminFundRepository
from utils import embeds as E
from utils.notifications import NotificationService

logger = logging.getLogger("wkd.cog.admin")

# Récupération de l'ID admin depuis l'environnement
def get_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_admin():
    """Check d'autorisation : uniquement les IDs dans ADMIN_IDS."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in get_admin_ids():
            return True
        await interaction.response.send_message(
            embed=E.embed_error("Accès refusé", "Vous n'êtes pas administrateur WKD."),
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


def is_mod_or_admin():
    """Check modérateur ou admin."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in get_admin_ids():
            return True
        mod_role_id = os.getenv("MOD_ROLE_ID")
        if mod_role_id and isinstance(interaction.user, discord.Member):
            if any(r.id == int(mod_role_id) for r in interaction.user.roles):
                return True
        await interaction.response.send_message(
            embed=E.embed_error("Accès refusé", "Rôle insuffisant."),
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


class AdminCog(commands.Cog, name="Admin"):
    """Commandes réservées aux administrateurs WKD."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # HELPER : demander confirmation 2FA
    # ------------------------------------------------------------------

    async def _require_confirmation(self, interaction: discord.Interaction, action: str) -> bool:
        """
        Envoie un code en DM et attend la confirmation.
        Retourne True si confirmé, False sinon.
        """
        if action not in CRITICAL_ADMIN_ACTIONS:
            return True

        code = await admin_confirmation.generate_code(interaction.user.id)

        try:
            await interaction.user.send(
                embed=discord.Embed(
                    title="🔐 Confirmation Requise",
                    description=(
                        f"Code de confirmation pour **{action}** :\n"
                        f"## `{code}`\n"
                        f"Répondez avec ce code dans **30 secondes**."
                    ),
                    color=E.COLOR_ADMIN,
                )
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=E.embed_error(
                    "DMs fermés",
                    "Ouvrez vos DMs pour recevoir le code de confirmation.",
                ),
                ephemeral=True,
            )
            return False

        await interaction.followup.send(
            embed=E.embed_warning(
                "Confirmation Envoyée",
                "Un code de confirmation a été envoyé en DM.\n"
                "Répondez dans ce canal avec le code dans 30s.",
            ),
            ephemeral=True,
        )

        def check(m: discord.Message):
            return m.author.id == interaction.user.id and isinstance(m.channel, discord.DMChannel)

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30.0)
            if await admin_confirmation.verify_code(interaction.user.id, msg.content.strip()):
                return True
            await interaction.user.send("❌ Code incorrect.")
            return False
        except Exception:
            await admin_confirmation.cancel(interaction.user.id)
            return False

    # ------------------------------------------------------------------
    # /admin_stats
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_stats", description="[ADMIN] Statistiques globales du système")
    @is_admin()
    async def admin_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with acquire() as conn:
            fund_repo = AdminFundRepository(conn)
            fund = await fund_repo.get()

            cfg = ConfigRepository(conn)
            blockchain_locked, lock_reason = await cfg.is_blockchain_locked()

            total_balance = await conn.fetchval(
                "SELECT COALESCE(SUM(balance), 0) FROM users WHERE banned = FALSE"
            )
            active_users = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE banned = FALSE AND last_message > NOW() - INTERVAL '7 days'"
            )
            pending_contracts = await conn.fetchval(
                "SELECT COUNT(*) FROM contracts WHERE status = 'pending'"
            )
            active_bets = await conn.fetchval(
                "SELECT COUNT(*) FROM bets WHERE status IN ('active', 'pending_resolution', 'pending_jury')"
            )
            jury_pool_size = await conn.fetchval(
                "SELECT COUNT(*) FROM jury_pool WHERE active = TRUE"
            )
            pending_airdrops = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE airdrop_received = FALSE AND airdrop_eligible_date IS NOT NULL AND airdrop_eligible_date <= NOW()"
            )

        stats = {
            "fund_balance": fund["balance"],
            "total_created": fund["total_created"],
            "total_balance": total_balance,
            "active_users": active_users,
            "pending_contracts": pending_contracts,
            "active_bets": active_bets,
            "jury_pool_size": jury_pool_size,
            "pending_airdrops": pending_airdrops,
            "blockchain_locked": blockchain_locked,
        }
        await interaction.followup.send(embed=E.embed_admin_stats(stats), ephemeral=True)

    # ------------------------------------------------------------------
    # /admin_fund_balance
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_fund_balance", description="[ADMIN] Voir le fond créateur")
    @is_admin()
    async def admin_fund_balance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with acquire() as conn:
            fund = await AdminFundRepository(conn).get()

        em = discord.Embed(title=f"{E.EMOJI_ADMIN} Fond Créateur WKD", color=E.COLOR_ADMIN)
        em.add_field(name="Solde actuel", value=f"**{fund['balance']:,} WKD**", inline=True)
        em.add_field(name="Total créé", value=f"{fund['total_created']:,} WKD", inline=True)
        em.add_field(name="Total distribué", value=f"{fund['total_distributed']:,} WKD", inline=True)
        em.add_field(name="Limite max", value=f"{fund['max_supply']:,} WKD", inline=True)
        remaining = fund["max_supply"] - fund["total_created"]
        em.add_field(name="Marge création restante", value=f"{remaining:,} WKD", inline=True)
        em.set_footer(**E._footer("Admin"))
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------
    # /admin_fund_distribute
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_fund_distribute", description="[ADMIN] Distribuer des WKD depuis le fond")
    @app_commands.describe(member="Bénéficiaire", amount="Montant WKD", reason="Raison")
    @is_admin()
    async def admin_fund_distribute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 99999],
        reason: str,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            result = await admin_distribute(interaction.user.id, member.id, amount, reason)

            await interaction.followup.send(
                embed=E.embed_success(
                    "Distribution effectuée",
                    f"**{amount} WKD** envoyés à {member.mention}\n"
                    f"Raison : {reason}\n"
                    f"Nouveau solde : {result['new_balance']:,} WKD",
                ),
                ephemeral=True,
            )

            # Post public
            notif = NotificationService(self.bot)
            pub_em = discord.Embed(title="💰 Distribution Admin", color=E.COLOR_ADMIN)
            pub_em.add_field(name="Bénéficiaire", value=member.mention, inline=True)
            pub_em.add_field(name="Montant", value=f"{amount} WKD", inline=True)
            pub_em.add_field(name="Raison", value=reason, inline=False)
            await notif._safe_channel_send(notif.ch_transactions, embed=pub_em)

            # DM au bénéficiaire
            dm_em = discord.Embed(
                title=f"💰 Vous avez reçu {amount} WKD",
                description=f"Distribution admin\nRaison : {reason}",
                color=E.COLOR_SUCCESS,
            )
            await notif._safe_dm(member.id, embed=dm_em)

        except (InsufficientFundsError, ValidationError) as e:
            await interaction.followup.send(embed=E.embed_error("Impossible", str(e)), ephemeral=True)

    # ------------------------------------------------------------------
    # /admin_rollback
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_rollback", description="[ADMIN] Annuler une transaction")
    @app_commands.describe(tx_id="ID de la transaction", reason="Raison obligatoire")
    @is_admin()
    async def admin_rollback(self, interaction: discord.Interaction, tx_id: str, reason: str):
        await interaction.response.defer(ephemeral=True)

        confirmed = await self._require_confirmation(interaction, "rollback")
        if not confirmed:
            await interaction.followup.send(
                embed=E.embed_error("Annulé", "Confirmation échouée ou timeout."), ephemeral=True
            )
            return

        try:
            result = await admin_rollback_transaction(tx_id.strip(), interaction.user.id, reason)

            await interaction.followup.send(
                embed=E.embed_success(
                    "Rollback Effectué",
                    f"Transaction `{tx_id}` annulée.\nRaison : {reason}",
                ),
                ephemeral=True,
            )

            notif = NotificationService(self.bot)
            pub_em = discord.Embed(title="↩️ Rollback Admin", color=E.COLOR_ADMIN)
            pub_em.add_field(name="Transaction", value=f"`{tx_id}`", inline=True)
            pub_em.add_field(name="Par", value=interaction.user.mention, inline=True)
            pub_em.add_field(name="Raison", value=reason, inline=False)
            await notif._safe_channel_send(notif.ch_transactions, embed=pub_em)

        except (ValidationError, InsufficientFundsError) as e:
            await interaction.followup.send(embed=E.embed_error("Impossible", str(e)), ephemeral=True)

    # ------------------------------------------------------------------
    # /admin_blockchain_lock / unlock
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_blockchain_lock", description="[ADMIN] Verrouiller la blockchain")
    @app_commands.describe(reason="Raison du verrouillage")
    @is_admin()
    async def admin_blockchain_lock(self, interaction: discord.Interaction, reason: str):
        await interaction.response.defer(ephemeral=True)

        confirmed = await self._require_confirmation(interaction, "blockchain_lock")
        if not confirmed:
            await interaction.followup.send(embed=E.embed_error("Annulé", "Confirmation échouée."), ephemeral=True)
            return

        async with transaction() as conn:
            cfg = ConfigRepository(conn)
            await cfg.set("blockchain_locked", "true")
            await cfg.set("blockchain_lock_reason", reason)
            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "blockchain_lock", details={"reason": reason})

        await interaction.followup.send(
            embed=E.embed_success("Blockchain Verrouillée", f"Raison : {reason}"), ephemeral=True
        )

        notif = NotificationService(self.bot)
        await notif._safe_channel_send(
            notif.ch_transactions,
            embed=E.embed_blockchain_locked(reason),
        )

    @app_commands.command(name="admin_blockchain_unlock", description="[ADMIN] Déverrouiller la blockchain")
    @is_admin()
    async def admin_blockchain_unlock(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        confirmed = await self._require_confirmation(interaction, "blockchain_unlock")
        if not confirmed:
            await interaction.followup.send(embed=E.embed_error("Annulé", "Confirmation échouée."), ephemeral=True)
            return

        async with transaction() as conn:
            cfg = ConfigRepository(conn)
            await cfg.set("blockchain_locked", "false")
            await cfg.set("blockchain_lock_reason", "")
            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "blockchain_unlock")

        await interaction.followup.send(
            embed=E.embed_success("Blockchain Déverrouillée", "Les transactions sont à nouveau actives."),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /admin_ban_user / unban
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_ban_user", description="[ADMIN] Suspendre le compte WKD d'un membre")
    @app_commands.describe(member="Membre à suspendre", reason="Raison")
    @is_admin()
    async def admin_ban_user(self, interaction: discord.Interaction, member: discord.Member, reason: str = ""):
        await interaction.response.defer(ephemeral=True)

        confirmed = await self._require_confirmation(interaction, "ban_user")
        if not confirmed:
            await interaction.followup.send(embed=E.embed_error("Annulé", "Confirmation échouée."), ephemeral=True)
            return

        async with transaction() as conn:
            users = UserRepository(conn)
            await users.set_banned(member.id, True)
            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "ban_user", target_id=member.id, details={"reason": reason})

        await interaction.followup.send(
            embed=E.embed_success("Compte Suspendu", f"{member.mention} ne peut plus utiliser le système WKD.\nRaison : {reason}"),
            ephemeral=True,
        )

    @app_commands.command(name="admin_unban_user", description="[ADMIN] Réactiver le compte WKD d'un membre")
    @app_commands.describe(member="Membre à réactiver")
    @is_admin()
    async def admin_unban_user(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        async with transaction() as conn:
            users = UserRepository(conn)
            await users.set_banned(member.id, False)
            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "unban_user", target_id=member.id)

        await interaction.followup.send(
            embed=E.embed_success("Compte Réactivé", f"{member.mention} peut à nouveau utiliser WKD."),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /admin_exempt_waiting
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_exempt_waiting", description="[ADMIN] Exempter l'attente de 15 jours (airdrop immédiat)")
    @app_commands.describe(member="Membre à exempter")
    @is_admin()
    async def admin_exempt_waiting(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        async with transaction() as conn:
            users = UserRepository(conn)
            user = await users.get_by_id(member.id)
            if not user:
                await interaction.followup.send(embed=E.embed_error("Introuvable", "Ce membre n'a pas de compte WKD."), ephemeral=True)
                return
            if user["airdrop_received"]:
                await interaction.followup.send(embed=E.embed_error("Déjà reçu", "Ce membre a déjà reçu son airdrop."), ephemeral=True)
                return

            await users.set_airdrop_eligible_now(member.id)

            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "exempt_waiting", target_id=member.id)

            # Enregistrer l'exemption
            await conn.execute(
                "INSERT INTO exemption_audit (exempted_user, exempted_by, reason) VALUES ($1, $2, $3)",
                member.id, interaction.user.id, "Exemption manuelle admin",
            )

        await interaction.followup.send(
            embed=E.embed_success(
                "Exemption Accordée",
                f"{member.mention} recevra son airdrop lors du prochain cycle (max 6h).",
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /admin_bet_resolve
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_bet_resolve", description="[ADMIN] Résoudre un pari manuellement")
    @app_commands.describe(
        bet_id="ID du pari",
        winner="Gagnant (laisser vide pour remboursement)",
    )
    @is_admin()
    async def admin_bet_resolve(
        self,
        interaction: discord.Interaction,
        bet_id: str,
        winner: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        bet_id = bet_id.strip().lower()

        try:
            result = await resolve_bet(
                bet_id=bet_id,
                winner_id=winner.id if winner else 0,
                resolved_by=interaction.user.id,
            )

            msg = (
                f"Gagnant : {winner.mention} — +{result['total_paid']} WKD"
                if winner
                else "Remboursement effectué aux deux parieurs."
            )
            await interaction.followup.send(embed=E.embed_success("Pari Résolu", msg), ephemeral=True)

            notif = NotificationService(self.bot)
            await notif.post_bet_resolved({"bet_id": bet_id}, winner.id if winner else None, result["total_paid"])
            if winner:
                await notif.send_bet_won_dm(winner.id, bet_id, result["total_paid"])

        except (ValidationError, InsufficientFundsError) as e:
            await interaction.followup.send(embed=E.embed_error("Impossible", str(e)), ephemeral=True)

    # ------------------------------------------------------------------
    # /jury_add / remove / list
    # ------------------------------------------------------------------

    @app_commands.command(name="jury_add", description="[ADMIN] Ajouter un membre au pool jury")
    @app_commands.describe(member="Membre à ajouter")
    @is_admin()
    async def jury_add(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        async with transaction() as conn:
            bets_repo = BetRepository(conn)
            cfg = ConfigRepository(conn)
            max_pool = await cfg.get_int("jury_pool_max", 8)
            current_size = await bets_repo.jury_pool_size()

            if current_size >= max_pool:
                await interaction.followup.send(
                    embed=E.embed_error("Pool plein", f"Limite atteinte : {max_pool} jurés maximum."),
                    ephemeral=True,
                )
                return

            # Vérifier que le membre a un compte WKD
            users = UserRepository(conn)
            if not await users.exists(member.id):
                await interaction.followup.send(
                    embed=E.embed_error("Introuvable", "Ce membre n'a pas encore de compte WKD."),
                    ephemeral=True,
                )
                return

            await bets_repo.add_to_jury_pool(member.id, interaction.user.id)
            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "jury_add", target_id=member.id)

        await interaction.followup.send(
            embed=E.embed_success("Juré Ajouté", f"{member.mention} a été ajouté au pool jury."),
            ephemeral=True,
        )

    @app_commands.command(name="jury_remove", description="[ADMIN] Retirer un membre du pool jury")
    @app_commands.describe(member="Membre à retirer")
    @is_admin()
    async def jury_remove(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        async with transaction() as conn:
            bets_repo = BetRepository(conn)
            await bets_repo.remove_from_jury_pool(member.id)
            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "jury_remove", target_id=member.id)

        await interaction.followup.send(
            embed=E.embed_success("Juré Retiré", f"{member.mention} a été retiré du pool jury."),
            ephemeral=True,
        )

    @app_commands.command(name="jury_list", description="[ADMIN] Voir le pool jury actuel")
    @is_admin()
    async def jury_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with acquire() as conn:
            bets_repo = BetRepository(conn)
            pool = await bets_repo.get_jury_pool()

        em = discord.Embed(
            title=f"{E.EMOJI_JURY} Pool Jury ({len(pool)} membres)",
            color=E.COLOR_BET,
        )
        if not pool:
            em.description = "*Pool vide.*"
        else:
            em.description = "\n".join([f"• <@{m['user_id']}> — {m['username']}" for m in pool])
        em.set_footer(**E._footer("Admin"))
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------
    # /admin_config view / set
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_config_view", description="[ADMIN] Voir la configuration")
    @is_admin()
    async def admin_config_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with acquire() as conn:
            cfg = ConfigRepository(conn)
            all_config = await cfg.get_all()

        em = discord.Embed(title=f"{E.EMOJI_ADMIN} Configuration WKD", color=E.COLOR_ADMIN)
        for key, value in sorted(all_config.items()):
            em.add_field(name=key, value=f"`{value}`", inline=True)
        em.set_footer(**E._footer("Admin"))
        await interaction.followup.send(embed=em, ephemeral=True)

    @app_commands.command(name="admin_config_set", description="[ADMIN] Modifier un paramètre de configuration")
    @app_commands.describe(key="Clé de configuration", value="Nouvelle valeur")
    @is_admin()
    async def admin_config_set(self, interaction: discord.Interaction, key: str, value: str):
        await interaction.response.defer(ephemeral=True)

        # Whitelist des clés modifiables
        allowed_keys = {
            "message_reward_count", "message_reward_amount", "daily_limit",
            "message_cooldown_seconds", "airdrop_amount", "airdrop_delay_days",
            "inactive_tax_rate", "inactive_tax_min_balance", "inactive_days_threshold",
            "rich_tax_rate", "rich_tax_top_n", "contract_cooldown_hours",
            "bet_cooldown_hours", "jury_pool_max", "jury_vote_initial_hours",
            "jury_vote_replacement_hours",
        }
        if key not in allowed_keys:
            await interaction.followup.send(
                embed=E.embed_error("Clé non autorisée", f"Clés modifiables : {', '.join(sorted(allowed_keys))}"),
                ephemeral=True,
            )
            return

        async with transaction() as conn:
            cfg = ConfigRepository(conn)
            await cfg.set(key, value, updated_by=interaction.user.id)
            audit = AuditRepository(conn)
            await audit.log_action(interaction.user.id, "config_set", details={"key": key, "value": value})

        await interaction.followup.send(
            embed=E.embed_success("Configuration mise à jour", f"`{key}` = `{value}`"),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /admin_logs
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_logs", description="[ADMIN] Voir les logs du système")
    @app_commands.describe(
        type="Filtrer par type d'action",
        limit="Nombre de logs (max 50)",
    )
    @is_mod_or_admin()
    async def admin_logs(
        self,
        interaction: discord.Interaction,
        type: Optional[str] = None,
        limit: app_commands.Range[int, 1, 50] = 20,
    ):
        await interaction.response.defer(ephemeral=True)
        async with acquire() as conn:
            audit = AuditRepository(conn)
            logs = await audit.get_recent_logs(limit=limit, log_type=type)

        em = discord.Embed(
            title=f"{E.EMOJI_ADMIN} Logs Système ({len(logs)})",
            color=E.COLOR_ADMIN,
        )
        lines = []
        for log in logs:
            ts = int(log["created_at"].timestamp())
            lines.append(
                f"`{log['action']}` — <@{log['actor_id']}> "
                f"{'→ <@' + str(log['target_id']) + '>' if log['target_id'] else ''} "
                f"<t:{ts}:R>"
            )
        em.description = "\n".join(lines[:30]) if lines else "*Aucun log.*"
        em.set_footer(**E._footer("Admin"))
        await interaction.followup.send(embed=em, ephemeral=True)

    # ------------------------------------------------------------------
    # /admin_backup_now
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_backup_now", description="[ADMIN] Déclencher un backup manuel")
    @is_admin()
    async def admin_backup_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            embed=E.embed_warning("Backup", "Backup en cours..."), ephemeral=True
        )
        try:
            import asyncio
            from backup.gcloud import perform_backup
            result = await asyncio.get_event_loop().run_in_executor(None, perform_backup)
            await interaction.followup.send(
                embed=E.embed_success("Backup Terminé", str(result)), ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                embed=E.embed_error("Backup Échoué", str(e)), ephemeral=True
            )

    # ------------------------------------------------------------------
    # /admin_pending_airdrops
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_pending_airdrops", description="[ADMIN] Voir les membres en attente d'airdrop")
    @is_admin()
    async def admin_pending_airdrops(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with acquire() as conn:
            users = UserRepository(conn)
            pending = await users.get_pending_airdrops()

        em = discord.Embed(
            title=f"🎁 Airdrops en Attente ({len(pending)})",
            color=E.COLOR_WKD,
        )
        if not pending:
            em.description = "*Aucun airdrop en attente.*"
        else:
            lines = []
            for u in pending[:20]:
                ts = int(u["airdrop_eligible_date"].timestamp())
                lines.append(f"• <@{u['user_id']}> — éligible <t:{ts}:R>")
            em.description = "\n".join(lines)
        em.set_footer(**E._footer("Admin"))
        await interaction.followup.send(embed=em, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
