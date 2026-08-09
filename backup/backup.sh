#!/usr/bin/env bash
# servingz backup — shared app Postgres + Coolify's own Postgres + Coolify's
# config directory (SSH keys, SSL certs, and critically its APP_KEY, the
# encryption key for every secret Coolify stores — without it a restored
# coolify-db is useless). Redis is deliberately NOT backed up: AGENTS.md
# documents it as cache/queue only, nothing irreplaceable.
#
# Archive is gpg-encrypted locally before it ever leaves the host, then
# uploaded to Cloudflare R2. Local copies kept 7 days, remote copies 30 days.
set -euo pipefail

BACKUP_DIR="/home/zman/zorc/backup"
SECRETS="$BACKUP_DIR/secrets"
STAGING="/mnt/data/backups"
RCLONE_CONF="$SECRETS/rclone.conf"
R2_JSON="$SECRETS/r2.json"
GPG_PASS_FILE="$SECRETS/gpg_passphrase"
NOTIFY_JSON="/home/zman/zorc/monitoring/secrets/notify.json"
HC_JSON="$SECRETS/healthchecks.json"

TS="$(date -u +%Y-%m-%d_%H%M%S)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

BUCKET="$(python3 -c "import json; print(json.load(open('$R2_JSON'))['bucket'])")"
NTFY_TOPIC="$(python3 -c "import json; print(json.load(open('$NOTIFY_JSON')).get('ntfy_topic',''))" 2>/dev/null || true)"
HC_URL="$(python3 -c "import json; print(json.load(open('$HC_JSON')).get('ping_url',''))" 2>/dev/null || true)"

alert_failure() {
  local msg="$1"
  echo "FAILED: $msg" >&2
  if [ -n "$NTFY_TOPIC" ]; then
    curl -fsS -H "Title: Critical: Backup failed — servingz" \
         -H "Priority: urgent" -H "Tags: rotating_light" \
         -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null || true
  fi
  if [ -n "$HC_URL" ]; then
    curl -fsS "$HC_URL/fail" >/dev/null || true
  fi
}
trap 'alert_failure "backup.sh exited non-zero at line $LINENO"' ERR

mkdir -p "$STAGING"

echo "[$TS] dumping shared app postgres..."
docker exec t5amapezxhesfta6w82ksyt0 pg_dumpall -U postgres | gzip > "$WORKDIR/app_postgres.sql.gz"

echo "[$TS] dumping coolify's own postgres..."
docker exec coolify-db pg_dumpall -U coolify | gzip > "$WORKDIR/coolify_postgres.sql.gz"

echo "[$TS] archiving coolify config (ssh keys, ssl, APP_KEY)..."
sudo tar czf "$WORKDIR/coolify_data.tar.gz" -C /data coolify

ARCHIVE="$WORKDIR/backup_${TS}.tar.gz"
tar czf "$ARCHIVE" -C "$WORKDIR" app_postgres.sql.gz coolify_postgres.sql.gz coolify_data.tar.gz

echo "[$TS] encrypting..."
ENCRYPTED="$STAGING/backup_${TS}.tar.gz.gpg"
gpg --batch --yes --passphrase-file "$GPG_PASS_FILE" \
    --symmetric --cipher-algo AES256 -o "$ENCRYPTED" "$ARCHIVE"

echo "[$TS] uploading to R2..."
rclone --config "$RCLONE_CONF" copy "$ENCRYPTED" "r2:${BUCKET}/servingz/"

echo "[$TS] pruning local backups older than 7 days..."
find "$STAGING" -name 'backup_*.tar.gz.gpg' -mtime +7 -delete

echo "[$TS] pruning remote backups older than 30 days..."
rclone --config "$RCLONE_CONF" delete "r2:${BUCKET}/servingz/" --min-age 30d

SIZE="$(du -h "$ENCRYPTED" | cut -f1)"
echo "[$TS] done — $SIZE"

if [ -n "$HC_URL" ]; then
  curl -fsS "$HC_URL" -d "backup ${TS} ok, ${SIZE}" >/dev/null || true
fi
