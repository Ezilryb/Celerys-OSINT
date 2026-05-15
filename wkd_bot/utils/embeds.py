"""
utils/embeds.py
Construction de tous les embeds Discord du bot WKD.
Centralisé ici pour cohérence visuelle et maintenance facile.
"""

from __future__ import annotations

import discord
from datetime import datetime, timezone
from typing import Optional


# Palette couleurs WKD
COLOR_WKD       = discord.Color.from_str("#FF4500")   # Orange-rouge principal
COLOR_SUCCESS   = discord.Color.green()
COLOR_ERROR     = discord.Color.red()
COLOR_WARNING   = discord.Color.orange()
COLOR_INFO      = discord.Color.blurple()
COLOR_TAX       = discord.Color.from_str("#FF6B00")
COLOR_BET       = discord.Color.from_str("#9B59B6")
COLOR_CONTRACT  = discord.Color.from_str("#2ECC71")
COLOR_ADMIN     = discord.Color.from_str("#E74C3C")
COLOR_BURN      = discord.Color.from_str("#8B0000")

EMOJI_WKD    = "🔥"
EMOJI_OK     = "✅"
EMOJI_ERR    = "❌"
EMOJI_WARN   = "⚠️"
EMOJI_LOCK   = "🔒"
EMOJI_BET    = "🎲"
EMOJI_CTR    = "🤝"
EMOJI_JURY   = "⚖️"
EMOJI_ADMIN  = "👑"
EMOJI_BURN   = "🔥"
EMOJI_MONEY  = "💰"


def _footer(text: str = "WKD Economy") -> None:
    """Retourne kwargs footer standard."""
    return {"text": f"{EMOJI_WKD} {text} • {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC"}


# ======================================================================
# WALLET
# ======================================================================

def embed_balance(
    user: discord.User,
    balance: int,
    total_earned: int,
    total_spent: int,
    total_burned: int,
    rank: Optional[int] = None,
) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_MONEY} Portefeuille WKD",
        color=COLOR_WKD,
    )
    em.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    em.add_field(name="Solde", value=f"**{balance:,} WKD**", inline=True)
    if rank:
        em.add_field(name="Classement", value=f"#{rank}", inline=True)
    em.add_field(name="\u200b", value="\u200b", inline=True)
    em.add_field(name="Total gagné", value=f"{total_earned:,} WKD", inline=True)
    em.add_field(name="Total dépensé", value=f"{total_spent:,} WKD", inline=True)
    em.add_field(name="Total brûlé", value=f"{total_burned:,} WKD", inline=True)
    em.set_footer(**_footer())
    return em


def embed_leaderboard(entries: list[dict]) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_WKD} Classement WKD — Top {len(entries)}",
        color=COLOR_WKD,
    )
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 10
    lines = []
    for i, e in enumerate(entries):
        medal = medals[i] if i < len(medals) else f"#{i+1}"
        lines.append(f"{medal} **{e['username']}** — {e['balance']:,} WKD")
    em.description = "\n".join(lines)
    em.set_footer(**_footer())
    return em


def embed_history(user: discord.User, entries: list) -> discord.Embed:
    em = discord.Embed(
        title=f"📋 Historique transactions",
        color=COLOR_INFO,
    )
    em.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    if not entries:
        em.description = "*Aucune transaction.*"
    else:
        lines = []
        for tx in entries[:15]:
            sign = "+" if tx["to_username"] == user.name else "-"
            lines.append(
                f"`{tx['tx_id']}` {sign}{tx['amount']} WKD — {tx['type']} "
                f"— <t:{int(tx['created_at'].timestamp())}:R>"
            )
        em.description = "\n".join(lines)
    em.set_footer(**_footer())
    return em


# ======================================================================
# CONTRATS P2P
# ======================================================================

