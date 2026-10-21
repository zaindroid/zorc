#!/usr/bin/env bash
# Idempotent: add Mosca's per-bucket S3A block to the shared Zetaris platform (installed by
# 20-zetaris-groundswell.sh) and restart the query engine only when the config changed.
#   bootstrap/21-zetaris-mosca.sh
# Prereqs: the lab-mosca-pg Coolify service running (lab-mosca-s3-alias answers as mosca.minio on groundswell-net),
# bucket `mosca` and its access key on the shared MinIO. Sources are registered afterwards with
#   cd ~/apps/openhack-mosca && .venv/bin/python app/zetaris/zsql.py -f app/zetaris/register_sources.sql
set -euo pipefail
DIR=$HOME/apps/zetaris/zetaris-platform
UC=$HOME/apps/openhack-mosca/app/zetaris/spark-defaults.mosca.conf
CONF=$DIR/conf/spark-defaults.conf
[ -f "$CONF" ] && [ -f "$UC" ] || { echo "missing $CONF or $UC"; exit 2; }
if grep -q "fs.s3a.bucket.mosca.endpoint" "$CONF"; then
  echo "mosca S3A block already present"
else
  cat "$UC" >> "$CONF"
  echo "appended mosca S3A block; restarting the query engine"
  (cd "$DIR" && docker compose restart driver)
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' zetaris-driver-1)" = healthy ] && break
    sleep 5
  done
  docker inspect -f 'driver {{.State.Health.Status}}' zetaris-driver-1
fi
