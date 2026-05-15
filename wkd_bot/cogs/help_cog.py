"""
cogs/help_cog.py
Aide et tutoriel interactif pour les membres WKD.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import embeds as E


class HelpCog(commands.Cog, name="Help"):
    """Commandes d'aide WKD."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Afficher l'aide WKD")
    @app_commands.describe(section="Section à afficher")
    @app_commands.choices(section=[
        app_commands.Choice(name="Général", value="general"),
        app_commands.Choice(name="Économie", value="economy"),
        app_commands.Choice(name="Contrats P2P", value="contracts"),
        app_commands.Choice(name="Paris", value="bets"),
    ])
    async def help(self, interaction: discord.Interaction, section: str = "general"):
        await interaction.response.defer(ephemeral=True)

        em = discord.Embed(color=E.COLOR_WKD)
        em.set_footer(**E._footer())

        if section == "general":
            em.title = f"{E.EMOJI_WKD} WKD — Guide Général"
            em.description = (
                "**WICKED (WKD)** est la monnaie virtuelle de ce serveur.\n"
                "Aucune valeur réelle — jeu communautaire uniquement."
            )
            em.add_field(
                name="Commandes principales",
                value=(
                    "`/balance` — Voir votre solde\n"
                    "`/leaderboard` — Classement\n"
                    "`/history` — Historique\n"
                    "`/contract_create` — Contrat P2P\n"
                    "`/bet_create` — Créer un pari\n"
                    "`/burn` — Brûler des WKD\n"
                    "`/help economy` — Économie\n"
                    "`/help contracts` — Contrats\n"
                    "`/help bets` — Paris"
                ),
                inline=False,
            )

        elif section == "economy":
            em.title = f"💰 Économie WKD"
            em.add_field(
                name="Gagner des WKD",
                value=(
                    "• **Messages** : 1 WKD tous les 222 messages\n"
                    "• Max **3 WKD/jour** via messages\n"
                    "• Cooldown anti-spam : 1 min entre messages comptés\n"
                    "• **Airdrop** : 111 WKD après 15 jours sur le serveur\n"
                    "• **Événements** : distributions admin ponctuelles"
                ),
                inline=False,
            )
            em.add_field(
                name="Taxes automatiques (samedi minuit)",
                value=(
                    "• **Inactivité** : -1% si ≥ 7 jours sans message et solde > 222 WKD\n"
                    "• **Riches** : -0.5% sur le top 9 des détenteurs\n"
                    "• Calcul : `math.floor()` (arrondi inférieur)"
                ),
                inline=False,
            )
            em.add_field(
                name="Règles de base",
                value=(
                    "• Minimum transférable : 1 WKD\n"
                    "• Compte Discord requis : ≥ 1 mois\n"
                    "• Ancienneté serveur requise : ≥ 15 jours (pour certaines actions)"
                ),
                inline=False,
            )

        elif section == "contracts":
            em.title = f"🤝 Contrats P2P"
            em.add_field(
                name="Concept",
                value=(
                    "Échange bilatéral **asymétrique** entre deux membres.\n"
                    "Le montant reçu DOIT être > montant envoyé (pour éviter les dons purs).\n\n"
                    "**Exemple** : A envoie 10 WKD → B renvoie 15 WKD"
                ),
                inline=False,
            )
            em.add_field(
                name="Commandes",
                value=(
                    "`/contract_create @User send:X receive:Y note:...` — Créer\n"
                    "`/contract_accept [ID]` — Accepter\n"
                    "`/contract_refuse [ID]` — Refuser\n"
                    "`/contract_pending` — Contrats en attente\n"
                    "`/contract_history` — Historique"
                ),
                inline=False,
            )
            em.add_field(
                name="Règles",
                value=(
                    "• Cooldown : 1 contrat par paire par 24h\n"
                    "• Expiration : 48h si pas de réponse\n"
                    "• Exécution atomique (tout réussit ou tout échoue)"
                ),
                inline=False,
            )

        elif section == "bets":
            em.title = f"🎲 Système de Paris"
            em.add_field(
                name="Concept",
                value=(
                    "Pari symétrique : chaque parieur mise le même montant.\n"
                    "Les fonds sont **bloqués en escrow** jusqu'à la résolution.\n"
                    "Un jury de 3 membres valide et résout les paris."
                ),
                inline=False,
            )
            em.add_field(
                name="Commandes",
                value=(
                    "`/bet_create @User amount:X condition:...` — Créer\n"
                    "`/bet_accept [ID]` — Accepter\n"
                    "`/bet_refuse [ID]` — Refuser\n"
                    "`/bet_claim [ID] note:...` — Réclamer victoire\n"
                    "`/bet_vote [ID] decision:[approve/reject/continue]` — Voter (jurés)\n"
                    "`/bet_active` — Paris actifs\n"
                    "`/bet_mine` — Mes paris\n"
                    "`/bet_status [ID]` — Détails"
                ),
                inline=False,
            )
            em.add_field(
                name="Système Jury",
                value=(
                    "• 3 jurés tirés aléatoirement (hors parieurs)\n"
                    "• Délai vote : 24h — remplacement automatique si oubli\n"
                    "• Majorité : 2/3 votes requis\n"
                    "• L'admin peut override"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=em, ephemeral=True)

    @app_commands.command(name="tutorial", description="Tutoriel interactif pour les nouveaux membres")
    async def tutorial(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        em = discord.Embed(
            title=f"🎓 Tutoriel WKD",
            color=E.COLOR_WKD,
        )
        em.add_field(
            name="1️⃣ Créer votre compte",
            value="Votre compte WKD est créé automatiquement quand vous rejoignez le serveur.",
            inline=False,
        )
        em.add_field(
            name="2️⃣ Recevoir votre airdrop",
            value="Après 15 jours sur le serveur, vous recevrez automatiquement **111 WKD** en DM.",
            inline=False,
        )
        em.add_field(
            name="3️⃣ Gagner des WKD",
            value="Envoyez des messages dans le serveur. 1 WKD tous les 222 messages (max 3/jour).",
            inline=False,
        )
        em.add_field(
            name="4️⃣ Voir votre solde",
            value="Utilisez `/balance` pour voir votre solde et statistiques.",
            inline=False,
        )
        em.add_field(
            name="5️⃣ Faire des échanges",
            value="Utilisez `/contract_create` pour échanger des WKD avec d'autres membres.",
            inline=False,
        )
        em.add_field(
            name="6️⃣ Parier",
            value="Utilisez `/bet_create` pour parier sur n'importe quelle condition avec un autre membre.",
            inline=False,
        )
        em.add_field(
            name="📖 Aide complète",
            value="Utilisez `/help` pour accéder au guide complet.",
            inline=False,
        )
        em.set_footer(**E._footer())
        await interaction.followup.send(embed=em, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