def embed_contract_created(contract: dict, creator: discord.User, acceptor: discord.User) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_CTR} Nouveau Contrat P2P",
        color=COLOR_CONTRACT,
    )
    em.add_field(name="Créateur", value=creator.mention, inline=True)
    em.add_field(name="Destinataire", value=acceptor.mention, inline=True)
    em.add_field(name="\u200b", value="\u200b", inline=True)
    em.add_field(name=f"{creator.display_name} envoie", value=f"**{contract['amount_sent']} WKD**", inline=True)
    em.add_field(name=f"{acceptor.display_name} renvoie", value=f"**{contract['amount_received']} WKD**", inline=True)
    if contract.get("note"):
        em.add_field(name="Note", value=contract["note"], inline=False)
    em.add_field(name="ID", value=f"`{contract['contract_id']}`", inline=True)
    em.add_field(
        name="Expire",
        value=f"<t:{int(contract['expires_at'].timestamp())}:R>",
        inline=True,
    )
    em.set_footer(**_footer())
    return em


def embed_contract_dm(contract: dict, creator_name: str) -> discord.Embed:
    """Embed envoyé en DM à l'accepteur."""
    em = discord.Embed(
        title=f"📬 Contrat P2P Reçu",
        description=f"**{creator_name}** te propose un contrat :",
        color=COLOR_CONTRACT,
    )
    em.add_field(name="Il/Elle envoie", value=f"**{contract['amount_sent']} WKD**", inline=True)
    em.add_field(name="Tu renvoies", value=f"**{contract['amount_received']} WKD**", inline=True)
    if contract.get("note"):
        em.add_field(name="Note", value=contract["note"], inline=False)
    em.add_field(name="ID du contrat", value=f"`{contract['contract_id']}`", inline=False)
    em.add_field(
        name="Expire",
        value=f"<t:{int(contract['expires_at'].timestamp())}:R>",
        inline=True,
    )
    em.add_field(
        name="Commandes",
        value=(
            f"`/contract_accept {contract['contract_id']}`\n"
            f"`/contract_refuse {contract['contract_id']}`"
        ),
        inline=False,
    )
    em.color = COLOR_CONTRACT
    em.set_footer(**_footer())
    return em


def embed_contract_completed(contract: dict, creator_name: str, acceptor_name: str) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_OK} Contrat Exécuté",
        color=COLOR_SUCCESS,
    )
    em.add_field(name="Contrat", value=f"`{contract['contract_id']}`", inline=False)
    em.add_field(name=f"{creator_name} a envoyé", value=f"{contract['amount_sent']} WKD", inline=True)
    em.add_field(name=f"{acceptor_name} a renvoyé", value=f"{contract['amount_received']} WKD", inline=True)
    em.set_footer(**_footer("Transaction confirmée"))
    return em


# ======================================================================
# PARIS
# ======================================================================

def embed_bet_created(bet: dict, bettor_a: discord.User, bettor_b: discord.User) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_BET} Nouveau Pari",
        color=COLOR_BET,
    )
    em.add_field(name="Parieur A", value=bettor_a.mention, inline=True)
    em.add_field(name="Parieur B", value=bettor_b.mention, inline=True)
    em.add_field(name="Mise chacun", value=f"**{bet['amount']} WKD**", inline=True)
    em.add_field(name="Condition", value=bet["condition"], inline=False)
    em.add_field(name="ID", value=f"`{bet['bet_id']}`", inline=True)
    em.add_field(
        name="Expire",
        value=f"<t:{int(bet['expires_at'].timestamp())}:R>",
        inline=True,
    )
    em.set_footer(**_footer())
    return em


def embed_bet_active(bet: dict) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_BET} Pari Actif",
        color=COLOR_BET,
    )
    em.add_field(name="Parieur A", value=f"<@{bet['bettor_a']}>", inline=True)
    em.add_field(name="Parieur B", value=f"<@{bet['bettor_b']}>", inline=True)
    em.add_field(name="Mise totale", value=f"**{bet['amount'] * 2} WKD** en escrow", inline=True)
    em.add_field(name="Condition", value=bet["condition"], inline=False)
    em.add_field(name="ID", value=f"`{bet['bet_id']}`", inline=True)
    em.add_field(name="Statut", value=bet["status"].replace("_", " ").title(), inline=True)
    em.set_footer(**_footer())
    return em


