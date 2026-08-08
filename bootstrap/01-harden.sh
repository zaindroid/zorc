#!/bin/bash
set -euo pipefail
apt update && apt upgrade -y
apt install -y unattended-upgrades fail2ban ufw curl git ca-certificates jq

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

systemctl enable --now fail2ban
systemctl is-active fail2ban

ufw default deny incoming
ufw default allow outgoing
ufw allow from 192.168.0.0/24 to any port 22 proto tcp
ufw allow from 100.64.0.0/10 to any port 22 proto tcp
ufw --force enable
ufw status verbose
