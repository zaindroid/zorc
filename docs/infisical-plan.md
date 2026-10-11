# Redis as a first-class resource + Infisical shared secrets platform

Status: **shipped and verified live** (2026-10-12). This began as a plan
written 2026-10-11 and is now the reference for what was built, what
differs from the original plan, and what is still manual. Each step landed
as its own PR.

| Step | PR | State |
|---|---|---|
| 1. `redis: true` dedicated Redis per app | #8 | merged, live-verified |
| 2. `health_path` + `generate: hex16`, Infisical deployed | #12 (+ deploy) | merged; Infisical running at https://infisical.zaindroid.me |
| 3. `from_infisical` references | #14 | merged, verified end to end against the real instance |
| Infisical's own error text in failures | #15 | see PR |

## 1. Redis as a first-class resource

`app.yaml` takes a top-level `redis: true`, same shape as `database: true`.
`deploy()` provisions a **dedicated** Redis per app (never shared), generates
its password, verifies it with an authenticated `PING`, and sets `REDIS_URL`
before the app's first start. The caller never sees the credential.

- Coolify nodes: `POST /databases/redis` with a generated `redis_password`.
- zorc-agent nodes: `docker run redis:7-alpine`, password passed by env var
  and expanded by the container's shell so it never appears in process args.
- 64 MB is added to the app's memory for the budget check
  (`DEDICATED_REDIS_MEMORY_MB`).
- A failed deploy rolls Redis back (and the Postgres created just before it,
  if Redis is what failed). `delete_app` removes it on teardown.
- `registry.yaml`'s dead `redis_db: null` field was replaced by `redis`;
  `analyze_deployment_requirements()` reports `redis_provisioned`.

Not changed: `delete_app` on the Coolify path still does not remove an app's
dedicated Postgres (pre-existing behaviour, deliberately left alone).

## 2. Infisical deployment

Deployed from `github.com/zaindroid/infisical-selfhost` (a thin wrapper
around `infisical/infisical`) on servingz as app `infisical`.

What the original plan did not anticipate, all handled:

- **Health route:** Infisical's is `/api/status`, not `/health`.
  `create_coolify_app()` now takes `health_check_path`, driven by
  `app.yaml`'s optional `health_path` (default `/health`, validated).
- **Key size:** `ENCRYPTION_KEY` is a 16-byte hex value; the existing
  `generate: hex` yields 32 bytes. Added `generate: hex16`.
- **DB variable name:** Infisical reads `DB_CONNECTION_URI`, zorc injects
  `DATABASE_URL`. The wrapper's entrypoint maps one to the other.
- **Heap:** upstream defaults to a 2 GB Node heap; the wrapper lowers it
  (`--max-old-space-size=640`) to fit the container limit.
- **`SITE_URL`:** chicken-and-egg as planned; set with `set_app_env_vars()`
  after the first deploy, which triggers a rolling redeploy.

Wrapper `app.yaml`: `database: true`, `redis: true`,
`health_path: /api/status`, `ENCRYPTION_KEY: {generate: hex16}`,
`AUTH_SECRET: {generate: hex}`. Reported memory ~918 MB (704 analysed plus
the dedicated Postgres and Redis).

Manual, one-time (not automatable by design): the first admin account, a
project, and a Universal Auth machine identity added to that project.

The wrapper image is still `FROM infisical/infisical:latest`; pinning a
version is advisable.

## 3. Secret resolution at deploy time

`env_overrides` (`deploy()`, `request_gpu_service()`) and `set_app_env_vars()`
accept, instead of a literal string:

```json
{"STRIPE_KEY": {"from_infisical": "<project-id>/<folders>/<SECRET_NAME>",
                "environment": "prod"}}
```

`environment` is optional (default `prod`). The first segment is the project
**ID** (the UUID from the project's settings, not its name or slug), the
last is the secret name, anything between is the folder path.

- Resolved server-side with Universal Auth login, then
  `GET /api/v4/secrets/{name}?projectId&environment&secretPath`.
- In `deploy()` this runs in the `resolve_env_vars` step, before anything is
  created, and only for keys `app.yaml` declares `required: true`.
- Values never appear in logs or errors; the audit log records reference
  paths and key names only.

### Config

`deploy/secrets/infisical.json`, placed by a human like `coolify.json`:

```json
{"url": "https://infisical.zaindroid.me",
 "client_id": "...", "client_secret": "...",
 "project_access": {"<project-id>": ["owner-name", ...]}}
```

### Authorization (added beyond the original plan)

The machine identity can read every project it is granted, so without a gate
any caller able to `deploy()` could pull any such secret into an app they
own. Admins may reference anything; any other caller may only reference
projects that list their owner name in `project_access`. A project not
listed is admin-only. Malformed references (including `.`/`..` segments)
are rejected up front.

### Gotchas found during the live test

- `project_access` keys and references use the project **ID**. An org ID
  (easy to copy from the URL) fails with `Project ... not found during bot
  lookup`.
- Environment slugs are the short ones (`dev`, `staging`, `prod`), not the
  display names.
- The machine identity must be added to the project (Access Control, then
  Machine Identities) before it can read anything in it.
- The client secret should be rotated if it was ever pasted into a chat or
  message.

## Verification record

2026-10-12: with a real secret in the `zorc` project, one
`set_app_env_vars()` call using a `from_infisical` reference was accepted
and, after the rolling redeploy, the target container's environment held the
secret's value.