def embed_jury_assigned(bet_id: str, jurors: list[discord.User], deadline_hours: int) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_JURY} Jury Désigné",
        description=f"3 jurés ont été sélectionnés pour le pari `{bet_id}`",
        color=COLOR_BET,
    )
    em.add_field(
        name="Jurés",
        value="\n".join([f"• {j.mention}" for j in jurors]),
        inline=False,
    )
    em.add_field(name="Délai de vote", value=f"**{deadline_hours}h**", inline=True)
    em.set_footer(**_footer())
    return em


def embed_jury_dm(bet_id: str, condition: str, deadline_hours: int, is_replacement: bool = False) -> discord.Embed:
    title = f"{EMOJI_JURY} {'[REMPLACEMENT] ' if is_replacement else ''}Vote Jury Requis"
    em = discord.Embed(title=title, color=COLOR_BET)
    em.add_field(name="Pari", value=f"`{bet_id}`", inline=False)
    em.add_field(name="Condition", value=condition, inline=False)
    em.add_field(name="Délai", value=f"**{deadline_hours}h** pour voter", inline=True)
    em.add_field(
        name="Commandes",
        value=(
            f"`/bet_vote {bet_id} approve` — Valider\n"
            f"`/bet_vote {bet_id} reject` — Rejeter\n"
            f"`/bet_vote {bet_id} continue` — Continuer"
        ),
        inline=False,
    )
    if is_replacement:
        em.description = "⚠️ Tu remplaces un juré qui n'a pas voté à temps."
    em.set_footer(**_footer())
    return em


def embed_bet_resolved(bet_id: str, winner: Optional[discord.User], total: int) -> discord.Embed:
    if winner:
        em = discord.Embed(
            title=f"{EMOJI_OK} Pari Résolu — {winner.display_name} gagne !",
            color=COLOR_SUCCESS,
        )
        em.add_field(name="Gagnant", value=winner.mention, inline=True)
        em.add_field(name="Gain", value=f"**{total} WKD**", inline=True)
    else:
        em = discord.Embed(
            title=f"Pari Annulé — Remboursement",
            color=COLOR_WARNING,
        )
        em.add_field(name="Résultat", value="Chaque parieur est remboursé", inline=False)
    em.add_field(name="Pari", value=f"`{bet_id}`", inline=False)
    em.set_footer(**_footer())
    return em


# ======================================================================
# TAXES
# ======================================================================

def embed_tax_inactive(tax_amount: int, new_balance: int, inactive_days: int) -> discord.Embed:
    em = discord.Embed(
        title=f"📉 Taxe d'Inactivité Appliquée",
        color=COLOR_TAX,
    )
    em.add_field(name="Montant prélevé", value=f"**-{tax_amount} WKD** (1%)", inline=True)
    em.add_field(name="Raison", value=f"{inactive_days} jours sans message", inline=True)
    em.add_field(name="Nouveau solde", value=f"**{new_balance:,} WKD**", inline=False)
    em.add_field(
        name="💡 Conseil",
        value="Envoyez un message par semaine pour éviter la taxe d'inactivité.",
        inline=False,
    )
    em.set_footer(**_footer("Taxe hebdomadaire — Samedi minuit"))
    return em


def embed_tax_rich(tax_amount: int, new_balance: int) -> discord.Embed:
    em = discord.Embed(
        title=f"💎 Taxe Riches Appliquée",
        color=COLOR_TAX,
    )
    em.add_field(name="Montant prélevé", value=f"**-{tax_amount} WKD** (0.5%)", inline=True)
    em.add_field(name="Raison", value="Top 9 détenteurs WKD", inline=True)
    em.add_field(name="Nouveau solde", value=f"**{new_balance:,} WKD**", inline=False)
    em.set_footer(**_footer("Taxe hebdomadaire — Samedi minuit"))
    return em


# ======================================================================
# AIRDROP
# ======================================================================

