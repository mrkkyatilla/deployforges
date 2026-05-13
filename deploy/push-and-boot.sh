#!/usr/bin/env bash
# Yerel makineden DeployForge'u VPS'e rsync + Docker ile ayağa kaldırır.
# Önkoşul: ssh root@188.132.197.229 parolasız (public key) çalışıyor olmalı.
#
# Kullanım (deployforges repo kökünden veya deploy/ içinden):
#   ./deploy/push-and-boot.sh
#
# Opsiyonel:
#   DEPLOY_SERVER=root@IP ./deploy/push-and-boot.sh
#   DEPLOY_REMOTE=/opt/deployforges ./deploy/push-and-boot.sh
#   SKIP_LOCAL_ENV=1 ./deploy/push-and-boot.sh   # yerel .env'i sunucuya kopyalama
#
set -euo pipefail

DEPLOY_SERVER="${DEPLOY_SERVER:-root@188.132.197.229}"
DEPLOY_REMOTE="${DEPLOY_REMOTE:-/opt/deployforges}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/deployforge_wrupup}"
SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
RSYNC=(rsync -az --delete --human-readable -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new")

EXCLUDES=(
  --exclude '.git'
  --exclude '__pycache__'
  --exclude '.venv'
  --exclude '*.pyc'
  --exclude '.pytest_cache'
  --exclude '*.egg-info'
)

echo "==> Hedef: ${DEPLOY_SERVER}:${DEPLOY_REMOTE}"
"${SSH[@]}" "${DEPLOY_SERVER}" "mkdir -p '${DEPLOY_REMOTE}'"

echo "==> Kod senkronu (rsync)..."
"${RSYNC[@]}" "${EXCLUDES[@]}" "${ROOT}/" "${DEPLOY_SERVER}:${DEPLOY_REMOTE}/"

if [[ "${SKIP_LOCAL_ENV:-0}" != "1" && -f "${ROOT}/.env" ]]; then
  echo "==> Yerel .env sunucuya kopyalanıyor..."
  rsync -az -e "ssh -i $SSH_KEY" "${ROOT}/.env" "${DEPLOY_SERVER}:${DEPLOY_REMOTE}/.env"
fi

echo "==> Sunucuda Docker + migrate + compose..."
"${SSH[@]}" "${DEPLOY_SERVER}" \
  "REMOTE_DIR=${DEPLOY_REMOTE}" \
  bash -s <<'REMOTE_BOOT'
set -euo pipefail
cd "$REMOTE_DIR"

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
  systemctl enable docker --now 2>/dev/null || true
}

install_docker

COMPOSE=(docker compose -f deploy/docker-compose.vps.yml --env-file .env)

if [[ ! -f .env ]]; then
  echo "==> .env yok; şablondan oluşturuluyor..."
  cp deploy/.env.deploy.example .env
  DBPW="$(openssl rand -hex 24)"
  ADMIN="$(openssl rand -hex 32)"
  sed -i "s/^DB_PASSWORD=.*/DB_PASSWORD=${DBPW}/" .env
  sed -i "s/^DF_ADMIN_API_KEY=.*/DF_ADMIN_API_KEY=${ADMIN}/" .env
  echo "    UYARI: DF_GEMINI_API_KEY boş — sunucuda nano .env ile doldurun, sonra: docker compose ... up -d --build"
fi

echo "==> db + redis başlatılıyor..."
"${COMPOSE[@]}" up -d db redis

echo "==> Postgres hazır bekleniyor..."
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T db sh -c 'pg_isready -U "${POSTGRES_USER}"' 2>/dev/null; then
    break
  fi
  sleep 1
done
if ! "${COMPOSE[@]}" exec -T db sh -c 'pg_isready -U "${POSTGRES_USER}"' 2>/dev/null; then
  echo "Postgres ayakta değil." >&2
  exit 1
fi

echo "==> Alembic migrate..."
"${COMPOSE[@]}" run --rm api alembic upgrade head

echo "==> Tam stack (build + up)..."
"${COMPOSE[@]}" up -d --build

echo "==> Durum:"
"${COMPOSE[@]}" ps
REMOTE_BOOT

echo ""
echo "Tamam. Kontrol: curl -sS https://deploy.wrupup.com/api/v1/health"
echo "(DNS / TLS yayılımı birkaç dakika sürebilir.)"
