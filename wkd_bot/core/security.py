"""
core/security.py
Sécurité : rate limiting en mémoire, vérification d'éligibilité,
détection multi-comptes, anti-spam, confirmation admin.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("wkd.security")


# ======================================================================
# RATE LIMITER EN MÉMOIRE
# ======================================================================

class RateLimiter:
    """
    Rate limiter simple par user_id + action.
    Thread-safe via asyncio.Lock.
    Les données sont en RAM uniquement (reset au redémarrage).
    """

    def __init__(self):
        self._limits: dict[str, float] = {}  # key → timestamp expiry
        self._lock = asyncio.Lock()

    def _key(self, user_id: int, action: str) -> str:
        return f"{user_id}:{action}"

    async def is_limited(self, user_id: int, action: str) -> bool:
        """Retourne True si l'utilisateur est encore en cooldown."""
        key = self._key(user_id, action)
        async with self._lock:
            expiry = self._limits.get(key, 0)
            return time.time() < expiry

    async def set_limit(self, user_id: int, action: str, seconds: int) -> None:
        """Pose un cooldown de `seconds` secondes."""
        key = self._key(user_id, action)
        async with self._lock:
            self._limits[key] = time.time() + seconds

    async def remaining(self, user_id: int, action: str) -> float:
        """Retourne les secondes restantes (0 si pas de limite)."""
        key = self._key(user_id, action)
        async with self._lock:
            expiry = self._limits.get(key, 0)
            return max(0.0, expiry - time.time())

    async def cleanup(self) -> int:
        """Supprime les entrées expirées. Retourne le nombre supprimé."""
        now = time.time()
        async with self._lock:
            expired = [k for k, v in self._limits.items() if v < now]
            for k in expired:
                del self._limits[k]
        return len(expired)


# Instance globale
rate_limiter = RateLimiter()


# ======================================================================
# COOLDOWNS SPÉCIFIQUES AUX MESSAGES
# ======================================================================

class MessageCooldownTracker:
    """
    Suivi du cooldown anti-spam des messages (1 min entre messages comptabilisés).
    En mémoire uniquement.
    """

    def __init__(self, cooldown_seconds: int = 60):
        self._last_message: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self.cooldown_seconds = cooldown_seconds

    async def can_count_message(self, user_id: int) -> bool:
        """True si le message peut être comptabilisé (cooldown passé)."""
        now = time.time()
        async with self._lock:
            last = self._last_message.get(user_id, 0)
            if now - last >= self.cooldown_seconds:
                self._last_message[user_id] = now
                return True
            return False

    async def update_cooldown(self, user_id: int, cooldown_seconds: int) -> None:
        """Met à jour le cooldown dynamiquement (si config changée)."""
        self.cooldown_seconds = cooldown_seconds


message_tracker = MessageCooldownTracker()


# ======================================================================
# VÉRIFICATION D'ÉLIGIBILITÉ
# ======================================================================

def check_account_age(created_at: datetime, min_days: int = 30) -> tuple[bool, int]:
    """
    Vérifie l'âge du compte Discord.
    Retourne (is_eligible, age_in_days).
    """
    age_days = (datetime.now(timezone.utc) - created_at).days
    return age_days >= min_days, age_days


def check_server_age(joined_at: datetime, min_days: int = 15) -> tuple[bool, int]:
    """
    Vérifie l'ancienneté sur le serveur.
    Retourne (is_eligible, age_in_days).
    """
    age_days = (datetime.now(timezone.utc) - joined_at).days
    return age_days >= min_days, age_days


# ======================================================================
# DÉTECTION MULTI-COMPTES / COMPORTEMENTS SUSPECTS
# ======================================================================

