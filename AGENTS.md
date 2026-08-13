# AGENTS.md

**Read this file completely before writing code, creating files, or deploying.**

This is a multi-app platform spanning **two nodes**, orchestrated through one
Coolify instance. Many services share each host and one set of shared
infrastructure. Your changes must not degrade services you did not write.

If this file conflicts with your instincts, a tutorial you recall, or a pattern
from another codebase — **this file wins.** If something you need isn't covered,
stop and ask the human rather than inventing a convention.

### Nodes

| Node | Reach | Public IP | Use for | Shared Postgres/Redis |
|---|---|---|---|---|
| **servingz** | home network, via Cloudflare Tunnel only | No | default — most apps | Yes, native Docker network |
| **hostinger-vps** | direct, real public IPv4 (`2.25.105.110`) | Yes | apps that need direct public ingress, or when servingz is out of headroom | No — reach servingz's over the internet, or ask first |

Both are managed by the **same Coolify instance** (runs on servingz) as two
Coolify "Servers" — one Coolify project, one API, one dashboard, regardless
of which node an app actually runs on. `registry.yaml`'s `nodes:` section is
the source of truth for each node's budget and Coolify `server_uuid`; every
app's `target:` field names exactly one of those node keys (or `pages` for
the Cloudflare Pages static-site edge target, which isn't a node at all).
`deploy/agent.py`'s `deploy()` takes a `target_node` argument to place a new
app on the node of your choice — defaults to `servingz` if not specified.

Picking a node for a new app:
- Default to **servingz** unless there's a specific reason not to.
- Pick **hostinger-vps** when the app needs direct public-IP ingress
  servingz structurally can't provide (e.g. something that can't sit behind
  the Cloudflare Tunnel), or when servingz's headroom (§1) doesn't fit it.
- Never split one app's pieces across both nodes — one repo, one container,
  one node (§5's "app contract" still holds, just now per-node).

### Deploying from outside this repo

If you're an agent working in a *different* repo and want something
deployed here: don't guess at infrastructure (no Cloudflare Workers, no
random unrelated VPS) -- add the **zorc-mcp** server
(`https://mcp.zaindroid.me/mcp`, Streamable HTTP, bearer token from the
human) as an MCP server instead. Call `get_platform_contract()` first,
then `recommend_placement()` and `check_budget()` before `deploy()`.
`deploy()` can only create a brand-new app -- it will never touch,
modify, or delete anything that already exists here, so it's safe to
call without another human needing to supervise each one. See
`deploy/mcp_server.py` for the full tool surface and guardrails.

---

## 1. Before you write any code

