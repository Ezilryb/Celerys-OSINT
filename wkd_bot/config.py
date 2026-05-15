"""
config.py
Configuration centrale du bot WKD.
Toutes les valeurs sensibles viennent du fichier .env — jamais en dur dans le code.
"""

from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# Charger .env dès l'import
load_dotenv()

logger = logging.getLogger("wkd.config")


def _require(key: str) -> str:
    """Retourne la valeur d'une variable d'environnement obligatoire."""
    val = os.getenv(key)
    if not val:
        logger.critical(f"Variable d'environnement obligatoire manquante : {key}")
        sys.exit(1)
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ======================================================================
# DISCORD
# ======================================================================
DISCORD_TOKEN: str = _require("DISCORD_TOKEN")
GUILD_ID: int = int(_require("DISCORD_GUILD_ID"))

# IDs admin (séparés par virgule)
ADMIN_IDS_RAW: str = _require("ADMIN_IDS")
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()
}

MOD_ROLE_ID: int | None = int(x) if (x := _optional("MOD_ROLE_ID")) else None

# ======================================================================
# SALONS DISCORD (IDs)
# ======================================================================
CHANNEL_TRANSACTIONS_ID: int = int(_require("CHANNEL_TRANSACTIONS_ID"))
CHANNEL_PARIS_ID: int = int(_require("CHANNEL_PARIS_ID"))
CHANNEL_VALIDATION_PARIS_ID: int = int(_require("CHANNEL_VALIDATION_PARIS_ID"))
CHANNEL_VERIFICATION_TX_ID: int = int(_require("CHANNEL_VERIFICATION_TX_ID"))

# ======================================================================
# BASE DE DONNÉES
# ======================================================================
POSTGRES_HOST: str     = _optional("POSTGRES_HOST", "localhost")
POSTGRES_PORT: int     = int(_optional("POSTGRES_PORT", "5432"))
POSTGRES_DB: str       = _optional("POSTGRES_DB", "wkd_database")
POSTGRES_USER: str     = _optional("POSTGRES_USER", "wkd_bot")
POSTGRES_PASSWORD: str = _require("POSTGRES_PASSWORD")

DATABASE_DSN: str = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

DB_POOL_MIN: int = int(_optional("DB_POOL_MIN", "5"))
DB_POOL_MAX: int = int(_optional("DB_POOL_MAX", "20"))

# ======================================================================
# GOOGLE CLOUD STORAGE
# ======================================================================
GCS_BUCKET_NAME: str = _optional("GCS_BUCKET_NAME", "wkd-backups")
GOOGLE_APPLICATION_CREDENTIALS: str = _optional("GOOGLE_APPLICATION_CREDENTIALS", "")

# ======================================================================
# CHEMINS
# ======================================================================
BASE_DIR: Path = Path(__file__).parent
SCHEMA_PATH: Path = BASE_DIR / "database" / "schema.sql"

# ======================================================================
# LOGGING
# ======================================================================
LOG_LEVEL: str  = _optional("LOG_LEVEL", "INFO").upper()
LOG_FILE: str   = _optional("LOG_FILE", "logs/wkd_bot.log")

# ======================================================================
# VALIDATION FINALE
# ======================================================================

def validate() -> None:
    """Vérifie la cohérence de la configuration au démarrage."""
    if not ADMIN_IDS:
        logger.critical("ADMIN_IDS est vide — le bot ne peut pas démarrer sans administrateur.")
        sys.exit(1)

    if DB_POOL_MIN > DB_POOL_MAX:
        logger.warning(f"DB_POOL_MIN ({DB_POOL_MIN}) > DB_POOL_MAX ({DB_POOL_MAX}), correction automatique")

    logger.info("Configuration validée ✓")
    logger.info(f"  Guild ID     : {GUILD_ID}")
    logger.info(f"  Admin IDs    : {ADMIN_IDS}")
    logger.info(f"  DB           : {POSTGRES_USER}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}")
    logger.info(f"  GCS Bucket   : {GCS_BUCKET_NAME}")
