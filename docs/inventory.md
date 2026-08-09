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

## Storage (updated 2026-08-08 — NVMe added)

Three-tier storage:
- **NVMe hot path**: `/dev/nvme0n1` (Samsung MZVLQ512HBLU-00BH1, 476.9G),
  single ext4 partition labeled `nvmefast`, UUID
  `1e3adfbf-3d2f-413a-9c9d-78e648790492`, mounted at `/mnt/fast`
  (`defaults,noatime,nofail` in fstab). Docker's data-root now lives here
  (`/etc/docker/daemon.json` → `"data-root": "/mnt/fast/docker"`, merged
  into the existing log-driver/log-opts/default-address-pools config, not
  replaced).
- **SATA SSD**: `/dev/sda` (MTFDDAK512MBF), OS root (`/`).
- **SATA HDD**: `/dev/sdb1` (`serverdata`), bulk storage at `/mnt/data`.
  Also now holds `/mnt/data/nvme-rescue/` — a full rsync copy (28G, 195,675
  files, verified matching) of what was on the NVMe before it was wiped
  (reused drive, had unrelated robotics/AI project data — confirmed not
  needed, kept as a safety-net copy anyway).
- **Old Docker data** retained at `/var/lib/docker.old` on the SSD, NOT yet
  deleted — pending explicit go-ahead once the new data-root is confirmed
  stable over time.
- All 6 Coolify containers (`coolify`, `coolify-db`, `coolify-redis`,
  `coolify-realtime`, `coolify-proxy`, `coolify-sentinel`) verified healthy
  on the new data-root; dashboard reachable through Access as before.

## Notes on today's boot-fix bugs (both found and fixed same session)

- `fix-docker-ufw-bypass.sh` was found completely empty (0 bytes) on this
  boot, cause unknown — its systemd service failed with "Exec format error,"
  meaning the port-8000 DOCKER-USER block did NOT reapply automatically.
  Rewritten with known-good content, verified working again.
- `fix-efi-bootorder.service` — first real-world reboot test (triggered by
  installing the NVMe) succeeded on its own, no manual intervention needed.
  The earlier same-day rewrite (handling a missing BootOrder gracefully,
  syncing the fallback file to grubx64.efi) held up correctly.

## Host watchdog + status page + node self-registration (2026-08-08)

- **Watchdog**: `~/zorc/monitoring/watchdog.py`, run every ~60s by
  `zorc-watchdog.timer` (systemd, host-level, NOT Docker — survives Docker
  dying). Runs as root (hardened: `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome=read-only`, `PrivateTmp`, `ReadWritePaths` scoped to
  `monitoring/` and `nodes/` only). 20 checks covering CPU/GPU temp, SMART
  (sda/sdb/nvme0n1), disk usage, RAM/swap, load average, AC/battery power,
  Docker daemon, all 6 Coolify containers, Postgres readiness, tailscaled,
  cloudflared tunnel health, failed systemd units, expected mounts, and a
  live check that the DOCKER-USER port-8000 block is actually in effect (not
  just that a fix script ran — closes the exact gap from the empty-script
  bug found earlier the same day).