Run these on **the node you're about to deploy to** — servingz and
hostinger-vps are separate hosts with separate Docker state, checking one
tells you nothing about the other. Every task. No exceptions.

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'   # what is running
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'  # what memory is left
free -m
```

Then read §2 below to see what capabilities already exist.

**The most common failure here is building something that already exists.**
The second is adding an app when the node has no room. Both are prevented by
looking first.

Where the live output disagrees with §2 of this file, the live output is
correct and this file is stale — fix it in your PR.

---

## 2. What already exists

### Shared infrastructure

**As of 2026-08-09: Postgres and Redis are live.** Object storage and the
LLM gateway are still NOT provisioned — apps must not assume either exists;
**stop and ask** before assuming, same as before. Coolify's own internal
`coolify-db`/`coolify-redis` remain platform-only, never for apps — the
shared instances below are separate, dedicated deployments.

| Service | Reach it at | Use for | Never |
|---|---|---|---|
| **Postgres 18** | `t5amapezxhesfta6w82ksyt0:5432` (internal Docker network only, `coolify` network) | relational data, job state, anything durable | query another app's DB; add a second Postgres |
| **Redis 7** | `h8nk9npsxzv9kkklvgqb93zj:6379` (internal Docker network only, `coolify` network) | queues, cache, rate limits, locks | store anything you can't lose; add another broker |
| **Object storage** | not provisioned — ask first | files, audio, images, exports over ~100 KB | store user files on the container filesystem |
| **LLM gateway** | not provisioned — ask first | all LLM, VLM, embedding, STT, TTS calls | call a provider SDK directly; hold a provider key in an app |

Postgres/Redis: **one database + one role per app** (Postgres) / **one
logical DB index per app** (Redis) — created on request when an app needs
one, not self-service. Connection details arrive as `DATABASE_URL`/
`REDIS_URL` environment variables at deploy time. Never hardcode the
hostnames above directly in app code — use the env vars so credentials and
routing can change without an app redeploy.

The LLM gateway is OpenAI-compatible — point any OpenAI SDK at `LLM_BASE_URL`.
Routing, failover, caching, cost tracking and tracing all live there. An app
that bypasses it loses all of that and will be rejected in review.

File uploads go **browser → presigned storage URL → storage**, directly. Never
proxy file bytes through an app container.

### Application capabilities — call these, don't reimplement them

<!-- MAINTAIN THIS TABLE. Every app that exposes an API adds a row here in the
     same PR that creates it. An app missing from this table is invisible to
     the next agent, who will then rebuild it. -->

| Capability | Service | Endpoint | Operations |
|---|---|---|---|
| _(none yet — add rows as apps are built)_ | | | |

Every app exposes `GET /openapi.json`. Fetch it to learn an API rather than
reading its source.

### Deliberately not available

Do not add these on your own initiative:

- **Email sending** — no provider configured. Ask.
- **Vector search** — Postgres with pgvector is the intended path when needed. Ask first; it changes the shared Postgres footprint.
- **Full-text search** — use Postgres `tsvector`. Do not add Elasticsearch or Meilisearch.
- **Message broker** — Redis queues are sufficient. Do not add Kafka, RabbitMQ, or NATS.

---

## 3. Decision procedure

Work down this list. Stop at the first match.

**1. Does the capability already exist?** (§2)
Call it over HTTP. Do not reimplement, do not copy its code, do not build "a
simpler version."

**2. Is it a natural extension of an existing app?**
Extend when *all* hold: same data the app already owns, same deploy lifecycle,
similar memory profile.
Create new when *any* hold: needs its own data, must scale or fail
independently, very different resource profile, different concern entirely.

Fewer well-scoped services beat many thin ones. When torn, extend.

**3. Static site or purely client-side frontend?**
It does not belong on the node. Deploy to the edge platform. Zero node memory.

**4. Long-running or scheduled work?**
It's a queue worker, not an HTTP service. No ingress, no subdomain.

**5. Otherwise, a new app.** Decide which node it targets (see "Picking a
node" above), then check *that node's* budget *before* writing code — the
two nodes have separate budgets, checking the wrong one tells you nothing:

```
sum(memory_mb of apps targeting this node) + yours  ≤  (usable_mb − reserved_mb) × max_utilisation
```

using that node's numbers from `registry.yaml`'s `nodes:` section (same
math `scripts/check_budget.py` enforces in CI). `reserved_mb` covers the
platform, Traefik and the OS on every node, plus Postgres and Redis on
servingz specifically (they don't run on hostinger-vps). If your app
doesn't fit on the node you wanted: **stop and tell the human.** Do not
shrink your declared limit to squeeze in. Do not raise the utilisation ceiling.

**6. Need infrastructure that doesn't exist?** Stop and ask.

---

## 4. Hard rules

Breaking any of these fails the task, even if the app works.

1. **No host port binding.** Traefik reaches containers by name on the shared
   network. Port collisions are structurally impossible — keep it that way.
2. **No cross-app database access.** Each app owns its database. Data moves
   between apps over HTTP or the queue, never over SQL.
3. **No unbounded containers.** Every service declares a memory limit.
4. **No secrets in the repo.** Environment variables only. No `.env` committed.
5. **No direct model provider calls.** Everything through the gateway.
6. **No long work in HTTP handlers.** Over ~10 seconds goes on the queue and
   returns a job id. The edge cuts connections at 100 seconds.
7. **No file bytes through app containers.** Presigned URLs; you handle keys.
8. **No new shared infrastructure** without human approval.
9. **No deploying straight to production.** Staging first, always.
10. **No destructive migrations.** Expand, deploy, contract — across two
    releases, never one.
11. **No editing another app's repository** to make yours work.
12. **No weakening a CI gate** to make a build pass.

---

## 5. The app contract

One repository, one container, one subdomain, one database.

### Required at repo root

```
Dockerfile              multi-stage, non-root user, HEALTHCHECK, <500 MB final
.dockerignore
app.yaml                declares this app to the platform
.github/workflows/ci.yml  copied unmodified from the template
README.md
tests/
```

### `app.yaml`

```yaml
name: transcribe             # lowercase-hyphens, unique platform-wide
description: Audio transcription API
owner: <human>

runtime:
  port: 8080                 # port INSIDE the container
  memory_mb: 512             # hard limit
  replicas: 1

domains:
  staging: transcribe-staging.zaindroid.me
  production: transcribe.zaindroid.me

dependencies:
  postgres: true
  redis: true
  storage: true
  internal: []               # other apps this calls, by name

provides:                    # add a matching row to §2 of AGENTS.md
  - id: transcription
    operations: [POST /jobs, GET /jobs/{id}]

