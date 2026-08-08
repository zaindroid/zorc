#!/bin/bash
set -euo pipefail
# Docker inserts DOCKER-USER/DOCKER-FORWARD rules ahead of UFW's chains in
# FORWARD, so UFW's "deny incoming" does not protect published container
# ports (docker-proxy binds 0.0.0.0 regardless of UFW state). Coolify's
# dashboard on port 8000 was found reachable directly over LAN/tailnet
# despite no UFW rule permitting it.
#
# Fix: a DOCKER-USER iptables rule (Docker never touches this chain) that
# matches the pre-DNAT original destination port via conntrack, since
# Docker's port-mapping DNAT rewrites the packet's dest port before
# DOCKER-USER ever sees it.
#
# NOTE: iptables-persistent was tried first for persistence and reverted --
# it conflicts with the ufw package (both want to own iptables persistence
# at boot; installing it silently removed ufw). Persistence is instead a
# systemd oneshot service that reapplies the rule at boot (same pattern as
# fix-efi-bootorder.service): see 09b-fix-docker-ufw-bypass.sh and
# fix-docker-ufw-bypass.service.
if ! sudo iptables -C DOCKER-USER -m conntrack --ctorigdstport 8000 -p tcp -j DROP 2>/dev/null; then
  sudo iptables -I DOCKER-USER -m conntrack --ctorigdstport 8000 -p tcp -j DROP
fi
