#!/usr/bin/env bash
# Idempotent: add Undercurrent's per-bucket S3A block to the shared Zetaris platform (installed by
# 20-zetaris-groundswell.sh) and restart the query engine only when the config changed.
#   bootstrap/21-zetaris-undercurrent.sh
# Prereqs: the undercurrent Coolify service running (uc-s3-alias answers as undercurrent.minio on groundswell-net),
# bucket `undercurrent` and its access key on the shared MinIO. Sources are registered afterwards with
#   cd ~/apps/undercurrent && PYTHONPATH=. .venv/bin/python infra/zetaris/zsql.py --keep-going -f infra/zetaris/register_sources.sql
set -euo pipefail
DIR=$HOME/apps/zetaris/zetaris-platform
UC=$HOME/apps/undercurrent/infra/zetaris/spark-defaults.undercurrent.conf
CONF=$DIR/conf/spark-defaults.conf
[ -f "$CONF" ] && [ -f "$UC" ] || { echo "missing $CONF or $UC"; exit 2; }
if grep -q "fs.s3a.bucket.undercurrent.endpoint" "$CONF"; then
  echo "undercurrent S3A block already present"
else
  cat "$UC" >> "$CONF"
  echo "appended undercurrent S3A block; restarting the query engine"
  (cd "$DIR" && docker compose restart driver)
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' zetaris-driver-1)" = healthy ] && break
    sleep 5
  done
  docker inspect -f 'driver {{.State.Health.Status}}' zetaris-driver-1
fi
