# AGENTS.md

**Read this file completely before writing code, creating files, or deploying.**

This is a multi-app platform spanning a **growing fleet of nodes**,
orchestrated through one Coolify instance for every node whose `backend` is
`coolify` (see below). Many services share each host and one set of shared
infrastructure. Your changes must not degrade services you did not write.

If this file conflicts with your instincts, a tutorial you recall, or a pattern
from another codebase — **this file wins.** If something you need isn't covered,
stop and ask the human rather than inventing a convention.

### Nodes

`registry.yaml`'s `nodes:` section is the source of truth for every node:
budget (`usable_mb`, `reserved_mb`, `max_utilisation`), Coolify `server_uuid`,
`has_public_ip`, and two policy fields — **set by a human, in this file
only, never derived from or overridable by a node's own self-reported
data** (see `nodes/<name>.yaml` below): `backend` (`coolify` today;
`zorc-agent` reserved for future lightweight devices that can't run full
Coolify) and `is_control_plane` (whether shared Postgres/Redis/Traefik and
Traefik-fronted apps are allowed there). Every app's `target:` field names
exactly one node key (or `pages` for the Cloudflare Pages static-site edge
target, which isn't a node at all).

Two nodes exist today — **more will be added over time, don't assume
exactly two anywhere in your own reasoning; always read the current set
from `registry.yaml`**:

| Node | Reach | Public IP | Control plane |
|---|---|---|---|
| **servingz** | home network, via Cloudflare Tunnel only | No | Yes — shared Postgres/Redis, default for most apps |
| **hostinger-vps** | direct, real public IPv4 (`2.25.105.110`) | Yes | No — apps that need direct public ingress, or when servingz is out of headroom |

Separately, `nodes/<name>.yaml` (one file per node) holds **live,
self-reported telemetry** — architecture, CPU, RAM, power source, GPU/
accelerator, online status, last-seen timestamp — refreshed automatically
every cycle by `zorc-watchdog.timer` (locally on servingz, over SSH for
every other node). This is informational only: it feeds the placement
scorer's fitness ranking below, it never grants trust or control-plane
status by itself.

Picking a node for a new app is automated, not manual guesswork: call
`recommend_placement()` (or let `analyze_deployment_requirements()` do it
for you as part of the normal deploy flow). It scores every node in
`registry.yaml` on budget fit, public-IP requirement, optional
architecture match, and live reachability/telemetry freshness, and
returns the best-suited one with its reasoning — see
`deploy/mcp_server.py`'s `_recommend_placement()`. The one fixed default
that survives is "prefer a control-plane node when nothing else forces
otherwise" — this file's long-standing default-to-servingz guidance,
expressed as a score nudge rather than a hardcoded node name, so a new
node dropped into `registry.yaml` needs no changes here to participate
correctly.

Never split one app's pieces across both nodes — one repo, one container,
one node (§5's "app contract" still holds, just now per-node).

### Adding a node

Registration is deliberately **not** something an external agent can
trigger on its own — a new node is a new trust boundary (new SSH access,
new compute zorc can reach and run things on) and stays human-confirmed,
indefinitely, not just for now.

What any agent *can* do: call `propose_node(hostname)` for a read-only
capability report — reachability, hardware, whether Docker/Coolify are
already present — but only for a host a human has already added to
`nodes/candidates.yaml`. It refuses anything else outright; it is not a
"probe any host a caller names" tool, same reasoning as this platform
never accepting an arbitrary DNS zone/record from a caller. Use that
report to guide actual onboarding, which a human runs themselves: the
full `bootstrap/*.sh` sequence for a new control-plane-capable node, or
Coolify's own lighter server-add flow for a worker node (see
`hostinger-vps`'s onboarding as precedent) — then add the node to
`registry.yaml`'s `nodes:` section, a normal reviewed edit like any other
change to that file.

**Physical/embedded targets (sensors, robots, voice bots, AI agents) are
explicitly not part of this node model and must never be added as new
actions on the existing deploy surface** — "deploy a container" and
"command a physical device" carry very different real-world blast radii.
Any future capability along those lines needs its own, separately-scoped
MCP tool surface designed from scratch for that risk profile, not bolted
onto this one's already-built auth/audit plumbing because it's
convenient.

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

Run these on **the node you're about to deploy to** — every node is a
separate host with separate Docker state, checking one tells you nothing
about another. Every task. No exceptions.

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

**As of 2026-08-09: Postgres and Redis are live. As of 2026-08-23: the
LLM gateway is live too** (`zorc-ai-gateway` -- free-tier Groq/Google AI
Studio/OpenRouter, auto-failover between them). Object storage is still
NOT provisioned — apps must not assume it exists; **stop and ask** before
assuming, same as before. Coolify's own internal `coolify-db`/
`coolify-redis` remain platform-only, never for apps — the shared
instances below are separate, dedicated deployments.

| Service | Reach it at | Use for | Never |
|---|---|---|---|
| **Postgres 18** | `t5amapezxhesfta6w82ksyt0:5432` (internal Docker network only, `coolify` network) | relational data, job state, anything durable | query another app's DB; add a second Postgres |
| **Redis 7** | `h8nk9npsxzv9kkklvgqb93zj:6379` (internal Docker network only, `coolify` network) | queues, cache, rate limits, locks | store anything you can't lose; add another broker |
| **Object storage** | not provisioned — ask first | files, audio, images, exports over ~100 KB | store user files on the container filesystem |
| **LLM gateway** | `ai-gateway-internal:8080` (internal Docker network only, `coolify` network; stable Coolify `custom_network_aliases`, not the raw container name, which changes every redeploy) | all LLM chat-completion calls | call a provider SDK directly; hold a provider key in an app; assume VLM/embedding/STT/TTS work yet (chat completions only so far) |

Postgres/Redis: **one database + one role per app** (Postgres) / **one
logical DB index per app** (Redis) — created on request when an app needs
one, not self-service. Connection details arrive as `DATABASE_URL`/
`REDIS_URL` environment variables at deploy time. Never hardcode the
hostnames above directly in app code — use the env vars so credentials and
routing can change without an app redeploy.

The LLM gateway is OpenAI-compatible — set app.yaml's `ai: true` and
deploy() injects `LLM_BASE_URL`/`LLM_API_KEY` automatically (same pattern
as `database: true`, see section 5). Point any OpenAI SDK at
`LLM_BASE_URL` and call chat completions; the gateway auto-picks a free
provider and fails over between them if one is unavailable -- the `model`
you send is ignored/substituted by the gateway itself. Cost tracking/
tracing are NOT built yet (deviation from what this section previously
promised) -- routing and failover are real, those two aren't. An app that
bypasses the gateway and calls a provider directly will be rejected in
review.

File uploads go **browser → presigned storage URL → storage**, directly. Never
proxy file bytes through an app container.

### Application capabilities — call these, don't reimplement them

<!-- MAINTAIN THIS TABLE. Every app that exposes an API adds a row here in the
     same PR that creates it. An app missing from this table is invisible to
     the next agent, who will then rebuild it. -->

| Capability | Service | Endpoint | Operations |
|---|---|---|---|
| Free-tier LLM chat completions, auto-failover | ai-gateway | `LLM_BASE_URL` (`ai: true` in app.yaml) | `POST /auto/v1/chat/completions`, `GET /providers`, `GET /usage`, `GET /auto/status` |

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
node" above), then check *that node's* budget *before* writing code — each
node has its own separate budget, checking the wrong one tells you nothing:

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
LLM_BASE_URL   LLM_API_KEY
S3_ENDPOINT    S3_BUCKET    S3_ACCESS_KEY_ID   S3_SECRET_ACCESS_KEY
```

`LLM_API_KEY` is a placeholder value, not a real credential -- the gateway
always uses its own server-held provider key regardless of what's sent to
it, this only exists because most OpenAI SDK clients refuse to construct
without a non-empty api_key. There's no `LLM_MODEL` -- the gateway's
`/auto` route picks the model itself (see the LLM gateway row above); use
a provider's direct route instead if you need an exact model id.

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
