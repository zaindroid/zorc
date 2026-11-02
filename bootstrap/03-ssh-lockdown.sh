#!/bin/bash
set -euo pipefail
# THE FILENAME PREFIX MATTERS and must stay below 50.
#
# sshd_config.d/*.conf is read in lexical order and OpenSSH honours the FIRST
# occurrence of a keyword, not the last. Ubuntu cloud images ship
# 50-cloud-init.conf, which the provider's provisioning fills with
# "PasswordAuthentication yes". A drop-in named 99-* therefore loses to it and
# is silently ineffective -- `sshd -T` keeps reporting
# "passwordauthentication yes" while the file on disk plainly says no. On
# Ubuntu's own cloud image it also overrides 60-cloudimg-settings.conf, which
# already says no.
#
# Found live 2026-11-02: this script had written 99-hardening.conf, so root
# password auth was still enabled on servingz (the control plane) and on
# hostinger-vps (public IPv4, with failed root attempts already in fail2ban)
# despite the script having been run on both.
#
# The Match block keeps a password route in from the private networks only,
# so losing a key file does not force a fallback to the provider's VNC
# console -- while nothing on the public internet is ever offered a password.
# Joining the tailnet already requires authentication.
#
# ADD THE HOME LAN to the Match line for a node that is reached over it
# (servingz uses "Match Address 100.64.0.0/10,192.168.0.0/24").
cat > /etc/ssh/sshd_config.d/01-zorc-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes

Match Address 100.64.0.0/10
    PasswordAuthentication yes
    KbdInteractiveAuthentication yes
    PermitRootLogin yes
EOF
# Drop the older, ineffective name if an earlier run left one behind.
rm -f /etc/ssh/sshd_config.d/99-hardening.conf
sshd -t
systemctl reload ssh

# Verify, rather than assuming the file took effect. These must differ.
echo "--- from a public source address (expect: no / without-password) ---"
sshd -T -C addr=8.8.8.8,user=root,host=x | grep -iE "^(passwordauthentication|permitrootlogin)"
echo "--- from a tailnet source address (expect: yes / yes) ---"
sshd -T -C addr=100.64.0.1,user=root,host=x | grep -iE "^(passwordauthentication|permitrootlogin)"
