# Inventory

## Hosts

| Name     | Hardware              | RAM  | IP             | MAC               | Role         |
|----------|------------------------|------|----------------|--------------------|--------------|
| servingz | HP ZBook i7-4810MQ    | 32GB | 192.168.0.107  | fc:3f:db:5c:01:1e | orchestrator |

## Databases

| Name | Engine | Host | Port | Notes |
|------|--------|------|------|-------|
|      |        |      |      |       |

## Domains

| Domain | Points to | Notes |
|--------|-----------|-------|
|        |           |       |

## Storage

- HDD `/dev/sdb1`, label `serverdata`, UUID `6916f52c-2986-46ae-8818-9ad2aa89de5f`,
  mounted at `/mnt/data` (ext4, `defaults,noatime,nofail` in `/etc/fstab`).
  Contains `waseem_backup`.

## Notes

- Secrets live in a password manager — never in this repo.

## Software / Host state

- OS: Ubuntu 24.04.4 LTS, kernel 6.8.0-137-generic
- Docker: 29.7.2 (docker-ce, official repo), Compose plugin v5.4.0. Data root on
  SSD (default), daily prune via /etc/cron.daily/docker-prune (keeps <168h old).
- Firewall: UFW active, default deny incoming / allow outgoing. Port 22 allowed
  only from 192.168.0.0/24 (LAN) and 100.64.0.0/10 (Tailscale CGNAT, reserved
  for future use — Tailscale itself not installed yet).
- fail2ban: active, default sshd jail.
- Unattended security upgrades: enabled (/etc/apt/apt.conf.d/20auto-upgrades). Automatic-Reboot explicitly set to false in /etc/apt/apt.conf.d/50unattended-upgrades (was commented/default, made explicit given the EFI boot-order fragility).
- SSH: password authentication disabled (key-only), see
  /etc/ssh/sshd_config.d/99-hardening.conf. PermitRootLogin prohibit-password.
- sudo: /etc/sudoers.d/90-zman-bootstrap grants zman passwordless sudo,
  added temporarily to unblock scripted bootstrap phases (no TTY available for
  interactive sudo from automation) — pending a decision on whether to revert.
- GPU / NVIDIA driver: INSTALLED. Quadro K2100M (Kepler/GK106), driver
  470.256.02 (NVIDIA's official .run installer with --dkms, since Ubuntu
  24.04's packaged nvidia-driver-470 no longer installs cleanly — see
  bootstrap/04-nvidia.sh for that investigation). nouveau blacklisted via
  /etc/modprobe.d/blacklist-nouveau.conf. CUDA 11.4. Verified working live via
  nvidia-smi; DKMS-registered so it rebuilds automatically on future kernel
  updates. Not yet reboot-tested (works live, reboot not required — deferred
  to a time someone is watching, see runbook.md for the EFI boot-order caveat
  discovered along the way).
- EFI boot-order quirk (HP ZBook 15 G2 firmware): discovered during this work
  — see docs/runbook.md "Lost SSH access / server won't boot" section and
  bootstrap/05-fix-efi-bootorder.sh for the self-healing fix now installed
  (fix-efi-bootorder.service, runs every boot).

## Networking

- Tailscale: installed, tailnet IP 100.115.156.84, tailscaled enabled on boot.
  No UFW changes needed (existing 100.64.0.0/10 SSH allow rule already covers it).
- Cloudflare Tunnel: tunnel name "servingz", UUID a8ceda0a-a10a-4924-8095-fb443319382d,
  zone zaindroid.me. Config at /etc/cloudflared/config.yml (installed copy) and
  ~/.cloudflared/config.yml (source), credentials at
  ~/.cloudflared/a8ceda0a-a10a-4924-8095-fb443319382d.json. cloudflared systemd
  service enabled on boot. Subdomain scheme: *.apps.zaindroid.me (no wildcard
  DNS record yet — only status.apps.zaindroid.me routed as a placeholder,
  service http://localhost:9999, nothing listening there yet). Real app
  hostnames to be added when Coolify + watchdog exist.

## Coolify

- Installed via official installer (v4.1.2). Containers: `coolify` (app),
  `coolify-db` (postgres:15-alpine), `coolify-redis` (redis:7-alpine),
  `coolify-realtime` (soketi/websocket). No separate Traefik/proxy container
  at install time — Coolify only deploys that when a server/app is registered.
- Dashboard: `https://coolify.zaindroid.me`, gated by Cloudflare Access
  (allow-list: zainey4@gmail.com, one-time PIN auth, 24h session).
  Port 8000 is NOT exposed via UFW allow rule — reachable only via localhost
  (for the tunnel) and blocked from LAN/tailnet by a DOCKER-USER iptables
  rule (see below).
- Auto-update: OFF. "This Machine" (localhost) registered as the deployment
  server, connected successfully.
- Data root: `/data/coolify/`. Env file: `/data/coolify/source/.env`
  (back this up externally — installer explicitly warns about this).

## Known Docker + UFW/firewall interactions (found + fixed 2026-08-08)

1. **Docker bypasses UFW for published container ports.** Docker inserts
   DOCKER-USER/DOCKER-FORWARD rules ahead of UFW's chains in FORWARD, so
   `ufw status` showing no rule for a port does NOT mean it's actually
   blocked — Coolify's port 8000 was reachable directly over LAN/tailnet
   despite no UFW allow rule. Fixed with a DOCKER-USER iptables rule
   matching the pre-DNAT original dest port via conntrack (Docker's DNAT
   rewrites the visible dest port before DOCKER-USER sees it — a plain
   `--dport 8000` match silently does nothing). See bootstrap/09-docker-ufw-fix.sh
   and bootstrap/09b-fix-docker-ufw-bypass.sh (systemd boot-time reapply,
   fix-docker-ufw-bypass.service — matches the fix-efi-bootorder.service
   pattern). NOTE: iptables-persistent was tried first for rule persistence
   and reverted — it conflicts with the ufw package itself (both want to
   own iptables persistence at boot; installing it silently removed ufw).
2. **UFW blocks Docker→host SSH by default.** Coolify's "This Machine"
   deployment model SSHes from its own container to `host.docker.internal:22`
   (the real host sshd) even for local-server management. Docker's internal
   network pool (10.0.0.0/8) wasn't covered by the LAN/tailnet-only SSH
   rules, causing "Operation timed out" in Coolify's connectivity check.
   Fixed by allowing SSH from 10.0.0.0/8 (bootstrap/10-ssh-from-docker.sh) —
   covers all current/future Coolify project networks, not just the one
   active at install time.
3. **Coolify needs its own SSH key in root's authorized_keys**, even for
   managing "This Machine" (itself) — it SSHes in as root to run Docker
   commands. Key added manually (not scripted — it's generated internally by
   Coolify and stored in its own data volume, not something to regenerate).

## Cloudflare zone / certificate note

- Free/Universal SSL only covers `zaindroid.me` and `*.zaindroid.me` (one
  wildcard level). A `*.apps.zaindroid.me` scheme (two levels deep) is NOT
  covered and fails TLS handshake at the edge — confirmed via openssl
  s_client showing no certificate offered. Advanced Certificate Manager
  (paid add-on) would be needed to cover a second-level wildcard.
- Decision: dropped the `*.apps.zaindroid.me` scheme from the original plan
  in favor of flat `*.zaindroid.me` hostnames (free, already covered).
  Current: `coolify.zaindroid.me`, `status.zaindroid.me` (placeholder,
  service http://localhost:9999, nothing listening — 502 expected).
