#!/bin/bash
set -euo pipefail
# The filename prefix matters and must stay BELOW 50.
#
# sshd_config.d/*.conf is read in lexical order and OpenSSH takes the FIRST
# occurrence of a keyword, not the last. Ubuntu cloud images ship
# 50-cloud-init.conf, which sets "PasswordAuthentication yes" from the
# provider's provisioning. A drop-in named 99-* therefore loses to it and is
# silently ineffective -- `sshd -T` keeps reporting
# "passwordauthentication yes" while the file on disk plainly says no.
#
# Found live on 2026-11-02: this script had written 99-hardening.conf, so
# password auth was still enabled on contabo AND on servingz despite the
# script having been run. On Ubuntu's own cloud image it also meant
# 60-cloudimg-settings.conf ("PasswordAuthentication no") was being
# overridden by cloud-init too.
#
# Verify with `sshd -T | grep -i passwordauthentication` after reloading --
# never trust the file's own contents.
cat > /etc/ssh/sshd_config.d/01-zorc-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
# Remove the older, ineffective name if a previous run left one behind.
rm -f /etc/ssh/sshd_config.d/99-hardening.conf
sshd -t
systemctl reload ssh
sshd -T | grep -iE "^(passwordauthentication|permitrootlogin|pubkeyauthentication)"