- **Alerting**: state-change only (not every cycle), via ntfy.sh. Topic
  lives in `~/zorc/monitoring/secrets/notify.json` (gitignored, NEVER
  committed — the topic's only protection is being unguessable). Sustained
  thresholds (CPU >90C, load avg > cores) require N consecutive cycles
  before escalating, tracked in `state/state.json`. Daily all-green
  heartbeat separate from the healthchecks.io ping. healthchecks.io ping URL
  not yet set — `ping_healthchecks()` is a documented no-op until added to
  secrets/notify.json.
- **Status page**: `~/zorc/monitoring/status/{index.html,status.json}`,
  rewritten every cycle, served by `zorc-status-server.service`
  (`python -m http.server`, bound to `127.0.0.1:9999` only — confirmed not
  reachable from LAN). Reached publicly via `status.zaindroid.me` through
  the existing tunnel, gated by Cloudflare Access (allow-list
  zainey4@gmail.com — same pattern as Coolify's app). **Note**: Access
  302-redirects unauthenticated requests to its login page, so this page
  cannot currently be polled programmatically by scripts — when a future
  multi-node swarm needs to read status.json automatically, the path is a
  Cloudflare Access **service token** (header-based credential) on that
  route, not assuming interactive-session access works for automation.
- **Node self-registration**: `~/zorc/nodes/servingz.yaml`, rewritten every
  cycle and periodically committed — a capability-schema contract
  (node/tailscale_ip/arch/accelerator/cpu/ram_mb/power/capabilities/labels/
  status/last_seen) designed to be the same join-script shape future
  Jetson/Pi/5090 boards will self-report against.
- **Python tooling**: managed via `uv` (`pyproject.toml` + `uv.lock`,
  committed; `.venv/` gitignored). Only dependency: `pyyaml` (everything
  else stdlib). systemd units point at the venv's python by absolute path
  (`/home/zman/zorc/monitoring/.venv/bin/python`), not bare `python3`/`uv
  run` — deterministic regardless of root's PATH.
- Alert flow tested end-to-end: manually tripped `cpu_temp` warning,
  confirmed real ntfy delivery (screenshot confirmed by user), restored
  threshold, confirmed RECOVERED notice.

## Side-fix found while building the watchdog: containerd data-root gap

The earlier NVMe/Docker migration moved `/var/lib/docker` but missed
containerd's separate data directory — container image/layer data (2.8G)
was still on the SATA SSD at `/var/lib/containerd`, not the NVMe, because
`/etc/docker/daemon.json`'s `data-root` does not control containerd (it has
its own `root` setting in `/etc/containerd/config.toml`, which defaults to
`/var/lib/containerd` when commented out). Fixed the same day: stopped
docker+containerd, rsynced to `/mnt/fast/containerd`, set
`root = "/mnt/fast/containerd"` in `/etc/containerd/config.toml`, restarted.
Verified via `mount | grep overlay` that container layer lowerdir/upperdir
paths now resolve under `/mnt/fast/containerd`. Old data retained at
`/var/lib/containerd.old` (same retention pattern as `/var/lib/docker.old`)
— not yet deleted, pending explicit go-ahead.

## First app deployed through the platform contract: hello-app (2026-08-09)

- **Repo**: `github.com/zaindroid/hello-app` (private), FastAPI, deployed via
  Coolify's "Private Repository (GitHub App)" resource, Dockerfile build pack.
- **Domain**: `https://hello.zaindroid.me`, public (no Cloudflare Access —
  intentional, unlike Coolify/status which are platform-internal admin tools).
- **Registered**: `zorc/registry.yaml` apps, 256 MB, budget check passing.
- **This bypassed the intended CI→GHCR→Coolify pipeline** (not wired up
  yet — `ci.yml` template exists but no GitHub Actions run for this app).
  Coolify built directly from the git repo on deploy instead. Wiring up
  proper CI is the next step before a second app.

### Real problems hit and fixed getting this working (all now fixed/documented)

1. **Coolify's "Docker Image" resource always attempts a registry pull**,
   even for a locally-built image with a matching tag — no local-image
   fallback. Building on the host and deploying by image tag alone does not
   work; the image must be pullable from somewhere.
2. **Coolify has a known, unresolved bug pulling from GHCR specifically**
   (upstream issue [coollabsio/coolify#4604](https://github.com/coollabsio/coolify/issues/4604))
   — `docker login ghcr.io` succeeds and credentials are correctly stored,
   but Coolify's pull still fails. Docker Hub works fine with the identical
   process. Root cause undetermined upstream; we worked around it entirely
   by switching to a git-based (Dockerfile) deploy instead of a registry pull.
3. **Coolify's own Instance Domain was never configured** (`Settings →
   General → URL`), defaulting to the raw public IP
   (`http://212.201.69.230:8000`) for all OAuth callbacks/redirects — broke
   the GitHub App creation flow (browser couldn't reach that address; it's
   not port-forwarded and we explicitly firewall it). Fixed by setting it to
   `https://coolify.zaindroid.me`.
4. **Major bug in our own earlier port-80/443/8000 DOCKER-USER fix**: the
   original rule matched on `--ctorigdstport` alone with no direction
   check, which blocked not just inbound LAN/tailnet traffic to those ports
   but **all outbound HTTPS from every container on the host** (a
   connection's "original destination port" is the same 443 whether it's
   inbound-to-us or outbound-from-us). This silently broke Coolify's own
   GitHub API calls. Fixed properly with two refinements: (a) `-i enp0s25`
   / `-i tailscale0` to only match traffic arriving via the actual external
   interfaces, not the docker bridge interface outbound traffic uses; (b)
   `--ctdir ORIGINAL` — interface restriction alone still wasn't enough,
   since a container-initiated outbound connection's *reply* traffic also
   legitimately arrives via those same external interfaces; `ctdir ORIGINAL`
   ensures only the connection-initiating direction (a real inbound attempt)
   is matched, never the reply leg of our own outbound connections.
   `bootstrap/09b-fix-docker-ufw-bypass.sh` updated with both fixes and the
   full explanation.
5. **Coolify's Domains field needs a full `https://` URL**, not a bare
   hostname — entering just `hello.zaindroid.me` produced a broken Traefik
   rule (`Host('') && PathPrefix('hello.zaindroid.me')` — the hostname got
   parsed as a path). Entering `https://hello.zaindroid.me` produced the
   correct `Host('hello.zaindroid.me')` rule.
6. **Traefik's `redirect-to-https` middleware causes an infinite redirect
   loop** when the Cloudflare Tunnel connects to it over plain HTTP
   (`http://localhost:80`) — Cloudflare's edge already terminated TLS, so
   Traefik seeing a "plain HTTP" request keeps redirecting to the same
   HTTPS URL forever. Fixed by pointing the tunnel ingress at Traefik's
   HTTPS entrypoint instead: `service: https://localhost:443` with
   `originRequest.noTLSVerify: true` (safe — this is a localhost-only hop,
   real TLS is already terminated at Cloudflare's edge for the actual
   public connection).
7. **Coolify doesn't pass custom Dockerfile build ARGs** (our `GIT_SHA`/
   `BUILD_TIME`) when building from a git source — those stayed `unknown`.
   Coolify does inject its own `SOURCE_COMMIT` env var at container runtime
   though; `main.py`'s `/version` endpoint now falls back to that when
   `GIT_SHA` isn't set. `BUILD_TIME` has no Coolify equivalent and stays
   `unknown` for git-based deploys — acceptable known gap until proper CI
   passes real build args.

### Verified working end-to-end
- `/health`, `/version` (shows real short SHA), `/` all return correctly
  from outside via `https://hello.zaindroid.me`.
- Watchdog status page confirms `overall: ok`, all 6 original Coolify
  containers still healthy — hello-app's deployment didn't degrade anything.

## Shared Postgres + Redis provisioned (2026-08-09)

- **Postgres 18** (`postgresql-database-t5amapezxhesfta6w82ksyt0`, container
  `t5amapezxhesfta6w82ksyt0`), deployed via Coolify as a Database resource in
  a new `platform` project (separate from `labs`, which houses actual apps).
  Version bumped from the template's example "17" to "18" — no reason to
  pin to 17, nothing existing to stay compatible with. Reachable at
  `t5amapezxhesfta6w82ksyt0:5432` on the `coolify` Docker network only — not
  published to any host port, not tunnel/Access-exposed (nothing external
  should reach a database directly). Verified with `pg_isready` from a
  separate container on the same network.
- **Redis 7.2** (container `h8nk9npsxzv9kkklvgqb93zj`), same project/pattern.
  Reachable at `h8nk9npsxzv9kkklvgqb93zj:6379`, same network, not published.
  Verified reachable (responds with a real Redis protocol auth challenge).
- Neither is wired to any app yet — hello-app explicitly has no
  postgres/redis dependency (see its `app.yaml`). This is infra provisioned
  ahead of the next app that needs it, per `AGENTS.md` §2's now-updated
  "declared and live" status (was "not yet provisioned" before today).
- Per-app database/role (Postgres) and DB-index (Redis) assignment happens
  on request when an app actually needs one — not self-service, matches
  AGENTS.md's "ask before assuming" philosophy.

## CI/CD pipeline + human approval gate for hello-app (2026-08-09)

Rewrote hello-app's `ci.yml` from the Node/npm-shaped template to Python/uv
tooling, keeping the exact same gate structure: `static -> unit ->
integration -> container -> deploy-staging -> e2e -> regression ->
approve-production -> deploy-production`. Registry target switched from
`ghcr.io` to Docker Hub (`docker.io/zainey4/hello-app`) — GHCR already known
broken with Coolify (see hello-app section above).

GitHub's native "Required reviewers" Environment protection rule needs
GitHub Pro/Team for a **private** repo — not available on the free plan
this account is on. Standard free-plan workaround adopted:
[`trstringer/manual-approval@v1`](https://github.com/trstringer/manual-approval)
— opens a GitHub Issue on push-to-main after `regression` passes, blocks the
workflow until someone comments `approved` or `denied` on it.

### Bugs hit and fixed
1. **`gitleaks-action@v2` failed with `fatal: ambiguous argument ...
   unknown revision`** on the very first real CI run — not an actual
   detected secret. `actions/checkout@v4` defaults to a shallow clone
   (`fetch-depth: 1`), so gitleaks couldn't resolve the git history needed
   to diff the push's before/after commit range. Fixed by adding
   `fetch-depth: 0` to the checkout step in the `static` job specifically.
2. **`approve-production` job failed to create its issue**:
   `403 Resource not accessible by integration`. The default `GITHUB_TOKEN`
   is read-only repo-wide unless a job explicitly requests more — no
   `permissions:` block existed anywhere in the workflow. Fixed by adding
   `permissions: issues: write` scoped to just that one job.
3. **`manual-approval@v1` posts its custom `issue-body` input as the
   *first comment* on the issue, not as the issue body itself** — the issue
   body is always the action's own fixed "pending manual review" template
   (`>[!NOTE] Workflow is pending manual review...`). Discovered while
   building the approvals dashboard below: its parser was reading
   `issue.body` and getting nothing back. Fixed by having the dashboard
   fetch `issue.comments[0]` instead.

### Approvals dashboard (same day — see feedback below)
User feedback, twice, in order:
- *"this is not efficient... i would want this review and approval on some
  of our own app dashboard or something not on github commenting"* — led to
  building a real UI instead of relying on GitHub Issue comments.
- *"it would be better if i see the app... how the app works on user end...
  it should have been intuitive review"* — the first version linked out to
  a GitHub compare diff as the primary review artifact; this was correctly
  called out as developer-centric, not an intuitive review for how the app
  actually behaves.

What got built, in `monitoring/server.py`:
- The previously static `python -m http.server` status-page service was
  replaced with a small FastAPI/uvicorn app — same port (9999, localhost
  only), same Cloudflare Access policy already covering the whole
  `status.zaindroid.me` hostname (no new Access app needed). `watchdog.py`'s
  existing static-file generation (`status/index.html`, `status/status.json`)
  is untouched; the new app just serves those files at `/` alongside new
  dynamic routes.
- `GET /approvals` — dashboard page. Polls `GET /api/approvals` every 10s.
  Each pending approval renders as a card with a **live iframe embed of the
  actual staging URL** as the primary review surface (labelled "this is the
  exact build you're approving"), with the code diff, CI run, and raw
  GitHub issue demoted to small secondary links underneath. Approve/Deny
  buttons POST to `/api/approvals/decide`, which posts the literal
  `approved`/`denied` comment to the GitHub issue server-side — the
  browser never touches a GitHub token.
- `apps_with_repos()` reads `registry.yaml`'s `apps[].repo` field directly,
  so any future app that follows the same `ci.yml` pattern shows up on this
  dashboard automatically — no per-app dashboard changes needed.
- Decision endpoint validates the target repo against the known app list
  before posting anything — the dashboard can't be used to comment on an
  arbitrary GitHub issue outside the platform's own apps.
- Auth token: a fine-grained GitHub PAT, resource owner `zaindroid`,
  **All repositories**, **Issues: Read and write** only — nothing else.
  Stored at `monitoring/secrets/github_approvals.json`, `chmod 600`, read
  lazily per-request (never held in memory beyond that). First two tokens
  the user generated had the wrong permission set (Contents instead of
  Issues) — same class of mistake as the earlier `ZORC_REPO_TOKEN` gotcha;
  fine-grained PAT permission pickers are easy to get wrong.
- `ci.yml`'s `issue-body` is now a structured `Field: value` block (`App:`,
  `Commit:`, `Staging:`, `Compare:`, `Run:`) specifically so the dashboard
  can parse it reliably — documented inline in `ci.yml` not to reformat
  without updating `server.py`'s `FIELD_RE` too.
- `deploy-production` now force-moves a `production` git tag to the
  deployed SHA on every successful deploy (needs `permissions: contents:
  write` on that job, plus an `actions/checkout@v4` step it didn't
  previously have). This makes each approval card's `Compare` link a real
  diff against what's actually live, not a diff against the whole repo
  history. Retroactively tagged the already-live commit (`e6bd077`) once by
  hand so the very first dashboard-generated compare link would be
  meaningful.

### Known gaps, not addressed
- Assumes at most one open approval issue per app repo at a time — true for
  this single-developer, single-branch workflow today; would need
  reconsidering if concurrent PRs/approvals per app become a thing.
- The Coolify API token used by `ci.yml`'s webhooks is still `root`-scoped,
  not the properly-scoped `deploy`-only token — swap deferred, not
  forgotten (see hello-app section above).

### Verified working end-to-end
Full pipeline run on commit `e4fb1d0` went through all 9 jobs. The
approval card on `status.zaindroid.me/approvals` correctly showed the live
staging preview, compare, and CI run links; clicking Approve posted the
comment via the API; `deploy-production` picked it up within ~10s,
force-moved the `production` tag, and production served the new SHA
(`https://hello.zaindroid.me/version` -> `e4fb1d0`) within the smoke-test
window.

### Approval notifications (same day, follow-up)
Pending approvals were initially only visible by opening the dashboard.
Added a step to `approve-production` (before the blocking manual-approval
step) that posts to the same ntfy.sh topic already used for host health
alerts (`NTFY_TOPIC` secret, reused from `monitoring/secrets/notify.json`)
— pending approvals now show up in the same phone notification stream as
disk/RAM/temperature alerts, rather than relying on GitHub's separate
issue-assignment notifications.

## Backups (2026-08-09)

Platform-level disaster recovery — not an app-facing capability, nothing in
`AGENTS.md`'s capability table changed. Daily via `zorc-backup.timer`
(03:00 UTC + up to 10min random delay), `backup/backup.sh`.

**What's backed up:**
- Shared app Postgres (`t5amapezxhesfta6w82ksyt0`) — full `pg_dumpall`.
- Coolify's own internal Postgres (`coolify-db`) — full `pg_dumpall`.
  User is `coolify`, not `postgres` (Coolify's own `POSTGRES_USER` — cost
  us the first test run with a "role does not exist" error).
- `/data/coolify` in full (tar) — SSH keys, SSL certs, and critically
  `source/.env`'s `APP_KEY`: the encryption key for every secret Coolify
  itself stores (env vars, source connections, DB passwords it manages).
  Without this key a restored `coolify-db` is undecryptable junk, so it has
  to travel with every backup, not be treated as a one-time secret.
- **Redis is deliberately excluded** — `AGENTS.md` §2 already documents it
  as cache/queue only, nothing irreplaceable is supposed to live there.

**Pipeline:** dump -> tar -> `gpg --symmetric --cipher-algo AES256`
(encrypted locally before it ever leaves the host) -> upload to Cloudflare
R2 via `rclone`. Local copies (`/mnt/data/backups/`) kept 7 days; remote
copies in R2 kept 30 days, pruned by the script itself each run.

**R2 setup gotchas:**
1. R2 doesn't show a "create bucket" option until you complete a one-time
   (free) subscription checkout under Storage & databases -> R2 -> Overview
   — not documented clearly in the dashboard flow itself.
2. The R2 API token is scoped to **Object Read & Write only** on the one
   bucket (least privilege — it never needs to create/delete buckets or
   read other buckets). This broke `rclone` by default: rclone's S3 backend
   pre-flights every write with a `CreateBucket` call, which 403'd against
   our deliberately-narrow token even though the bucket already existed.
   Fixed with `no_check_bucket = true` in `rclone.conf`.

**Restore tested for real**, not assumed: downloaded the actual uploaded
object back from R2, decrypted it, and restored both dumps into fresh
throwaway `postgres:18-alpine` / `postgres:15-alpine` containers (matching
the real versions) — confirmed the `coolify` database and role came back
correctly before deleting the test containers.

**Known gap, flagged not fixed:** the GPG passphrase and R2 credentials
that make backups decryptable currently exist ONLY as files on servingz
itself (`backup/secrets/`, gitignored) — if the host's disk dies, the
encrypted backups in R2 are unrecoverable without them. User was told to
copy the passphrase into a password manager they control; not yet
confirmed done. This is also why `backup/secrets/` is `chmod 600` and
excluded via the existing repo-wide `secrets/` gitignore pattern, same as
`monitoring/secrets/`.

A dedicated healthchecks.io dead-man's-switch check (`servingz-backup`,
~26h period) now pings on every successful run, separate from the host
watchdog's own check — a silently-failing backup is now distinguishable
from a healthy host.
