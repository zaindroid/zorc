#!/usr/bin/env bash
# Idempotent: install/refresh the Zetaris hackathon platform for Groundswell on servingz, internal only.
#   bootstrap/20-zetaris-groundswell.sh [path/to/zetaris-platform.zip]
# Prereqs: `docker login zetarishackathon.azurecr.io` done once by a human (registry credentials from the
# organiser's "Run the platform locally" page); the groundswell-infra Coolify service running (creates the
# groundswell-net network); ~/apps/groundswell cloned. See ~/apps/groundswell/infra/zetaris/README.md.
set -euo pipefail
ZIP=${1:-$HOME/apps/zetaris/zetaris-platform.zip}
DIR=$HOME/apps/zetaris/zetaris-platform
GS=$HOME/apps/groundswell/infra/zetaris

if [ ! -d "$DIR" ]; then
  [ -f "$ZIP" ] || { echo "no $DIR and no zip at $ZIP"; exit 2; }
  mkdir -p "$(dirname "$DIR")"
  python3 -m zipfile -e "$ZIP" "$(dirname "$DIR")"   # no unzip on servingz
  chmod +x "$DIR"/*.sh
fi
cd "$DIR"

# Loopback-only UI and the guide's 8 GB / 4 CPU sizing; the vendor .env is kept once as .env.vendor.
[ -f .env.vendor ] || cp .env .env.vendor
sed -i 's|^UI_PORT=.*|UI_PORT=127.0.0.1:3000|; s|^ZSPARK_DRIVER_MEM=.*|ZSPARK_DRIVER_MEM=4g|;
        s|^ZSPARK_DRIVER_CORES=.*|ZSPARK_DRIVER_CORES=4|; s|^ZSPARK_MASTER_URL=.*|ZSPARK_MASTER_URL=local[4]|;
        s|^ZETARIS_API_MAX_HEAP=.*|ZETARIS_API_MAX_HEAP=2g|; s|^TELEMETRY_ENABLED=.*|TELEMETRY_ENABLED=false|' .env
chmod 600 .env

cp "$GS/docker-compose.override.yml" docker-compose.override.yml
[ -f conf/spark-defaults.conf.vendor ] || cp conf/spark-defaults.conf conf/spark-defaults.conf.vendor
cat conf/spark-defaults.conf.vendor "$GS/spark-defaults.groundswell.conf" > conf/spark-defaults.conf

docker network inspect groundswell-net >/dev/null || { echo "groundswell-net missing: start groundswell-infra first"; exit 2; }
# Refuse to start if anything would publish on a non-loopback address.
if docker compose config --format json | python3 -c '
import json, sys
bad = [(n, p.get("published")) for n, s in json.load(sys.stdin)["services"].items()
       for p in s.get("ports", []) if p.get("host_ip") != "127.0.0.1"]
sys.exit(1 if bad else 0)'; then :; else echo "refusing: a port would bind beyond 127.0.0.1"; exit 1; fi

./preflight.sh
docker compose pull -q
docker compose up -d
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Status}}' zetaris-bootstrap-1 2>/dev/null)" = exited ] && break
  sleep 10
done
./verify.sh