class FraudDetector:
    """
    Détection heuristique d'abus.
    Ne remplace pas la supervision admin — génère des signaux.
    """

    def __init__(self):
        # Historique des transactions récentes par user (RAM)
        self._recent_tx: dict[int, list[dict]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def record_transaction(
        self,
        from_user: Optional[int],
        to_user: Optional[int],
        tx_type: str,
        amount: int,
    ) -> list[str]:
        """
        Enregistre une transaction et retourne la liste des signaux détectés.
        """
        signals = []
        now = time.time()
        window = 3600  # 1 heure

        async with self._lock:
            for uid in filter(None, [from_user, to_user]):
                # Purger les anciens enregistrements
                self._recent_tx[uid] = [
                    t for t in self._recent_tx[uid] if t["ts"] > now - window
                ]
                self._recent_tx[uid].append({
                    "ts": now,
                    "from": from_user,
                    "to": to_user,
                    "type": tx_type,
                    "amount": amount,
                })

            # Détecter transactions circulaires (A→B→C→A dans 1h)
            if from_user and to_user:
                signals.extend(self._detect_circular(from_user, to_user, now, window))

        return signals

    def _detect_circular(
        self,
        from_user: int,
        to_user: int,
        now: float,
        window: float,
    ) -> list[str]:
        """Détecte si from_user a reçu de to_user récemment (A→B et B→A)."""
        signals = []
        to_user_txs = self._recent_tx.get(to_user, [])
        for tx in to_user_txs:
            if tx["from"] == to_user and tx["to"] == from_user and tx["ts"] > now - window:
                signals.append(
                    f"CIRCULAR_TX: {from_user}→{to_user} et {to_user}→{from_user} dans 1h"
                )
        return signals

    async def detect_rapid_account_creation(
        self,
        new_user_id: int,
        account_created_at: datetime,
        server_joined_at: datetime,
    ) -> list[str]:
        """Vérifie si le compte est suspect (trop récent)."""
        signals = []
        account_age_days = (datetime.now(timezone.utc) - account_created_at).days
        server_age_days = (datetime.now(timezone.utc) - server_joined_at).days

        if account_age_days < 7:
            signals.append(f"NEW_ACCOUNT: Compte Discord créé il y a seulement {account_age_days} jours")
        if server_age_days < 3:
            signals.append(f"NEW_MEMBER: Sur le serveur depuis seulement {server_age_days} jours")

        return signals


fraud_detector = FraudDetector()


# ======================================================================
# CONFIRMATION ADMIN (2FA légère)
# ======================================================================

class AdminConfirmation:
    """
    Système de confirmation pour les actions admin critiques.
    Génère un code à 6 chiffres, valable 30 secondes.
    """

    def __init__(self):
        self._pending: dict[int, tuple[str, float]] = {}  # admin_id → (code, expiry)
        self._lock = asyncio.Lock()

    async def generate_code(self, admin_id: int) -> str:
        """Génère et stocke un code de confirmation."""
        # Code aléatoire 6 chiffres via os.urandom (cryptographiquement sûr)
        raw = int.from_bytes(os.urandom(4), "big") % 1_000_000
        code = f"{raw:06d}"
        async with self._lock:
            self._pending[admin_id] = (code, time.time() + 30)
        return code

    async def verify_code(self, admin_id: int, code: str) -> bool:
        """Vérifie le code. Consomme le code si correct (one-time)."""
        async with self._lock:
            pending = self._pending.get(admin_id)
            if not pending:
                return False
            stored_code, expiry = pending
            if time.time() > expiry:
                del self._pending[admin_id]
                return False
            if hashlib.sha256(code.encode()).hexdigest() == hashlib.sha256(stored_code.encode()).hexdigest():
                del self._pending[admin_id]
                return True
            return False

    async def cancel(self, admin_id: int) -> None:
        async with self._lock:
            self._pending.pop(admin_id, None)


admin_confirmation = AdminConfirmation()

# Actions nécessitant une confirmation
CRITICAL_ADMIN_ACTIONS = {
    "rollback",
    "ban_user",
    "blockchain_lock",
    "blockchain_unlock",
    "fund_create",
}
