"""
main.py
Point d'entrée principal du bot WKD.
Lance la connexion Discord, initialise la DB, charge les cogs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

# Configuration en premier
import config
config.validate()

from database.connection import init_pool, close_pool, execute_schema
from core.scheduler import Scheduler

# ======================================================================
# LOGGING
# ======================================================================

def setup_logging() -> None:
    log_dir = Path(config.LOG_FILE).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)

    # Console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(level)

    # Fichier (rotation simple)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Réduire le bruit de discord.py
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.ERROR)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


logger = logging.getLogger("wkd.main")

# ======================================================================
# BOT
# ======================================================================

COGS = [
    "cogs.events",
    "cogs.wallet",
    "cogs.contracts",
    "cogs.bets",
    "cogs.admin",
    "cogs.help_cog",
]


class WKDBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="!wkd_",  # Préfixe de secours (non utilisé — tout est slash)
            intents=intents,
            help_command=None,       # Désactivé — on utilise /help
        )
        self.scheduler: Scheduler | None = None

    async def setup_hook(self) -> None:
        """Appelé une seule fois avant le login Discord."""
        logger.info("=== WKD Bot démarrage ===")

        # 1. Base de données
        logger.info("Connexion PostgreSQL...")
        await init_pool(
            dsn=config.DATABASE_DSN,
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
        )

        # 2. Initialiser le schéma
        logger.info("Initialisation du schéma DB...")
        await execute_schema(str(config.SCHEMA_PATH))

        # 3. Charger les cogs
        logger.info("Chargement des cogs...")
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info(f"  ✓ {cog}")
            except Exception as e:
                logger.error(f"  ✗ {cog}: {e}", exc_info=True)

        # 4. Synchroniser les slash commands sur le guild
        logger.info(f"Synchronisation des slash commands (guild {config.GUILD_ID})...")
        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info(f"  {len(synced)} commandes synchronisées")

        # 5. Scheduler
        self.scheduler = Scheduler(self)

    async def on_ready(self) -> None:
        logger.info(f"Bot connecté : {self.user} (ID: {self.user.id})")
        logger.info(f"Serveur cible : Guild {config.GUILD_ID}")

        # Démarrer le scheduler après connexion
        if self.scheduler:
            self.scheduler.start()
            logger.info("Scheduler démarré")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="l'économie WKD 🔥",
            )
        )

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        logger.error(f"Erreur non gérée dans {event_method}", exc_info=True)

    async def close(self) -> None:
        """Arrêt propre."""
        logger.info("Arrêt du bot...")
        if self.scheduler:
            await self.scheduler.stop()
        await close_pool()
        await super().close()
        logger.info("Bot arrêté proprement.")


# ======================================================================
# POINT D'ENTRÉE
# ======================================================================

async def main() -> None:
    setup_logging()
    bot = WKDBot()

    async with bot:
        try:
            await bot.start(config.DISCORD_TOKEN)
        except discord.LoginFailure:
            logger.critical("Token Discord invalide. Vérifiez DISCORD_TOKEN dans .env")
            sys.exit(1)
        except KeyboardInterrupt:
            logger.info("Interruption clavier reçue")
        finally:
            if not bot.is_closed():
                await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
