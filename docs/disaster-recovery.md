# Disaster recovery — rebuilding servingz from nothing

This is the runbook for "the host is gone" — dead disk, dead hardware,
anything short of the physical machine being unrecoverable. It assumes you
have: a fresh Ubuntu 24.04 LTS install (same or replacement hardware),
console/physical access, this repo's GitHub remote, and your Bitwarden
vault (for the two secrets nothing else can recover — see bottom of
`docs/inventory.md`'s Backups section).

Written from a full audit of `bootstrap/*.sh` against what they actually
automate vs. what was historically done by hand — gaps found during that
audit (tunnel credentials, tunnel config, containerd/Docker data-root) were
fixed as part of writing this, not just documented around. Not yet tested
against a real from-scratch rebuild — see "What this hasn't proven" at the
bottom.

Steps marked **[MANUAL]** need a human judgment call or interactive input
and cannot be scripted as-is. Everything else is `bootstrap/NN-*.sh`,
run in order, idempotent (safe to re-run if a step fails partway).

---

## Phase 0 — before you start

- [ ] Fresh Ubuntu 24.04 LTS, SSH reachable, your public key already in
      `~/.ssh/authorized_keys` (or console access to add it)
- [ ] Open Bitwarden, have ready: the GPG backup passphrase, the R2
      credentials (`access_key_id`, `secret_access_key`, `account_id`,
      `endpoint`, `bucket`)
- [ ] **[MANUAL]** If this is different physical hardware: note the new
      NVMe/bulk-disk device paths and partition UUIDs (`lsblk -f`) — every
      UUID referenced below is specific to the *old* hardware and will not
      match.

## Phase 1 — base OS + access

```bash
git clone https://github.com/zaindroid/zorc.git ~/zorc
cd ~/zorc/bootstrap
sudo ./01-harden.sh      # UFW, fail2ban, unattended-upgrades
sudo ./02-docker.sh      # Docker CE + containerd
sudo ./03-ssh-lockdown.sh   # key-only auth
```

- [ ] **[MANUAL]** Verify you can still SSH in with your key in a *second*
      terminal before closing the first — `03-ssh-lockdown.sh` disables
      password auth immediately.

## Phase 2 — disk layout

- [ ] **[MANUAL]** Partition/mount the fast (NVMe) and bulk disks. On the
      original hardware these are `/mnt/fast` (NVMe, Docker/containerd
      data-root) and `/mnt/data` (bulk HDD, backup staging + general bulk
      storage). Add both to `/etc/fstab` by UUID, `mount -a`.

## Phase 3 — Docker + containerd data-root

Both of these were one-time manual edits historically (see
`docs/inventory.md`, "Side-fix found while building the watchdog:
containerd data-root gap") — never scripted. Doing them explicitly here
instead of pointing at prose:

```bash
sudo systemctl stop docker containerd
```

`/etc/docker/daemon.json`:
```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "default-address-pools": [{ "base": "10.0.0.0/8", "size": 24 }],
  "data-root": "/mnt/fast/docker"
}
```

`/etc/containerd/config.toml` — set (uncomment if needed):
```toml
root = "/mnt/fast/containerd"
```

```bash
sudo systemctl start containerd docker
docker info | grep 'Docker Root Dir'   # confirm /mnt/fast/docker
```

## Phase 4 — GPU driver (only if this hardware has the NVIDIA GPU)

```bash
sudo ./04-nvidia.sh
```
- [ ] **[MANUAL]** If this hits the same Kepler-GPU/driver-470 packaging
      issue on Ubuntu 24.04 documented in `docs/inventory.md`, use
      NVIDIA's official `.run` installer per that doc, not apt.

## Phase 5 — EFI boot-order (only if this is the same HP ZBook)

```bash
sudo ./05-fix-efi-bootorder.sh
sudo cp bootstrap/fix-efi-bootorder.service /etc/systemd/system/
sudo systemctl enable --now fix-efi-bootorder.service
```
Only relevant if you hit the firmware bug where the boot entry silently
drops from `BootOrder` on reboot — replacement hardware may not need this
at all.

## Phase 6 — Tailscale

```bash
sudo ./06-tailscale.sh
sudo tailscale up
```
- [ ] **[MANUAL]** `tailscale up` requires interactive browser auth against
      your Tailscale account — cannot be scripted.

## Phase 7 — cloudflared (install only, not configured yet)

```bash
sudo ./07-cloudflared.sh
```

## Phase 8 — Coolify

```bash
sudo ./08-coolify.sh
```
Leave the Coolify containers running but treat the instance as **empty**
until Phase 10 restores its real state — do not create anything in the UI
yet.

## Phase 9 — firewall fixes

```bash
sudo cp fix-docker-ufw-bypass.service /etc/systemd/system/
sudo ./09b-fix-docker-ufw-bypass.sh
sudo systemctl enable --now fix-docker-ufw-bypass.service
./10-ssh-from-docker.sh
```

## Phase 10 — restore real state from backup

Install the restore tools:
```bash
curl -s https://rclone.org/install.sh | sudo bash
```

Configure rclone with the R2 credentials from Bitwarden:
```bash
mkdir -p ~/restore && cd ~/restore
cat > rclone.conf << EOF
[r2]
type = s3
provider = Cloudflare
access_key_id = <from Bitwarden>
secret_access_key = <from Bitwarden>
endpoint = <from Bitwarden>
acl = private
no_check_bucket = true
EOF
```

Pull the most recent backup and decrypt it (passphrase from Bitwarden):
```bash
rclone --config rclone.conf lsf r2:servingz-backups/servingz/ | sort | tail -1
# copy the newest filename, then:
rclone --config rclone.conf copy r2:servingz-backups/servingz/<newest-file> .
gpg --decrypt -o backup.tar.gz <newest-file>   # prompts for passphrase
tar xzf backup.tar.gz
```

**Order matters from here — Coolify's own state first, then it recreates
the app postgres container, then that gets its data restored:**

1. Stop Coolify entirely:
   ```bash
   docker stop coolify coolify-db coolify-redis coolify-proxy coolify-realtime coolify-sentinel
   ```
2. Restore Coolify's config dir (must happen before Coolify starts again —
   this includes `APP_KEY`, without which the restored `coolify-db` is
   undecryptable):
   ```bash
   tar xzf coolify_data.tar.gz -C /tmp/coolify-restore
   sudo rsync -a /tmp/coolify-restore/coolify/ /data/coolify/
   ```
3. Start `coolify-db` only, restore into it:
   ```bash
   docker start coolify-db && sleep 5
   zcat coolify_postgres.sql.gz | docker exec -i coolify-db psql -U coolify
   ```
4. Start the rest of Coolify:
   ```bash
   docker start coolify coolify-redis coolify-proxy coolify-realtime coolify-sentinel
   ```
   Coolify should come up with all its previously-known resources — apps,
   the shared Postgres/Redis, source connections — because that state was
   in the dump you just restored. Give it a minute to recreate containers
   for resources it manages.
5. Once the shared app Postgres container is back up (check
   `docker ps | grep t5amapezxhesfta6w82ksyt0` — **the container ID/name
   is stable across Coolify redeploys of the same resource**, but confirm
   it in the Coolify UI if it's changed), restore into it:
   ```bash
   zcat app_postgres.sql.gz | docker exec -i <container-name> psql -U postgres
   ```
6. Restore the Cloudflare Tunnel:
   ```bash
   tar xzf cloudflared.tar.gz -C ~/.cloudflared/
   sudo cp ~/zorc/cloudflared/config.yml /etc/cloudflared/config.yml
   sudo cloudflared service install
   sudo systemctl enable --now cloudflared
   ```
7. Re-provision the backup system itself on the new host (secrets don't
   travel via git — pull them back out of Bitwarden):
   ```bash
   mkdir -p ~/zorc/backup/secrets
   # recreate r2.json, rclone.conf, gpg_passphrase, healthchecks.json from Bitwarden
   sudo cp ~/zorc/backup/systemd/*.service ~/zorc/backup/systemd/*.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now zorc-backup.timer
   ```

## Phase 11 — verify

- [ ] `https://status.zaindroid.me` loads, shows all-green (or explains
      what's still degraded)
- [ ] `https://coolify.zaindroid.me` loads, dashboard shows the expected
      resources
- [ ] `https://hello.zaindroid.me/version` returns the expected SHA
- [ ] `docker exec <app-postgres-container> psql -U postgres -c '\l'` shows
      the expected databases (not just the defaults)
- [ ] Manually run `bash ~/zorc/backup/backup.sh` once, confirm it
      completes and the healthchecks.io ping lands — proves the *new* host
      can protect itself going forward, not just that it was restored once

---

## What this hasn't proven

This runbook was assembled from a careful audit of every bootstrap script
plus `docs/inventory.md`'s prose, cross-checked against the live host —
not from an actual from-scratch rebuild. The individual restore step (data
in, data out, verified correct) **was** tested for real against R2. The
*full sequence* end-to-end, on genuinely fresh hardware, has not been. If
you ever have a spare window (a spare drive, a VM, anything disposable),
running this top-to-bottom for real is the only way to find out what's
still missing.
