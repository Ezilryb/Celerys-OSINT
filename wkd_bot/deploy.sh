#!/bin/bash
# ============================================================
# WKD Bot — Script de déploiement Raspberry Pi 5
# Usage : bash deploy.sh
# ============================================================

set -euo pipefail

echo "🔥 WKD Bot — Déploiement Raspberry Pi 5"
echo "========================================"

# ---- Variables ----
BOT_USER="wkd"
BOT_DIR="/home/${BOT_USER}/wkd_bot"
VENV_DIR="/home/${BOT_USER}/venv"
SERVICE_NAME="wkd-bot"
PYTHON_MIN="3.11"

# ---- Couleurs ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

# ---- 1. Vérification root ----
if [[ $EUID -ne 0 ]]; then
    error "Ce script doit être exécuté en tant que root (sudo bash deploy.sh)"
fi

# ---- 2. Mise à jour système ----
info "Mise à jour des paquets système..."
apt-get update -qq
apt-get upgrade -y -qq

# ---- 3. Dépendances système ----
info "Installation des dépendances système..."
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    postgresql postgresql-contrib \
    libpq-dev \
    git curl wget \
    build-essential

# ---- 4. Vérification version Python ----
PYTHON_VERSION=$(python3 --version | grep -oP '\d+\.\d+')
info "Python version : ${PYTHON_VERSION}"

# ---- 5. Création utilisateur système ----
if ! id "${BOT_USER}" &>/dev/null; then
    info "Création de l'utilisateur ${BOT_USER}..."
    useradd -r -m -s /bin/bash "${BOT_USER}"
else
    info "Utilisateur ${BOT_USER} déjà existant"
fi

# ---- 6. Création des dossiers ----
info "Création de l'arborescence..."
mkdir -p "${BOT_DIR}" "${BOT_DIR}/logs" "/home/${BOT_USER}/backups"
chown -R "${BOT_USER}:${BOT_USER}" "/home/${BOT_USER}"

# ---- 7. Configuration PostgreSQL ----
info "Configuration PostgreSQL..."

# Démarrer PostgreSQL
systemctl start postgresql
systemctl enable postgresql

# Lire les variables depuis .env si il existe
ENV_FILE="${BOT_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
    DB_NAME=$(grep "^POSTGRES_DB=" "${ENV_FILE}" | cut -d= -f2 || echo "wkd_database")
    DB_USER=$(grep "^POSTGRES_USER=" "${ENV_FILE}" | cut -d= -f2 || echo "wkd_bot")
    DB_PASS=$(grep "^POSTGRES_PASSWORD=" "${ENV_FILE}" | cut -d= -f2 || echo "")
else
    DB_NAME="wkd_database"
    DB_USER="wkd_bot"
    DB_PASS=""
    warning ".env non trouvé — créez ${ENV_FILE} depuis .env.example avant de continuer"
fi

# Créer l'utilisateur et la base
sudo -u postgres psql -tc "SELECT 1 FROM pg_user WHERE usename = '${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};"
sudo -u postgres psql -d "${DB_NAME}" -c "GRANT ALL ON SCHEMA public TO ${DB_USER};"

info "PostgreSQL configuré : ${DB_USER}@localhost/${DB_NAME}"

# ---- 8. Environnement virtuel Python ----
info "Création de l'environnement virtuel Python..."
if [[ ! -d "${VENV_DIR}" ]]; then
    sudo -u "${BOT_USER}" python3 -m venv "${VENV_DIR}"
fi

info "Installation des dépendances Python..."
sudo -u "${BOT_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip -q
sudo -u "${BOT_USER}" "${VENV_DIR}/bin/pip" install -r "${BOT_DIR}/requirements.txt" -q

info "Dépendances installées"

# ---- 9. Dossier logs ----
mkdir -p "${BOT_DIR}/logs"
chown -R "${BOT_USER}:${BOT_USER}" "${BOT_DIR}/logs"

# ---- 10. Service systemd ----
info "Installation du service systemd..."
cp "${BOT_DIR}/wkd-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

# ---- 11. Configuration PostgreSQL sécurisée ----
info "Sécurisation PostgreSQL..."
PG_CONF="/etc/postgresql/*/main/postgresql.conf"
# Écoute localhost uniquement (pas d'accès réseau externe)
sed -i "s/#listen_addresses = 'localhost'/listen_addresses = 'localhost'/" $PG_CONF 2>/dev/null || true

systemctl restart postgresql

# ---- 12. Vérification .env ----
if [[ ! -f "${ENV_FILE}" ]]; then
    warning "ATTENTION : ${ENV_FILE} n'existe pas !"
    warning "Copiez .env.example → .env et remplissez vos valeurs AVANT de démarrer le bot."
    echo ""
    echo "    cp ${BOT_DIR}/.env.example ${BOT_DIR}/.env"
    echo "    nano ${BOT_DIR}/.env"
    echo ""
else
    info "Fichier .env trouvé"
fi

# ---- Résumé ----
echo ""
echo "========================================"
echo -e "${GREEN}✅ Déploiement terminé !${NC}"
echo ""
echo "Prochaines étapes :"
echo "  1. Vérifiez/complétez ${ENV_FILE}"
echo "  2. Démarrez le bot :"
echo "       sudo systemctl start ${SERVICE_NAME}"
echo "  3. Vérifiez les logs :"
echo "       sudo journalctl -u ${SERVICE_NAME} -f"
echo "       tail -f ${BOT_DIR}/logs/wkd_bot.log"
echo ""
echo "Commandes utiles :"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  sudo systemctl stop ${SERVICE_NAME}"
echo "========================================"
