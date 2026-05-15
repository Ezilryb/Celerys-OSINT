"""
backup/gcloud.py
Backup PostgreSQL vers Google Cloud Storage.
Exécuté en thread séparé (run_in_executor) pour ne pas bloquer asyncio.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("wkd.backup")


def perform_backup() -> str:
    """
    1. pg_dump de la base WKD
    2. Compression gzip
    3. Upload vers Google Cloud Storage
    4. Suppression des backups > 30 jours
    Retourne le nom du fichier uploadé.
    """
    db_name = os.getenv("POSTGRES_DB", "wkd_database")
    db_user = os.getenv("POSTGRES_USER", "wkd_bot")
    db_host = os.getenv("POSTGRES_HOST", "localhost")
    db_port = os.getenv("POSTGRES_PORT", "5432")
    bucket_name = os.getenv("GCS_BUCKET_NAME", "wkd-backups")
    gcs_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_filename = f"wkd_backup_{timestamp}.sql.gz"

    with tempfile.TemporaryDirectory() as tmpdir:
        sql_path = os.path.join(tmpdir, f"wkd_backup_{timestamp}.sql")
        gz_path = os.path.join(tmpdir, backup_filename)

        # --- 1. pg_dump ---
        env = {**os.environ, "PGPASSWORD": os.getenv("POSTGRES_PASSWORD", "")}
        result = subprocess.run(
            [
                "pg_dump",
                "-h", db_host,
                "-p", db_port,
                "-U", db_user,
                "-d", db_name,
                "-f", sql_path,
                "--no-password",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(f"pg_dump échoué : {result.stderr}")

        logger.info(f"pg_dump OK : {sql_path}")

        # --- 2. Compression gzip ---
        with open(sql_path, "rb") as f_in:
            with gzip.open(gz_path, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)

        logger.info(f"Compression OK : {gz_path}")

        # --- 3. Upload vers GCS ---
        try:
            from google.cloud import storage

            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(f"backups/{backup_filename}")
            blob.upload_from_filename(gz_path)

            logger.info(f"Upload GCS OK : gs://{bucket_name}/backups/{backup_filename}")

            # --- 4. Nettoyage backups anciens ---
            _cleanup_old_backups(bucket, days=30)

        except ImportError:
            logger.warning("google-cloud-storage non installé — backup local uniquement")
            # Fallback : copie locale
            local_backup_dir = os.getenv("LOCAL_BACKUP_DIR", "/home/wkd/backups")
            os.makedirs(local_backup_dir, exist_ok=True)
            local_dest = os.path.join(local_backup_dir, backup_filename)
            shutil.copy2(gz_path, local_dest)
            logger.info(f"Backup local : {local_dest}")

    return backup_filename


def _cleanup_old_backups(bucket, days: int = 30) -> int:
    """Supprime les backups GCS de plus de `days` jours. Retourne le nombre supprimé."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = 0

    blobs = list(bucket.list_blobs(prefix="backups/"))
    for blob in blobs:
        if blob.time_created and blob.time_created < cutoff:
            try:
                blob.delete()
                deleted += 1
                logger.info(f"Backup supprimé (> {days}j) : {blob.name}")
            except Exception as e:
                logger.warning(f"Impossible de supprimer {blob.name}: {e}")

    return deleted


def restore_from_backup(backup_filename: str, target_db: str = None) -> bool:
    """
    Restaure un backup GCS (usage admin uniquement, jamais automatisé).
    À appeler MANUELLEMENT uniquement.
    """
    bucket_name = os.getenv("GCS_BUCKET_NAME", "wkd-backups")
    db_name = target_db or os.getenv("POSTGRES_DB", "wkd_database")
    db_user = os.getenv("POSTGRES_USER", "wkd_bot")
    db_host = os.getenv("POSTGRES_HOST", "localhost")

    logger.warning(f"RESTAURATION BACKUP : {backup_filename} → {db_name}")

    try:
        from google.cloud import storage

        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(f"backups/{backup_filename}")

        with tempfile.TemporaryDirectory() as tmpdir:
            gz_path = os.path.join(tmpdir, backup_filename)
            sql_path = gz_path.replace(".gz", "")

            blob.download_to_filename(gz_path)

            with gzip.open(gz_path, "rb") as f_in:
                with open(sql_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

            env = {**os.environ, "PGPASSWORD": os.getenv("POSTGRES_PASSWORD", "")}
            result = subprocess.run(
                ["psql", "-h", db_host, "-U", db_user, "-d", db_name, "-f", sql_path],
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                logger.error(f"Restauration échouée : {result.stderr}")
                return False

        logger.info("Restauration terminée avec succès")
        return True

    except Exception as e:
        logger.error(f"Erreur restauration : {e}", exc_info=True)
        return False