health:
  path: /health
```

### Required endpoints

| Path | Returns | Purpose |
|---|---|---|
| `GET /health` | 200 `{"status":"ok"}` | liveness — **must not touch the DB** |
| `GET /ready` | 200 when deps reachable | readiness — may check DB and Redis |
| `GET /version` | `{"sha":…,"built":…}` | deploy verification |
| `GET /openapi.json` | OpenAPI spec | so the next agent can call you |

`/health` must answer in under a second and must never fail because a
dependency is down. If health checks hit Postgres, one slow query makes every
app on the node report unhealthy and restart at once.

### Standard environment variables

```
DATABASE_URL   REDIS_URL   APP_ENV   LOG_LEVEL
LLM_BASE_URL   LLM_API_KEY   LLM_MODEL
S3_ENDPOINT    S3_BUCKET    S3_ACCESS_KEY_ID   S3_SECRET_ACCESS_KEY
```

Fail loudly at startup if a required variable is missing. Never fall back to a
default for anything security-relevant.

### Typical memory footprints

| Kind | memory_mb |
|---|---|
| Static site | 0 — edge, not the node |
| Small API (Node, FastAPI) | 256–512 |
| App with SSR | 512–768 |
| Background worker | 512 |
| Anything larger | justify it in the PR |

---

## 6. Testing gates

All four must pass before staging. Staging must pass before production.

**Gate 1 — static.** Lint, type check, dependency audit, secret scan,
`app.yaml` validation.

**Gate 2 — unit.** Pure logic. No network, no database. Under 60 seconds.

**Gate 3 — integration.** Against ephemeral Postgres and Redis service
containers in CI — never against staging or production data. Must prove
migrations apply cleanly to an empty database *and* are idempotent on a second
run. Every route covered: happy path plus one failure. External APIs stubbed;
never call a paid API from CI.

**Gate 4 — container smoke.** Build the image, start it, assert `/health`
returns 200 within 30s, `/version` reports the SHA being built, the process is
non-root, and the image is under the size limit.

**After staging — E2E.** Critical path only, against the real staging URL. Do
not build a 200-case suite; it will rot and get disabled.

**After any deploy — cross-app regression.** Hit `/health` and one critical
path on **every other app**. This is what catches "the new worker ate the RAM
and killed the CRM." Failure rolls the deploy back.

An app with no integration test is not done.

---

## 7. Deployment

```
feature branch  →  gates 1–4
merge to main   →  gates 1–4  →  staging  →  E2E  →  regression
                                                        │
                                          human approval │
                                                        ▼
                                       production  →  smoke  →  rollback on fail
```

- Images build in CI and are pulled by the platform. **Never build on the
  host, never `docker compose up` on the host, never `docker run` manually.**
- **Production promotes the exact image tested in staging.** Never a rebuild.
  Tag by commit SHA, never `latest`.
- **Rollback = redeploy the previous SHA.** Keep the last five.
- **Migrations run pre-deploy**, not on container boot. A failed migration
  aborts the deploy rather than leaving a half-migrated fleet.
- **A human promotes to production. You do not.**

---

## 8. Observability

Every app must:

- log structured JSON to stdout, never to files
- carry a request id through every log line, propagated via `X-Request-Id`
- never log secrets, tokens, full request bodies, or personal data
- emit a startup line with name, SHA, and environment

Any app making LLM calls must trace every run: step count, tokens, outcome.

---

## 9. If your app runs agents

- **Hard step cap** on tool-call iterations, enforced in code
- **Hard timeout** — exceeded runs are killed and marked failed
- **Token budget** checked before the call, not after
- **Concurrency cap** per worker container
- **Run state in Postgres**, never process memory — workers are disposable
- **Idempotent job handling** — a job may be delivered twice

---

## 10. Stop and ask when

- the memory budget doesn't fit
- you need infrastructure that doesn't exist
- a migration would drop or rename anything
- the task needs changes to another app's repo
- you would need to skip or weaken a CI gate
- an existing app is already unhealthy
- memory headroom is under 20%
- anything here is ambiguous for your task

Asking costs one message. Guessing costs an outage.

---

## 11. Definition of done

- [ ] Checked §2 — this doesn't already exist
- [ ] Memory budget verified before coding
- [ ] All required endpoints implemented, `/health` touches nothing
- [ ] `app.yaml` complete and valid
- [ ] Integration tests pass against ephemeral Postgres and Redis
- [ ] Container smoke passes locally
- [ ] §2 capability table updated if this app exposes an API
- [ ] No secrets committed, no ports bound, no cross-app SQL
- [ ] Deployed to staging and verified; production left to the human
