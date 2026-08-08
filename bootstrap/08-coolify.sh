#!/bin/bash
set -euo pipefail
if ! docker ps --format '{{.Names}}' | grep -q '^coolify$'; then
  curl -fsSL https://cdn.coollabs.io/coolify/install.sh | sudo bash
fi
