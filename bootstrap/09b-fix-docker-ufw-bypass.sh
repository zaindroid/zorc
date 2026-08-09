#!/bin/bash
# Docker inserts DOCKER-USER/DOCKER-FORWARD rules ahead of UFW's chains in
# FORWARD, so UFW's "deny incoming" does not protect published container
# ports (docker-proxy binds 0.0.0.0 regardless of UFW state). This blocks
# ports that must stay localhost-only (reachable via the Cloudflare tunnel,
# not the LAN/tailnet) at the DOCKER-USER chain, which Docker never
# touches, so it survives Docker restarts. Matches the pre-DNAT original
# destination port via conntrack since Docker's port-mapping DNAT rewrites
# the packet's dest port before DOCKER-USER ever sees it.
#
# IMPORTANT — two bugs found and fixed 2026-08-09, both from the same root
# cause (matching on ctorigdstport alone doesn't encode direction):
#   1. A bare "-m conntrack --ctorigdstport PORT -j DROP" with no interface
#      restriction blocks ALL traffic touching that port regardless of
#      direction -- including every container's own OUTBOUND connections to
#      external services on port 443 (e.g. Coolify's GitHub App / API calls
#      all timed out). Fixed by restricting to -i enp0s25 / -i tailscale0
#      (the actual inbound-facing interfaces), so outbound traffic --
#      which arrives at DOCKER-USER via the docker bridge interface, not
#      enp0s25/tailscale0 -- is never matched.
#   2. Interface restriction ALONE still isn't enough: the RETURN traffic of
#      a container-initiated outbound connection legitimately arrives back
#      via enp0s25/tailscale0 too (that's just how responses come back from
#      the internet), and was still being matched/dropped. Fixed by adding
#      "--ctdir ORIGINAL", so only the connection-initiating direction is
#      matched -- an inbound SYN from LAN/tailnet trying to reach our
#      published port -- never the reply leg of a connection our own
#      container initiated outbound.
#
# Ports covered:
#   8000   Coolify dashboard (admin, gated by Cloudflare Access)
#   80/443 Traefik/coolify-proxy (routes all Coolify-deployed apps)
#
# NOTE: iptables-persistent was tried first for persistence and reverted --
# it conflicts with the ufw package (both want to own iptables persistence
# at boot; installing it silently removed ufw). This runs at boot instead,
# via fix-docker-ufw-bypass.service, same pattern as fix-efi-bootorder.service.
set -euo pipefail

add_rule() {
  local iface="$1" port="$2" proto="$3"
  if ! iptables -C DOCKER-USER -i "$iface" -m conntrack --ctorigdstport "$port" --ctdir ORIGINAL -p "$proto" -j DROP 2>/dev/null; then
    iptables -I DOCKER-USER -i "$iface" -m conntrack --ctorigdstport "$port" --ctdir ORIGINAL -p "$proto" -j DROP
  fi
}

for iface in enp0s25 tailscale0; do
  add_rule "$iface" 8000 tcp
  add_rule "$iface" 80 tcp
  add_rule "$iface" 443 tcp
  add_rule "$iface" 443 udp
done
