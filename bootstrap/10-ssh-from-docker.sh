#!/bin/bash
set -euo pipefail
# Coolify's "This Machine" deployment model works by SSHing from its own
# container to host.docker.internal:22 (the host's real sshd), even for
# managing the local server. Docker's network pool (10.0.0.0/8, configured
# at Coolify install time) isn't covered by the existing LAN/tailnet SSH
# UFW rules, so this traffic was being dropped ("Operation timed out" in
# Coolify's server connectivity check) until this rule was added. This
# covers all of Coolify's current and future project/environment bridge
# networks, not just the one active at install time.
if ! sudo ufw status | grep -q '10.0.0.0/8'; then
  sudo ufw allow from 10.0.0.0/8 to any port 22 proto tcp
fi
