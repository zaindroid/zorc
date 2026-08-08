#!/bin/bash
set -euo pipefail
# One-time migration performed 2026-08-08: format the newly-installed NVMe
# (Samsung MZVLQ512HBLU-00BH1, /dev/nvme0n1) and move Docker's data-root
# onto it while Coolify's databases were still empty (clean window). NOT
# safe to blindly re-run against a live system with real data -- this
# documents the exact steps taken, guarded to no-op if already applied.
NVME_UUID="1e3adfbf-3d2f-413a-9c9d-78e648790492"

if ! blkid -U "$NVME_UUID" >/dev/null 2>&1; then
  echo "NVMe partition with UUID $NVME_UUID not found -- nothing to do (already migrated, or hardware differs)" >&2
  exit 0
fi

if ! grep -q "$NVME_UUID" /etc/fstab; then
  echo "UUID=$NVME_UUID /mnt/fast ext4 defaults,noatime,nofail 0 2" | sudo tee -a /etc/fstab
fi
sudo mkdir -p /mnt/fast
sudo mount -a

if [ "$(docker info 2>/dev/null | grep 'Docker Root Dir' | awk '{print $NF}')" = "/mnt/fast/docker" ]; then
  echo "Docker data-root already on /mnt/fast/docker -- nothing to do"
  exit 0
fi

echo "This performs a one-time data-root migration and must be run manually," >&2
echo "not automatically -- see docs/inventory.md and git history for the exact steps." >&2
exit 1
