#!/bin/bash
# Docker inserts DOCKER-USER/DOCKER-FORWARD rules ahead of UFW's chains in
# FORWARD, so UFW's "deny incoming" does not protect published container
# ports (docker-proxy binds 0.0.0.0 regardless of UFW state). This blocks
# Coolify's dashboard port (8000) at the DOCKER-USER chain, which Docker
# never touches, so it survives Docker restarts. Matches the pre-DNAT
# original destination port via conntrack since Docker's port-mapping DNAT
# rewrites the packet's dest port before DOCKER-USER ever sees it.
#
# NOTE: iptables-persistent was deliberately NOT used here -- it conflicts
# with the ufw package (both want to own iptables persistence at boot;
# installing it silently removed ufw). This runs at boot instead, via
# fix-docker-ufw-bypass.service, same pattern as fix-efi-bootorder.service.
set -euo pipefail

if ! iptables -C DOCKER-USER -m conntrack --ctorigdstport 8000 -p tcp -j DROP 2>/dev/null; then
  iptables -I DOCKER-USER -m conntrack --ctorigdstport 8000 -p tcp -j DROP
fi
