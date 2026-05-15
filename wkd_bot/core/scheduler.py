"""
core/scheduler.py
Tâches planifiées : taxes hebdomadaires, airdrops automatiques,
surveillance jury, backups Google Cloud.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

logger = logging.getLogger("wkd.scheduler")


class Scheduler:
    """
    Gestionnaire de tâches planifiées pour le bot WKD.
    Utilise asyncio.Task — pas de dépendance externe (schedule lib).
    """

    def __init__(self, bot: "discord.ext.commands.Bot"):
        self.bot = bot
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def start(self) -> None:
        """Démarre toutes les tâches planifiées."""
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._weekly_tax_loop(), name="weekly_tax"),
            asyncio.create_task(self._airdrop_loop(), name="airdrop"),
            asyncio.create_task(self._jury_watchdog_loop(), name="jury_watchdog"),
            asyncio.create_task(self._contract_expire_loop(), name="contract_expire"),
            asyncio.create_task(self._daily_backup_loop(), name="daily_backup"),
            asyncio.create_task(self._rate_limiter_cleanup_loop(), name="rl_cleanup"),
        ]
        logger.info(f"{len(self._tasks)} tâches planifiées démarrées")

    async def stop(self) -> None:
        """Arrête toutes les tâches proprement."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Toutes les tâches planifiées arrêtées")

    # ------------------------------------------------------------------
    # CALCUL : secondes jusqu'au prochain moment donné
    # ------------------------------------------------------------------

    @staticmethod
    def _seconds_until_next(weekday: int, hour: int = 0, minute: int = 0) -> float:
        """
        Calcule le délai en secondes jusqu'au prochain `weekday` (0=lundi, 5=samedi)
        à `hour:minute` UTC.
        """
        now = datetime.now(timezone.utc)
        days_ahead = weekday - now.weekday()
        if days_ahead < 0 or (days_ahead == 0 and (now.hour, now.minute) >= (hour, minute)):
            days_ahead += 7
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        target += timedelta(days=days_ahead)
        return (target - now).total_seconds()

    @staticmethod
    def _seconds_until_next_midnight() -> float:
        """Calcule les secondes jusqu'à minuit UTC."""
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (tomorrow - now).total_seconds()

    @staticmethod
    def _seconds_until_next_hour_check(interval_seconds: int = 3600) -> float:
        """Délai jusqu'au prochain check toutes les N secondes."""
        return interval_seconds

    # ------------------------------------------------------------------
    # TAXE HEBDOMADAIRE (samedi minuit)
    # ------------------------------------------------------------------

    async def _weekly_tax_loop(self) -> None:
        """Boucle : attend le samedi minuit UTC puis applique les taxes."""
        while self._running:
            try:
                delay = self._seconds_until_next(weekday=5, hour=0, minute=0)
                logger.info(f"Prochaine taxe dans {delay/3600:.1f}h")
                await asyncio.sleep(delay)
                await self._run_weekly_taxes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur loop taxes: {e}", exc_info=True)
                await asyncio.sleep(60)  # Retry dans 1 min

    async def _run_weekly_taxes(self) -> None:
        from core.economy import apply_weekly_taxes
        from utils.notifications import NotificationService

        logger.info("=== APPLICATION DES TAXES HEBDOMADAIRES ===")
        try:
            results = await apply_weekly_taxes()
            notif = NotificationService(self.bot)

            # Notifier les utilisateurs taxés
            for entry in results["inactive"]:
                await notif.send_tax_notification(
                    user_id=entry["user_id"],
                    tax_amount=entry["tax"],
                    tax_type="inactive",
                )
                await asyncio.sleep(0.5)  # Rate limit Discord

            for entry in results["rich"]:
                await notif.send_tax_notification(
                    user_id=entry["user_id"],
                    tax_amount=entry["tax"],
                    tax_type="rich",
                )
                await asyncio.sleep(0.5)

            logger.info(
                f"Taxes OK — {len(results['inactive'])} inactifs, "
                f"{len(results['rich'])} riches, "
                f"{len(results['errors'])} erreurs"
            )

            # Post public dans #transactions (optionnel)
            await notif.post_tax_summary(results)

        except Exception as e:
            logger.error(f"Erreur lors des taxes: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # AIRDROPS AUTOMATIQUES (toutes les 6h)
    # ------------------------------------------------------------------

    async def _airdrop_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(6 * 3600)  # Vérification toutes les 6h
                await self._run_airdrops()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur loop airdrops: {e}", exc_info=True)
                await asyncio.sleep(300)

    async def _run_airdrops(self) -> None:
        from core.economy import process_pending_airdrops
        from utils.notifications import NotificationService

        try:
            distributed = await process_pending_airdrops()
            notif = NotificationService(self.bot)

            for entry in distributed:
                await notif.send_airdrop_received(
                    user_id=entry["user_id"],
                    amount=entry["amount"],
                )
                await asyncio.sleep(0.5)

            if distributed:
                logger.info(f"Airdrops distribués : {len(distributed)} membres")
        except Exception as e:
            logger.error(f"Erreur airdrops: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # SURVEILLANCE JURY (toutes les 30 min)
    # ------------------------------------------------------------------

    async def _jury_watchdog_loop(self) -> None:
        """Remplace les jurés qui n'ont pas voté dans le délai."""
        while self._running:
            try:
                await asyncio.sleep(30 * 60)
                await self._check_overdue_jurors()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur jury watchdog: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_overdue_jurors(self) -> None:
        from database.connection import transaction
        from database.repositories.transactions import BetRepository
        from database.repositories.config import ConfigRepository
        from utils.notifications import NotificationService

        try:
            async with transaction() as conn:
                bets_repo = BetRepository(conn)
                overdue = await bets_repo.get_overdue_jurors()

            if not overdue:
                return

            logger.info(f"Jurés en retard : {len(overdue)}")
            notif = NotificationService(self.bot)

            for entry in overdue:
                try:
                    await self._replace_juror(
                        bet_id=entry["bet_id"],
                        overdue_juror_id=entry["juror_id"],
                        phase=entry["vote_phase"],
                    )
                    await notif.send_jury_timeout_notification(entry["juror_id"])
                except Exception as e:
                    logger.error(f"Remplacement juré {entry['juror_id']} échoué: {e}")
        except Exception as e:
            logger.error(f"Erreur vérification jurés: {e}", exc_info=True)

    async def _replace_juror(
        self, bet_id: str, overdue_juror_id: int, phase: str
    ) -> None:
        """Remplace un juré en retard par un nouveau membre du pool."""
        from database.connection import transaction
        from database.repositories.transactions import BetRepository
        from database.repositories.config import ConfigRepository
        from utils.notifications import NotificationService

        async with transaction() as conn:
            bets_repo = BetRepository(conn)
            cfg = ConfigRepository(conn)

            bet = await bets_repo.get_bet(bet_id)
            if not bet:
                return

            parieurs = {bet["bettor_a"], bet["bettor_b"]}
            current_votes = await bets_repo.get_active_votes(bet_id, phase)
            current_jurors = {v["juror_id"] for v in current_votes}
            all_excluded = parieurs | current_jurors | {overdue_juror_id}

            pool = await bets_repo.get_jury_pool()
            eligible = [m for m in pool if m["user_id"] not in all_excluded]

            if not eligible:
                logger.warning(f"Pool jury vide pour remplacement pari {bet_id}")
                return

            replacement = random.choice(eligible)
            replacement_hours = await cfg.get_int("jury_vote_replacement_hours", 12)
            deadline = datetime.now(timezone.utc) + timedelta(hours=replacement_hours)

            await bets_repo.register_replacement_juror(
                bet_id=bet_id,
                new_juror_id=replacement["user_id"],
                replaced_juror_id=overdue_juror_id,
                deadline=deadline,
                phase=phase,
            )

        # Notification hors transaction
        notif = NotificationService(self.bot)
        await notif.send_jury_assignment(
            user_id=replacement["user_id"],
            bet_id=bet_id,
            deadline_hours=replacement_hours,
            is_replacement=True,
        )
        logger.info(
            f"Juré {overdue_juror_id} remplacé par {replacement['user_id']} pour pari {bet_id}"
        )

    # ------------------------------------------------------------------
    # EXPIRATION DES CONTRATS (toutes les heures)
    # ------------------------------------------------------------------

    async def _contract_expire_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(3600)
                from database.connection import transaction
                from database.repositories.transactions import ContractRepository

                async with transaction() as conn:
                    contracts = ContractRepository(conn)
                    count = await contracts.expire_old_contracts()
                    if count > 0:
                        logger.info(f"{count} contrat(s) expirés")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur expiration contrats: {e}", exc_info=True)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # BACKUP QUOTIDIEN (minuit UTC)
    # ------------------------------------------------------------------

    async def _daily_backup_loop(self) -> None:
        while self._running:
            try:
                delay = self._seconds_until_next_midnight()
                logger.info(f"Prochain backup dans {delay/3600:.1f}h")
                await asyncio.sleep(delay)
                await self._run_backup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur backup: {e}", exc_info=True)
                await asyncio.sleep(300)

    async def _run_backup(self) -> None:
        from backup.gcloud import perform_backup
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, perform_backup)
            logger.info(f"Backup terminé : {result}")
        except Exception as e:
            logger.error(f"Backup échoué : {e}", exc_info=True)

    # ------------------------------------------------------------------
    # NETTOYAGE RATE LIMITER (toutes les 10 min)
    # ------------------------------------------------------------------

    async def _rate_limiter_cleanup_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(600)
                from core.security import rate_limiter
                count = await rate_limiter.cleanup()
                if count > 0:
                    logger.debug(f"Rate limiter: {count} entrées expirées supprimées")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur cleanup rate limiter: {e}", exc_info=True)
