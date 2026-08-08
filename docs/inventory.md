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
- Unattended security upgrades: enabled (/etc/apt/apt.conf.d/20auto-upgrades).
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