def embed_welcome_new_member(airdrop_date: datetime, delay_days: int) -> discord.Embed:
    em = discord.Embed(
        title=f"🎉 Bienvenue sur le serveur !",
        description="Ton compte WKD est créé.",
        color=COLOR_WKD,
    )
    em.add_field(
        name=f"🕐 Airdrop de bienvenue",
        value=(
            f"Tu recevras **111 WKD** <t:{int(airdrop_date.timestamp())}:R>\n"
            f"*(dans {delay_days} jours)*"
        ),
        inline=False,
    )
    em.add_field(
        name="En attendant",
        value=(
            "• Gagner des WKD par messages (1 WKD / 222 messages)\n"
            "• Maximum **3 WKD/jour**\n"
            "• Consulter `/help`"
        ),
        inline=False,
    )
    em.add_field(
        name="💡 Info",
        value="L'admin peut t'exempter de cette attente si besoin.",
        inline=False,
    )
    em.set_footer(**_footer())
    return em


def embed_airdrop_received(amount: int, new_balance: int) -> discord.Embed:
    em = discord.Embed(
        title=f"💰 Airdrop Reçu !",
        description=f"Tu as reçu ton airdrop de bienvenue !",
        color=COLOR_SUCCESS,
    )
    em.add_field(name="Montant", value=f"**+{amount} WKD**", inline=True)
    em.add_field(name="Ton solde", value=f"**{new_balance:,} WKD**", inline=True)
    em.add_field(
        name="Tu peux maintenant",
        value=(
            "• Créer des contrats P2P\n"
            "• Parier avec d'autres membres\n"
            "• Consulter `/help`"
        ),
        inline=False,
    )
    em.set_footer(**_footer())
    return em


# ======================================================================
# ERREURS
# ======================================================================

def embed_error(title: str, description: str) -> discord.Embed:
    em = discord.Embed(title=f"{EMOJI_ERR} {title}", description=description, color=COLOR_ERROR)
    em.set_footer(**_footer())
    return em


def embed_success(title: str, description: str = "") -> discord.Embed:
    em = discord.Embed(title=f"{EMOJI_OK} {title}", description=description, color=COLOR_SUCCESS)
    em.set_footer(**_footer())
    return em


def embed_warning(title: str, description: str) -> discord.Embed:
    em = discord.Embed(title=f"{EMOJI_WARN} {title}", description=description, color=COLOR_WARNING)
    em.set_footer(**_footer())
    return em


# ======================================================================
# ADMIN
# ======================================================================

def embed_admin_stats(stats: dict) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_ADMIN} Statistiques Système WKD",
        color=COLOR_ADMIN,
    )
    em.add_field(name="Fond admin", value=f"{stats.get('fund_balance', 0):,} WKD", inline=True)
    em.add_field(name="Total circulant", value=f"{stats.get('total_balance', 0):,} WKD", inline=True)
    em.add_field(name="Total créé", value=f"{stats.get('total_created', 0):,} WKD", inline=True)
    em.add_field(name="Membres actifs", value=f"{stats.get('active_users', 0)}", inline=True)
    em.add_field(name="Contrats en attente", value=f"{stats.get('pending_contracts', 0)}", inline=True)
    em.add_field(name="Paris actifs", value=f"{stats.get('active_bets', 0)}", inline=True)
    em.add_field(name="Pool jury", value=f"{stats.get('jury_pool_size', 0)} membres", inline=True)
    em.add_field(name="En attente airdrop", value=f"{stats.get('pending_airdrops', 0)}", inline=True)
    em.add_field(name="Blockchain", value="🔒 Verrouillée" if stats.get("blockchain_locked") else "🟢 Active", inline=True)
    em.set_footer(**_footer("Admin Panel"))
    return em


def embed_blockchain_locked(reason: str) -> discord.Embed:
    em = discord.Embed(
        title=f"{EMOJI_LOCK} Blockchain Verrouillée",
        description=f"Toutes les transactions sont suspendues.\n\n**Raison :** {reason or 'Maintenance'}",
        color=COLOR_ERROR,
    )
    em.set_footer(**_footer())
    return em
