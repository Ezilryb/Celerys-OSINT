"""
database/connection.py
Pool de connexions PostgreSQL (asyncpg) - Thread-safe, singleton.
"""

import asyncpg
import logging
from typing import Optional
from contextlib import asynccontextmanager

logger = logging.getLogger("wkd.db")

# Pool global
_pool: Optional[asyncpg.Pool] = None


async def init_pool(dsn: str, min_size: int = 5, max_size: int = 20) -> asyncpg.Pool:
    """Initialise le pool de connexions. À appeler au démarrage."""
    global _pool
    if _pool is not None:
        return _pool

    logger.info("Initialisation du pool PostgreSQL...")
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=30,
        # Codecs : toutes les dates retournées avec timezone
        server_settings={"timezone": "UTC"},
    )
    logger.info(f"Pool PostgreSQL créé ({min_size}-{max_size} connexions)")
    return _pool


async def close_pool() -> None:
    """Ferme le pool proprement. À appeler à l'arrêt."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Pool PostgreSQL fermé")


def get_pool() -> asyncpg.Pool:
    """Retourne le pool (doit être initialisé avant)."""
    if _pool is None:
        raise RuntimeError("Pool PostgreSQL non initialisé. Appelez init_pool() d'abord.")
    return _pool


@asynccontextmanager
async def acquire():
    """Contexte manager pour acquérir une connexion du pool."""
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction():
    """
    Contexte manager pour une transaction atomique.
    En cas d'exception → rollback automatique.
    Garantit l'atomicité ACID de toutes les opérations.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


async def execute_schema(schema_path: str) -> None:
    """Exécute le fichier schema.sql pour initialiser les tables."""
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()

    async with acquire() as conn:
        await conn.execute(sql)
    logger.info("Schéma PostgreSQL initialisé")
