#!/bin/bash
set -euo pipefail
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
if [ ! -f /etc/apt/sources.list.d/docker.list ]; then
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
    > /etc/apt/sources.list.d/docker.list
fi
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker zman
systemctl enable --now docker

cat > /etc/cron.daily/docker-prune <<'EOF'
#!/bin/sh
docker system prune -af --filter "until=168h"
EOF
chmod +x /etc/cron.daily/docker-prune
