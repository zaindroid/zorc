# Plan: Redis as a first-class resource + Infisical shared secrets platform

Status: architecture only, not yet implemented. Written 2026-10-11 after a
session that shipped `preferred_node` placement, `add_custom_domain`, and the
public-IP node-probing fix (PRs #2/#3/#5) — this is the next piece of work,
picked up fresh so it gets proper budget (a secrets manager is the most
security-sensitive thing on the platform; not worth rushing).

Companion repo for the Infisical image itself:
github.com/zaindroid/infisical-selfhost (just `FROM infisical/infisical:latest`
for now — not deployable yet, see step 2 below).

## 1. Redis as a first-class resource (build this first — Infisical needs it)

Mirrors `database: true` exactly, not the half-built `redis_db` field that
currently exists in registry.yaml's app schema with zero provisioning code
behind it (confirmed: `grep -i redis deploy/agent.py` turns up nothing but
that one dead field). Replace it with one real model instead of two
half-built ones.

- `app.yaml` gains top-level `redis: true` (same shape as `database: true`).
- `deploy()` provisions a DEDICATED Redis container per app (not shared —
  same "no cross-app access" principle `database: true` already follows),
  sets `REDIS_URL` before the container's first real start, generate-and-set
  pattern, caller never sees credentials — same contract as DATABASE_URL.
- New code needed, mirroring the existing Postgres functions exactly:
  - `_provision_redis_coolify()` (Coolify-backend nodes) — likely
    `redis:7-alpine` via Coolify's database-resource API if it supports
    Redis as a first-class type, else a plain `docker run` over SSH same as
    `_provision_postgres_zorc_agent()` does for its backend.
  - `_provision_redis_zorc_agent()` (zorc-agent nodes) — direct `docker run`,
    generated `--requirepass`, matches `_provision_postgres_zorc_agent()`'s
    shape.
  - Wire both into `deploy()`'s existing provisioning sequence, same place
    `database: true` already hooks in.
- `registry.yaml` schema: replace `redis_db: null` with `redis: true/false`
  in every app entry, update the schema comment block, update
  `analyze_deployment_requirements()`'s report to surface `redis_provisioned`
  the same way it already surfaces `database_provisioned`/`ai_provisioned`.

## 2. Infisical deployment

**Known gap, resolve first:** zorc's `deploy()` only builds from a GitHub
repo (Nixpacks or the repo's own Dockerfile) — there's no "deploy this exact
registry image" path. A wrapper repo (`FROM infisical/infisical:latest`,
nothing else) is the straightforward fix, already created at
github.com/zaindroid/infisical-selfhost.

**Second known gap:** `create_coolify_app()` hardcodes
`"health_check_path": "/health"` in its Coolify payload. Infisical's actual
health endpoint needs confirming before this "just works" — if it's
something else (e.g. `/api/status`), either teach `create_coolify_app()` to
accept a configurable `health_check_path` (the more honest, reusable fix —
this will bite again for any future prebuilt-image deploy, not just this
one), or add a tiny shim in the wrapper repo's Dockerfile.

**app.yaml** (once Redis support from step 1 exists):
```yaml
name: infisical
memory_mb: 512   # placeholder, needs a real analyze_deployment_requirements() estimate
database: true
redis: true
env:
  ENCRYPTION_KEY:
    generate: hex
  AUTH_SECRET:
    generate: hex
```
`SITE_URL` is chicken-and-egg (depends on the subdomain deploy() assigns) —
two-step: deploy first, then `set_app_env_vars()` with the real URL, then
`redeploy()`. Same pattern already proven working this session.

**Target node:** `hostinger-vps` or `ovh` if a custom domain via
`add_custom_domain()` matters later; `servingz` is fine for internal-only use.

## 3. The actual payoff: seamless secret resolution at deploy time

This is the part that makes it "seamless" rather than just another app with
its own env vars.

- Infisical exposes machine-identity auth + a REST API to read secrets by
  project/path.
- New zorc capability: `env_overrides` (or a new `secret_refs` param) accepts
  `{"KEY": {"from_infisical": "project/path/SECRET_NAME"}}` instead of (or
  alongside) a raw value. `deploy()`/`set_app_env_vars()` resolves it
  server-side at deploy/set time using a zorc-held Infisical machine-identity
  token — same trust tier as the existing Coolify API token in
  `deploy/secrets/`, no new credential-handling pattern invented.
- This is the real win: type a credential into Infisical once, every app
  that needs it references it by name, rotation happens in one place instead
  of hunting down every app.yaml/env_overrides call site that has a copy.
- Build this LAST, as its own PR, once Infisical itself is actually running
  and steps 1–2 are proven live.

## Suggested sequencing for the next session

1. Confirm Infisical's real health endpoint (quick live check against the
   public image, or read its source/docs precisely).
2. Build and ship Redis provisioning (step 1) — testable independently of
   Infisical, lower risk, unblocks step 2.
3. Deploy Infisical itself (step 2), verify it's actually reachable and
   healthy, create a first real secret in it by hand to confirm the whole
   stack works before building any automation on top of it.
4. Build the `from_infisical` resolution layer (step 3) last.

Each step is independently shippable and testable — don't try to land all
three in one PR.
