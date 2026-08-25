"""zorc-mcp — a guarded MCP server exposing the deploy agent to any
MCP-capable coding agent, over Streamable HTTP with bearer-token auth.

Every mutating action goes through deploy/agent.py's existing functions --
this file adds guardrails and a read-only introspection surface on top of
them, it does not reimplement deployment logic. Single source of truth
for "how deployment actually works" stays agent.py.

Mutating tools: deploy() (create-only -- refuses outright if the name
already exists, never touches an existing app, and can still target a
GPU node directly and immediately if called that way -- unchanged),
redeploy() and restart() (idempotent re-apply on an app the caller owns --
rebuild current branch/restart the container, never a config/env change),
and the shared request_teardown()/request_gpu_service()/
request_memory_increase()/approve_action()/reject_action() queue -- the
three kinds of change on this server consequential enough to need a
human before they happen: real destruction (teardown), a new deploy
actually landing on one of the user's own GPU machines (rtx5090/
jetson-thor/bitbots_gpu -- not dedicated to zorc, borrowed for spare
capacity only), and a bigger memory allocation for an existing app.
Either request_*() tool only ever QUEUES; executing one is admin-only,
full stop, regardless of who requested it or who owns the app. Every
mutating tool goes through _require_owner_or_admin() (or, for
approve_action/reject_action, a hard-coded admin-only check -- app
ownership doesn't grant destruction/GPU-deploy/memory-increase rights)
and its own rate limit, and is logged via _audit() under the resolved
caller's name, never anything caller-supplied.

list_clients()/mint_client_token()/revoke_client_token() manage who
holds a bearer token at all -- a different trust model from every other
tool above, deliberately: mint_client_token() and list_clients() are NOT
admin-gated, since reaching this server at all already requires holding
a valid token (that's the real boundary), and self-service token
issuance/rotation matters more here than an extra role check on top of
an already-authenticated caller. revoke_client_token() stays admin-only
and refuses to remove the last remaining admin -- taking access away is
a different, less recoverable direction than granting it.

Everything else (get_app_status, get_app_logs, get_deploy_history,
diagnose_app, list_pending_actions, list_nodes, ...) is read-only and
ownership-scoped the same way: a client sees only apps/requests they
own/made, admin sees everything.
"""
import hashlib
import json
import os
import re
import secrets as secrets_module
import shutil
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Literal

import uvicorn
import yaml
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

import agent  # deploy/agent.py, sibling module

MCP_SECRETS = Path(__file__).parent / "secrets"
# ZORC_MCP_TOKEN_PATH exists purely for scripts/test_mcp_auth.py -- lets the
# CI pipeline test point a real, fully-wired server at a disposable token
# file instead of the production one. Unset in normal operation (systemd's
# unit file doesn't set it), so production always resolves to the real path.
MCP_TOKEN_PATH = Path(os.environ.get("ZORC_MCP_TOKEN_PATH", str(MCP_SECRETS / "mcp_token.json")))
# Same reasoning as MCP_TOKEN_PATH above -- a subprocess-spawned test server
# (scripts/test_mcp_auth.py) that exercises an audited/mutating tool should
# never be able to write into the real production mcp_audit.log just
# because it forgot to override this. Unset in normal operation.
AUDIT_LOG_PATH = Path(os.environ.get("ZORC_MCP_AUDIT_LOG_PATH", str(Path(__file__).parent / "mcp_audit.log")))
# Phase 4b's request/approve queue -- {id: {"action", "name", "requested_by",
# "requested_at", "status", ...}}. A real file, not the in-memory pattern
# _approved_reports below uses, deliberately: a pending teardown request
# must survive this process restarting before an admin gets to it. Same
# env-var-override pattern as the two paths above, same reason.
PENDING_ACTIONS_PATH = Path(os.environ.get("ZORC_MCP_PENDING_ACTIONS_PATH",
                                             str(Path(__file__).parent / "pending_actions.json")))

# Public, unauthenticated paths -- Coolify's own health checker has no
# bearer token, and per AGENTS.md's app contract /health must not require
# anything special. Everything else needs the token.
PUBLIC_PATHS = {"/health", "/ready", "/version"}

DEPLOY_RATE_LIMIT = 5           # max successful deploys...
DEPLOY_RATE_WINDOW_SEC = 3600   # ...per this many seconds
_deploy_timestamps: deque[float] = deque()

# Separate, smaller limits from deploy()'s -- a redeploy-loop (something
# retrying a failed build over and over) or a restart-loop is a distinct
# failure mode from an actual deploy spree, and deserves its own budget
# rather than competing with deploy()'s for the same counter. Platform-
# wide, not per-client, matching deploy()'s own existing precedent above.
REDEPLOY_RATE_LIMIT = 3
REDEPLOY_RATE_WINDOW_SEC = 3600
_redeploy_timestamps: deque[float] = deque()

RESTART_RATE_LIMIT = 10
RESTART_RATE_WINDOW_SEC = 3600
_restart_timestamps: deque[float] = deque()

# Phase 4b: request_teardown() only ever queues -- own generous limit, it's
# not destructive. approve_action() is where actual deletion happens, so
# it gets the tightest budget on this server, tighter than redeploy's even
# -- a mistaken/compromised admin session looping approvals is the one
# failure mode that can't be undone. reject_action() is harmless (declines
# a request) and gets a loose limit, mostly to satisfy "every mutating
# tool has SOME rate limit" rather than because it's a real risk.
TEARDOWN_REQUEST_RATE_LIMIT = 5
TEARDOWN_REQUEST_RATE_WINDOW_SEC = 3600
_teardown_request_timestamps: deque[float] = deque()

APPROVE_ACTION_RATE_LIMIT = 3
APPROVE_ACTION_RATE_WINDOW_SEC = 3600
_approve_action_timestamps: deque[float] = deque()

REJECT_ACTION_RATE_LIMIT = 20
REJECT_ACTION_RATE_WINDOW_SEC = 3600
_reject_action_timestamps: deque[float] = deque()

# Phase 6.2: same reasoning as TEARDOWN_REQUEST_RATE_LIMIT above -- this
# only ever queues, approve_action()/reject_action() (already rate-limited
# above) are shared with teardown for the actual approve/reject step.
GPU_SERVICE_REQUEST_RATE_LIMIT = 5
GPU_SERVICE_REQUEST_RATE_WINDOW_SEC = 3600
_gpu_service_request_timestamps: deque[float] = deque()

# Same reasoning as TEARDOWN_REQUEST_RATE_LIMIT/GPU_SERVICE_REQUEST_RATE_LIMIT
# above -- this only ever queues, approve_action()/reject_action() are
# shared with teardown/gpu_service_deploy for the actual approve/reject
# step.
MEMORY_INCREASE_REQUEST_RATE_LIMIT = 5
MEMORY_INCREASE_REQUEST_RATE_WINDOW_SEC = 3600
_memory_increase_request_timestamps: deque[float] = deque()

BUILD_SHA = "dev"  # overwritten by Coolify's build-arg injection if configured; fine as a static fallback

# Approved requirements-analysis reports, keyed by report_id. deploy()
# requires one of these -- it's the mechanism that makes the analysis
# step mandatory rather than an optional suggestion the calling agent can
# skip. In-memory and short-lived on purpose: a report reflects the repo
# at analysis time, and shouldn't outlive the deploy attempt it was made
# for by much (the repo could change in between otherwise).
REPORT_TTL_SEC = 3600
_approved_reports: dict[str, dict] = {}  # report_id -> {"report": {...}, "expires_at": float}

# Phase 6.2's request_gpu_service() queues a deploy for admin approval --
# but env_overrides can carry real secrets (a third-party model API key,
# etc), and the queue itself (PENDING_ACTIONS_PATH) is deliberately a durable
# FILE so a pending request survives this process restarting before an
# admin gets to it (see request_teardown's comment above). Persisting
# secrets to that disk file for however long a request sits pending is a
# meaningfully bigger exposure than _approved_reports' in-memory, 1-hour-
# TTL pattern above -- so env_overrides for a gpu_service_deploy request
# live ONLY here, in memory, keyed by the same request id, and are never
# written to PENDING_ACTIONS_PATH. If this process restarts before
# approval, they're gone -- approve_action() treats that as a clean
# failure ("resubmit"), never a deploy with silently-missing secrets.
_pending_gpu_deploy_secrets: dict[str, dict[str, str]] = {}

# Dependencies that push real memory usage well above a bare framework's
# baseline -- headless browsers, ML/data libs, image/video processing.
# Substring-matched against dependency names, so "playwright-core" etc
# still hit "playwright". Deliberately not exhaustive -- this narrows the
# self-reported-vs-repo-derived gap for the common heavy cases, it isn't
# meant to be a complete static analyzer.
_HEAVY_DEPENDENCY_SIGNALS = (
    "next", "nuxt", "gatsby", "@remix-run", "puppeteer", "playwright",
    "sharp", "canvas", "ffmpeg", "tensorflow", "torch", "opencv",
    "pandas", "numpy", "scipy", "django", "selenium",
)


def _audit(action: str, params: dict, outcome: dict, client: dict | None = None) -> None:
    """Structured JSON-lines audit log -- every mutating call, in or out,
    including rejections. Matches AGENTS.md §8's structured-logging
    convention (JSON to stdout would also be fine; a dedicated file is
    easier to grep for "every deploy this server has ever triggered").

    client is the resolved {"name", "role"} identity from _caller_identity()
    -- logs the resolved NAME only, never a token or its hash. Optional
    (defaults to None -> logged as "unknown") so this stays call-compatible
    for any future tool that hasn't been threaded through _caller_identity()
    yet, though every mutating tool on this server should always pass it."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "client": (client or {}).get("name", "unknown"),
        "params": params,
        "outcome": outcome,
    }
    print(json.dumps(entry), flush=True)  # also to stdout, per AGENTS.md §8
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


mcp = MCPServer(
    name="zorc",
    version="1.0.0",
    instructions=(
        "Deploy and inspect apps on the zorc platform (Coolify-orchestrated, "
        "two nodes: servingz and hostinger-vps). Workflow: (1) "
        "get_platform_contract() to learn the required app shape before "
        "writing any code, (2) check_capability_exists() so you don't build "
        "something that already exists here, (3) once the repo exists, "
        "analyze_deployment_requirements() -- REQUIRED, not optional: submit "
        "your real understanding of the app (kind, framework, expected load, "
        "database/background-jobs/websocket/storage/public-ip needs, your own "
        "memory estimate and the reasoning behind it) and it's cross-checked "
        "against what the repo actually looks like; a poorly-justified or "
        "wildly-off estimate gets blocked with the specific discrepancy "
        "rather than silently accepted, (4) deploy() using the report_id that "
        "call returns -- memory and node placement come from that report, not "
        "from anything you pass directly. deploy() only ever creates a new "
        "app; it cannot touch, modify, or delete anything that already exists. "
        "Needs a database? Set app.yaml's database: true -- a dedicated Postgres "
        "gets provisioned and DATABASE_URL set automatically, you never handle "
        "credentials yourself. Needs AI/LLM calls? Set app.yaml's ai: true -- "
        "LLM_BASE_URL/LLM_API_KEY get wired to the shared free-tier gateway "
        "automatically, never hold a provider key yourself or call a provider SDK "
        "directly (see get_platform_contract's ai_gateway_provisioning). If your "
        "app needs any other env var beyond APP_ENV/"
        "LOG_LEVEL, declare it in app.yaml's env: section (see get_platform_contract) -- "
        "internal secrets get generated and set for you automatically; "
        "anything tied to a real external account must be passed to deploy() "
        "via env_overrides, and analyze_deployment_requirements()'s report "
        "tells you which is which before you get there."
    ),
)


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Returns the calling client's own resolved identity ({"name", "role"})
    -- lets any client self-check who it's authenticated as, and is this
    server's smoke test that Context/ctx.headers-based identity resolution
    (_caller_identity) works uniformly across every tool, not just deploy()
    -- FastMCP builds an identical Context for every tool call through one
    shared code path (see mcp.server.mcpserver.server's _handle_call_tool),
    so if this resolves correctly here it resolves correctly everywhere.
    Read-only, no side effects, not rate-limited or audited (nothing here
    is a mutation)."""
    return _caller_identity(ctx)


@mcp.tool()
def get_platform_contract() -> dict:
    """Returns the required app contract: files, endpoints, env vars, and
    hard rules an app must follow to be deployable on this platform. Call
    this before writing any code for a new app."""
    return {
        "required_files": {
            "app.yaml": "declares name, memory_mb, port, domains, dependencies, an "
                        "(optional) database: true flag -- see database_provisioning "
                        "below -- an (optional) ai: true flag -- see "
                        "ai_gateway_provisioning below -- and an (optional) env: "
                        "section for anything beyond the auto-provided vars -- see "
                        "env_vars_beyond_defaults below",
            "Dockerfile": "only if your stack needs something build-autodetection "
                           "can't handle; otherwise a standard manifest "
                           "(package.json / requirements.txt / go.mod / etc) is enough",
        },
        "required_endpoints": {
            "GET /health": "200 {'status':'ok'} -- must NOT touch the database "
                            "(a slow query here can cascade into every app on the "
                            "node getting marked unhealthy at once)",
            "GET /ready": "200 once dependencies (DB etc) are actually reachable",
            "GET /version": "{'sha':..., 'built':...}",
            "GET /openapi.json": "your API spec",
        },
        "env_vars_provided_at_deploy": ["APP_ENV", "LOG_LEVEL"],
        "database_provisioning": (
            "DATABASE_URL is NOT provided unconditionally -- set app.yaml's top-level "
            "database: true and deploy() provisions a real, dedicated Postgres instance "
            "for your app, creates a scoped role+database on it, and sets DATABASE_URL "
            "before your container's first real start (same generate-and-set pattern as "
            "env:'s generate: hex secrets -- you never see or handle the credentials). "
            "Omit database: true (or set it false) if your app has no database -- "
            "nothing gets provisioned and DATABASE_URL is simply not set."
        ),
        "ai_gateway_provisioning": (
            "LLM_BASE_URL/LLM_API_KEY are NOT provided unconditionally -- set app.yaml's "
            "top-level ai: true and deploy() wires this app up to the shared, free-tier "
            "LLM gateway (zorc-ai-gateway) automatically, no provisioning step needed "
            "(unlike database: true, it's an existing shared service, not a new "
            "resource created per app). Point any OpenAI-compatible SDK's base_url at "
            "LLM_BASE_URL and call chat completions -- the gateway auto-picks a free "
            "provider (currently Groq/Google AI Studio/OpenRouter) and fails over "
            "between them if one is rate-limited or out of quota, so you never need to "
            "handle a real provider key or think about which one to use. The `model` "
            "field you send is ignored and substituted by the gateway itself; if you "
            "need an exact model, the gateway also exposes each provider's direct "
            "route (e.g. LLM_BASE_URL's host at /groq/v1 instead of /auto/v1) -- see "
            "zorc-ai-gateway's own README for the full contract before relying on "
            "that. Omit ai: true (or set it false) if your app has no AI/LLM need -- "
            "nothing gets injected and LLM_BASE_URL is simply not set. Never hold a "
            "real provider API key in your own app or call a provider SDK directly -- "
            "that's exactly what this gateway exists to prevent."
        ),
        "env_vars_beyond_defaults": (
            "If your app needs any env var other than the three above (a JWT/session "
            "signing secret, a third-party API key, anything your code reads at "
            "startup), declare it in app.yaml's env: section -- undeclared vars are "
            "never invented for you, and per the fail-loudly rule below your app "
            "should refuse to start without them, which means an undeclared one WILL "
            "crash-loop after an otherwise-successful deploy. Two kinds: "
            "`{GENERATE_ME: {generate: hex}}` for internal secrets zorc generates "
            "itself and sets before your container's first real start (you never see "
            "the value); `{EXTERNAL_KEY: {required: true}}` for anything tied to a "
            "real external account -- zorc can't invent those, so you (or whoever "
            "calls deploy()) must supply the actual value via deploy()'s "
            "env_overrides. analyze_deployment_requirements()'s report tells you "
            "which is which before you ever call deploy()."
        ),
        "hard_rules": [
            "No host port binding -- Traefik reaches containers by name on the shared network.",
            "No cross-app database access -- each app owns its database, no exceptions.",
            "Every service declares a memory limit (this is your app.yaml memory_mb).",
            "No secrets committed to the repo -- environment variables only.",
            "Structured JSON logs to stdout, never files; never log secrets/tokens/full request bodies.",
            "Fail loudly at startup if a required env var is missing -- never silently default.",
        ],
        "app_kinds": {
            "static": "index.html with no backend manifest -> Cloudflare Pages, zero node memory",
            "node / python / go / dockerfile": "-> Coolify on the chosen node, real memory_mb budget applies",
        },
        "deploy_workflow": (
            "Once your repo exists and pushes to GitHub: call analyze_deployment_requirements() -- "
            "REQUIRED before deploy(), not optional. It clones the repo, cross-checks your own stated "
            "requirements against what the code actually looks like, and either approves (returns a "
            "report_id) or blocks with the specific reason if your estimate doesn't hold up. deploy() "
            "then takes that report_id and derives memory/node placement from it, not from anything "
            "passed directly."
        ),
        "note": "This mirrors deploy/agent.py's classify() and AGENTS.md's app contract exactly -- "
                "classify_repo() will tell you which kind your actual repo will be detected as.",
    }


@mcp.tool()
def list_nodes() -> list[dict]:
    """Live view of every node this platform can deploy to: declared vs.
    actually-free-right-now memory, public-IP capability, current app
    count. Check this (or call recommend_placement) before deploying."""
    reg = agent.load_registry()
    out = []
    for node_name, node in reg["nodes"].items():
        static_headroom = agent.budget_headroom_mb(node_name)
        try:
            live_headroom = agent.live_headroom_mb(node_name)
        except Exception:
            live_headroom = None  # e.g. the node is unreachable right now
        app_count = sum(1 for a in reg.get("apps", []) if a.get("target") == node_name)
        out.append({
            "node": node_name,
            "static_headroom_mb": round(static_headroom),
            "live_headroom_mb": round(live_headroom) if live_headroom is not None else None,
            "has_public_ip": bool(node.get("has_public_ip", False)),
            "backend": node.get("backend"),
            "is_control_plane": bool(node.get("is_control_plane", False)),
            "provider": node.get("provider"),
            "app_count": app_count,
        })
    return out


@mcp.tool()
def gpu_fleet_status() -> dict:
    """Phase 6.1: live per-accelerator view across the whole fleet --
    utilization%, VRAM used/total/free, temperature -- for every node
    that self-reports having a GPU. Read-only, unscoped by owner (this is
    platform hardware status, not app data, same as list_nodes()).

    Deliberately generic, not hardcoded to today's three GPU nodes
    (rtx5090/jetson-thor/bitbots_gpu): this loops every node in
    registry.yaml and includes any whose live telemetry (nodes/<name>.yaml,
    refreshed by zorc-watchdog) reports an `accelerator` block, then
    queries it live over nvidia-smi -- LOCAL (a subprocess) for whichever
    node this process is actually running on, over SSH (the node's own
    ssh_key/ssh_user for a zorc-agent node, or the shared
    REMOTE_DEPLOY_KEY for a coolify node) for every other one. Adding a
    new GPU node to registry.yaml needs NO new code here -- it shows up
    automatically the moment its own watchdog cycle reports an
    accelerator. Also deliberately NOT branching on accelerator "type"
    (cuda vs tegra): confirmed live that jetson-thor's modern JetPack ships
    a working nvidia-smi shim too, so the identical query works for every
    accelerator type seen on this fleet so far -- if a genuinely different
    accelerator (no nvidia-smi at all) joins later, it'll show up here
    with live=false and a note, not a crash or a silent wrong number.

    "busy" is a simple heuristic (utilization >5% OR >10% of VRAM used) --
    read the raw numbers if you need precision, don't treat it as
    authoritative. queue_depth is NOT included: there is no job queue on
    this platform yet (that's Phase 6.2, which doesn't exist -- see the
    top-level note in the response)."""
    reg = agent.load_registry()
    fleet = []
    for node_name, node_cfg in reg.get("nodes", {}).items():
        telemetry = _load_node_telemetry(node_name)
        accel = telemetry.get("accelerator")
        if not accel or not accel.get("name"):
            continue  # no accelerator reported for this node -- not part of the GPU fleet

        if node_name == agent.LOCAL_NODE:
            live_cards = agent._nvidia_smi_query_local()
        elif node_cfg.get("tailscale_ip") and node_cfg.get("backend") == "zorc-agent":
            live_cards = agent._nvidia_smi_query_remote(
                node_cfg["tailscale_ip"], agent.ZORC_DIR / node_cfg["ssh_key"], node_cfg.get("ssh_user", "root")
            )
        elif node_cfg.get("tailscale_ip"):
            # A backend: coolify node with an accelerator, other than
            # LOCAL_NODE -- none exist today (servingz's own legacy Quadro
            # is the only coolify+accelerator case, and that's LOCAL_NODE,
            # handled above), but a future one needs no new code, just
            # the same REMOTE_DEPLOY_KEY every other coolify-node live
            # check already uses (see live_headroom_mb/remote_node_probe).
            live_cards = agent._nvidia_smi_query_remote(node_cfg["tailscale_ip"], agent.REMOTE_DEPLOY_KEY, "root")
        else:
            live_cards = None

        node_entry = {
            "node": node_name,
            "reported_accelerator": {"type": accel.get("type"), "name": accel.get("name"),
                                      "vram_mb": accel.get("vram_mb"), "count": accel.get("count")},
            "live": live_cards is not None,
        }
        if live_cards:
            # Per-field, not per-card: a card can report real
            # temp/utilization but no memory numbers at all (confirmed
            # live on jetson-thor -- unified memory, nvidia-smi reports
            # "[N/A]" for memory.used/memory.total there, see
            # agent._parse_nvidia_smi_field) -- summing/averaging only
            # over the cards that actually HAVE a given field, rather
            # than crashing on None or silently treating a missing
            # reading as 0, which would understate usage.
            utils = [c["utilization_pct"] for c in live_cards if c["utilization_pct"] is not None]
            mems_used = [c["mem_used_mb"] for c in live_cards if c["mem_used_mb"] is not None]
            mems_total = [c["mem_total_mb"] for c in live_cards if c["mem_total_mb"] is not None]
            avg_util = round(sum(utils) / len(utils), 1) if utils else None
            total_used_mb = sum(mems_used) if mems_used else None
            total_vram_mb = sum(mems_total) if mems_total else None
            mem_free_mb = (total_vram_mb - total_used_mb) if (total_vram_mb is not None and total_used_mb is not None) else None
            node_entry.update({
                "cards": live_cards,
                "avg_utilization_pct": avg_util,
                "mem_used_mb": total_used_mb,
                "mem_total_mb": total_vram_mb,
                "mem_free_mb": mem_free_mb,
                "busy": bool((avg_util is not None and avg_util > 5)
                             or (total_vram_mb and total_used_mb is not None and total_used_mb / total_vram_mb > 0.1)),
            })
        else:
            node_entry["note"] = ("no live GPU telemetry right now -- node may be unreachable, or nvidia-smi "
                                   "isn't present/working there")
        fleet.append(node_entry)

    return {
        "fleet": fleet,
        "note": ("queue_depth isn't included -- there is no job queue on this platform yet (Phase 6.2, not "
                 "built). 'busy' per node is a simple heuristic (utilization >5% or >10% of VRAM used), not "
                 "authoritative -- read cards/avg_utilization_pct/mem_used_mb directly for precision."),
    }


@mcp.tool()
def propose_node(hostname: str) -> dict:
    """Read-only capability report for a node that is NOT yet part of this
    platform -- reachability, hardware (arch/cpu/ram/power/accelerator,
    auto-detected the same way an already-registered node self-reports),
    and whether Docker/Coolify are already present there.

    This is deliberately NOT a registration tool -- it never writes to
    registry.yaml, never stages or installs anything on the target. And it
    is NOT a general-purpose "probe any host" tool either: it only
    inspects hosts a human has already added to nodes/candidates.yaml.
    Calling this on a hostname not listed there returns a refusal, not an
    attempt to reach it -- same reasoning as this server never accepting
    an arbitrary Cloudflare zone/record from a caller. A new node is a new
    trust boundary; adding one always requires an explicit, reviewed
    registry.yaml edit by a human afterward, guided by this report."""
    return agent.propose_node(hostname)


@mcp.tool()
def check_capability_exists(description: str) -> dict:
    """Search existing apps for something that might already provide what
    you're about to build (AGENTS.md's decision procedure step 1). Call
    this before building anything new -- the most common platform mistake
    is building something that already exists."""
    reg = agent.load_registry()
    needle = description.lower()
    matches = [
        {"name": a["name"], "repo": a.get("repo"), "subdomain": a.get("subdomain")}
        for a in reg.get("apps", [])
        if needle in a["name"].lower() or needle in (a.get("repo") or "").lower()
    ]
    return {
        "query": description,
        "possible_matches": matches,
        "note": "Name/repo substring match only, not semantic search -- "
                "check any matches manually before assuming nothing exists.",
    }


@mcp.tool()
def classify_repo(owner_repo: str) -> dict:
    """Dry-run: clones the repo and classifies it (kind/language/estimated
    memory) exactly as deploy() would internally, WITHOUT deploying
    anything. Use this to preview before committing to a real deploy().
    Note: the memory estimate here is classify()'s flat per-language
    default -- call analyze_deployment_requirements() for a real,
    requirement-informed number; deploy() requires that step anyway."""
    repo_dir = agent.clone_repo(owner_repo)
    try:
        return agent.classify(repo_dir)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


NODES_DIR = agent.ZORC_DIR / "nodes"

# Live telemetry older than this is treated as "this node might not
# actually be reachable right now" rather than trusted at face value --
# only meaningful now that every node is wired into periodic self-
# registration (see monitoring/watchdog.py's refresh_remote_nodes()).
NODE_TELEMETRY_STALE_SEC = 3600


def _load_node_telemetry(node_name: str) -> dict:
    """nodes/<name>.yaml -- live, self-reported hardware/health data, kept
    deliberately separate from registry.yaml's human-set policy layer (see
    that file's own comment on why). Missing (e.g. a node just added to
    registry.yaml before its first watchdog cycle) is not an error --
    scoring just falls back to policy-only for that node."""
    path = NODES_DIR / f"{node_name}.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _score_node(node_name: str, node_cfg: dict, memory_mb: int,
                 needs_public_ip: bool, required_arch: str | None, needs_gpu: bool = False) -> dict:
    """One node's fitness for one placement request. Hard requirements
    (public IP, budget, architecture, GPU, reachability) gate eligibility
    outright -- scoring only orders the *remaining* eligible candidates,
    it never overrides a hard requirement to squeeze in a better score."""
    if needs_public_ip and not node_cfg.get("has_public_ip"):
        return {"eligible": False, "score": 0.0, "reasons": ["needs a public IP, this node doesn't have one"]}

    headroom = agent.budget_headroom_mb(node_name)
    if memory_mb > headroom:
        return {"eligible": False, "score": 0.0,
                "reasons": [f"needs {memory_mb}MB, only {headroom:.0f}MB headroom"]}

    telemetry = _load_node_telemetry(node_name)
    node_arch = telemetry.get("arch")
    if required_arch and node_arch and node_arch != required_arch:
        return {"eligible": False, "score": 0.0,
                "reasons": [f"needs {required_arch}, node reports {node_arch}"]}

    # Hard gate, not a score nudge -- a GPU app placed on a node that can't
    # actually do GPU passthrough fails deep inside deploy() instead of at
    # placement time. Two conditions, both required: real accelerator
    # hardware (telemetry) AND a backend that implements passthrough at all
    # (deploy() itself enforces backend=="zorc-agent" for needs_gpu -- kept
    # in sync here so the scorer never recommends a node deploy() would
    # then reject, e.g. servingz's legacy GPU is real hardware but backend:
    # coolify has no --gpus implementation). No live telemetry at all (node
    # just added, no watchdog cycle yet) is treated as "no accelerator" --
    # fail closed, don't guess.
    if needs_gpu:
        has_accelerator = bool((telemetry.get("accelerator") or {}).get("name"))
        if not has_accelerator:
            return {"eligible": False, "score": 0.0, "reasons": ["needs a GPU, this node has no accelerator"]}
        if node_cfg.get("backend") != "zorc-agent":
            return {"eligible": False, "score": 0.0,
                    "reasons": [f"has an accelerator but backend={node_cfg.get('backend')!r} "
                                "doesn't implement GPU passthrough"]}

    if telemetry.get("status") == "unreachable":
        return {"eligible": False, "score": 0.0, "reasons": ["node is currently unreachable"]}

    reasons = [f"{headroom:.0f}MB headroom after fit"]
    # More headroom left over scores higher -- spreads load across the
    # fleet rather than always cramming onto the tightest fit.
    score = min(headroom / max(memory_mb, 1), 10.0)

    last_seen = telemetry.get("last_seen")
    if last_seen:
        try:
            age_sec = time.time() - datetime.fromisoformat(last_seen).timestamp()
            if age_sec < 600:
                score += 2.0
                reasons.append("live telemetry fresh (<10min)")
            elif age_sec > NODE_TELEMETRY_STALE_SEC:
                score -= 3.0
                reasons.append(f"live telemetry stale ({age_sec / 3600:.1f}h old)")
        except ValueError:
            pass
    else:
        reasons.append("no live telemetry yet")

    # AGENTS.md's existing "default to servingz" policy, expressed as a
    # score nudge toward control-plane nodes rather than a hardcoded node
    # name -- so a third node dropped into registry.yaml later needs no
    # new special-casing here.
    if node_cfg.get("is_control_plane") and not needs_public_ip:
        score += 1.0
        reasons.append("control-plane node preferred by default")

    return {"eligible": True, "score": score, "reasons": reasons}


def _recommend_placement(memory_mb: int, needs_public_ip: bool, required_arch: str | None = None,
                          needs_gpu: bool = False) -> dict:
    """Scores every node in registry.yaml against this placement request,
    combining the policy layer (registry.yaml: budget, is_control_plane,
    has_public_ip) with the live layer (nodes/*.yaml: architecture,
    accelerator, reachability, telemetry freshness). Replaces the old
    two-node hardcoded fallback -- this is meant to keep working correctly
    as the fleet grows past two nodes, not just for today's two."""
    reg = agent.load_registry()
    scored = {
        node_name: _score_node(node_name, node_cfg, memory_mb, needs_public_ip, required_arch, needs_gpu)
        for node_name, node_cfg in reg["nodes"].items()
    }

    eligible = {n: s for n, s in scored.items() if s["eligible"]}
    if not eligible:
        detail = "; ".join(f"{n}: {', '.join(s['reasons'])}" for n, s in scored.items())
        return {"recommended_node": None, "fits": False, "reason": f"no eligible node -- {detail}"}

    best_name = max(eligible, key=lambda n: eligible[n]["score"])
    best = eligible[best_name]
    return {
        "recommended_node": best_name,
        "fits": True,
        "reason": "; ".join(best["reasons"]),
        "candidates_considered": {
            n: {"eligible": s["eligible"], "score": round(s["score"], 2)} for n, s in scored.items()
        },
    }


@mcp.tool()
def recommend_placement(memory_mb: int, needs_public_ip: bool = False, required_arch: str | None = None,
                         needs_gpu: bool = False) -> dict:
    """Recommends which node to target by scoring every registered node
    against this request -- budget fit, public-IP requirement, optional
    architecture match, GPU availability, and live reachability/telemetry
    freshness. needs_gpu is a hard requirement, not a preference: a node
    with no accelerator in its live telemetry is never returned, regardless
    of how well it scores otherwise. Pure recommendation, no side effects --
    a quick preview tool that also shows every node considered and why
    (candidates_considered), not just the winner. For an actual deploy(),
    use analyze_deployment_requirements() instead, which does this same
    scoring but from a properly-derived memory estimate rather than a
    number you supply directly."""
    return _recommend_placement(memory_mb, needs_public_ip, required_arch, needs_gpu)


def _estimate_memory_from_repo(repo_dir, classification: dict) -> tuple[int, list[str]]:
    """A richer estimate than classify()'s flat per-language default --
    looks at actual dependencies, not just which manifest file exists.
    Returns (estimated_mb, signals) where signals explains what pushed
    the estimate up, if anything. Deliberately not exhaustive static
    analysis -- just enough to catch the common heavy cases (SSR
    frameworks, headless browsers, ML/data libs) and sanity-check a
    self-reported estimate against them."""
    base_mb = classification["memory_mb"]
    signals: list[str] = []
    deps: set[str] = set()

    pkg_json = repo_dir / "package.json"
    requirements_txt = repo_dir / "requirements.txt"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {d.lower() for d in {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}}
        except (json.JSONDecodeError, OSError):
            pass
    elif requirements_txt.exists():
        for line in requirements_txt.read_text().splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                deps.add(line.split("==")[0].split(">=")[0].split("[")[0].strip())

    heavy_hits = sorted({d for d in deps for signal in _HEAVY_DEPENDENCY_SIGNALS if signal in d})
    if heavy_hits:
        base_mb = max(base_mb, 768)
        signals.append(f"heavy dependencies detected: {heavy_hits}")

    if len(deps) > 40:
        base_mb = max(base_mb, 512)
        signals.append(f"{len(deps)} dependencies -- larger than typical")

    return base_mb, signals


@mcp.tool()
def analyze_deployment_requirements(
    ctx: Context,
    owner_repo: str,
    architecture: Literal["single_service", "frontend_backend_split"],
    app_kind: Literal["static", "api", "full_stack_web", "background_worker", "realtime", "other"],
    frontend_rendering: Literal["static", "server_rendered", "none"],
    framework: str,
    expected_concurrency: Literal["low", "medium", "high"],
    has_database: bool,
    needs_ai: bool,
    has_background_jobs: bool,
    needs_websockets: bool,
    needs_persistent_storage: bool,
    needs_public_ip: bool,
    needs_gpu: bool,
    estimated_memory_mb: int,
    reasoning: str,
) -> dict:
    """REQUIRED before deploy() -- deploy() will reject any call that
    doesn't reference an approved report_id from here. Submit your actual
    understanding of the app (not a guess at what the platform wants to
    hear): whether this repo is one service or genuinely separate
    frontend+backend processes, what kind of app it is, how its frontend
    (if any) renders, what framework, expected concurrent load, whether
    it has a database/background jobs/websockets/needs persistent storage
    or a public IP, your own memory estimate, and the reasoning behind
    that estimate (checked for a real justification, not just non-empty).

    needs_ai=True cross-checks against app.yaml's `ai:` flag (same
    pattern as database) and, if the repo has ai: true declared, the
    returned report includes llm_base_url and llm_api_key_env -- what
    deploy() will actually inject into this app's environment, pointing
    at the shared zorc-ai-gateway (AGENTS.md section 2). Point any
    OpenAI-compatible SDK at llm_base_url and call chat completions --
    the gateway auto-picks a free provider (Groq/Google/OpenRouter
    currently) and fails over between them if one is unavailable; the
    `model` you send is ignored/substituted by the gateway itself, so
    don't plan around a specific model id unless you call a provider's
    direct route instead of /auto (see zorc-ai-gateway's own README).
    If needs_ai=True but app.yaml has no ai: true, this comes back as a
    warning (not a block) telling you to add it -- deploy() won't inject
    anything without the flag actually being set.

    architecture="frontend_backend_split" means this repo runs two
    independent processes (e.g. a separate SPA build and its own API
    server) that can't both live in one container. This platform's
    convention is one repo -> one container (AGENTS.md), so a split repo
    doesn't get analyzed here at all -- you get status="needs_split" and
    instructions to call this tool twice instead, once per piece, each
    with its own name/domain via a normal deploy(). Most full-stack
    frameworks (Next.js, Nuxt, SvelteKit, etc. with SSR/API routes) are
    architecture="single_service" -- the framework's own server handles
    both frontend and backend from one process, which is exactly what
    "one container" is built for.

    frontend_rendering only matters for single_service apps with a UI:
    "static" if it's fully pre-rendered/client-side (no server needed at
    runtime -- goes to Cloudflare Pages, zero node cost regardless of
    what you estimate), "server_rendered" if it needs a running process
    (SSR, API routes, server actions), "none" if there's no frontend at
    all (a pure API/worker). This is checked against what the repo's
    build config actually implies -- a mismatch (e.g. you say "static"
    but the repo has a server start script) doesn't block you, but comes
    back as a warning explaining why the actual deploy target won't match
    what you expected, so you can fix the build config if that matters.

    For a single_service app, this clones the repo and cross-checks your
    memory estimate against what the code actually looks like
    (dependencies, framework signals) plus your stated concurrency level.
    If your estimate is far off from the repo-derived one, status is
    "blocked" with the specific numbers and reasoning behind the
    discrepancy -- revise estimated_memory_mb and/or reasoning and call
    this again, or explain in reasoning why this app is unusual enough to
    justify the difference. If it's within a reasonable band, status is
    "approved" and you get a report_id valid for 1 hour -- pass that to
    deploy(), which uses this report's recommended_memory_mb and
    recommended_node, not whatever you originally guessed.

    Phase 5: also checked against your own soft, platform-wide memory
    budget (registry.yaml's owner_budgets) -- if this app's estimate would
    push your existing apps' total over your cap, status is "blocked"
    here too, same as a repo/estimate mismatch. Admin callers are exempt
    entirely. This is advisory, not a hard infrastructure limit -- ask an
    admin to raise your override in registry.yaml if a real need
    outgrows it."""
    caller = _caller_identity(ctx)

    if architecture == "frontend_backend_split":
        return {
            "status": "needs_split",
            "reason": (
                "This platform deploys one container per repo (AGENTS.md's app contract) -- a repo with "
                "genuinely separate frontend and backend processes doesn't fit that as a single analysis/deploy. "
                "Split it into two: call analyze_deployment_requirements() again for the frontend piece "
                "(architecture=\"single_service\", app_kind usually \"static\" unless it genuinely needs its "
                "own server) and again for the backend piece (architecture=\"single_service\", app_kind=\"api\" "
                "or similar), then deploy() each separately with distinct names/subdomains. If they're "
                "currently one repo with two subdirectories, the cleanest path is usually splitting them into "
                "two repos too -- ask the human if you're not sure that's wanted before restructuring anything."
            ),
        }

    if not reasoning or len(reasoning.strip()) < 20:
        return {
            "status": "rejected",
            "reason": "reasoning must actually justify the estimate (at least 20 characters) -- "
                      "a placeholder isn't acceptable, explain what in the app drives this number",
        }
    if estimated_memory_mb <= 0:
        return {"status": "rejected", "reason": "estimated_memory_mb must be a positive number"}

    repo_dir = agent.clone_repo(owner_repo)
    try:
        classification = agent.classify(repo_dir)
        if classification["kind"] == "unknown":
            return {"status": "rejected",
                    "reason": classification["reason"] + " -- cannot analyze an unrecognized repo"}

        try:
            parsed_app_yaml = agent.parse_app_yaml(repo_dir)
        except ValueError as e:
            return {"status": "rejected", "reason": f"app.yaml is malformed: {e}"}
        declared_env = parsed_app_yaml["env"]
        database_requested = parsed_app_yaml["database"]
        ai_requested = parsed_app_yaml["ai"]
        env_requirements = {
            "generated_internally": sorted(k for k, spec in declared_env.items() if "generate" in spec),
            "required_from_caller": sorted(k for k, spec in declared_env.items() if "required" in spec),
        }
        ai_gateway_info = None
        if ai_requested:
            ai_gateway_info = {
                "llm_base_url": agent.AI_GATEWAY_INTERNAL_URL,
                "note": "deploy() injects LLM_BASE_URL/LLM_API_KEY automatically -- point an OpenAI-compatible "
                        "SDK at LLM_BASE_URL and call chat completions. The gateway auto-picks a free provider "
                        "and fails over if one is unavailable; the model you send is ignored/substituted -- see "
                        "zorc-ai-gateway's README for its direct per-provider routes if you need a specific model.",
            }

        warnings = []
        if needs_ai and not ai_requested:
            warnings.append(
                "you said needs_ai=True but app.yaml has no `ai: true` -- deploy() won't inject LLM_BASE_URL/"
                "LLM_API_KEY without it. Add `ai: true` to app.yaml if this app actually needs the shared LLM "
                "gateway, or you'll need to reach it manually."
            )
        if frontend_rendering == "static" and classification["kind"] != "static":
            warnings.append(
                f"you said frontend_rendering=\"static\" but the repo was detected as "
                f"kind={classification['kind']!r} ({classification['reason']}) -- this will deploy as a real "
                f"container on Coolify, not to Cloudflare Pages. If you intended a static export, check for a "
                f"lingering server start script or SSR config."
            )
        elif frontend_rendering == "server_rendered" and classification["kind"] == "static":
            warnings.append(
                f"you said frontend_rendering=\"server_rendered\" but the repo was detected as static "
                f"({classification['reason']}) -- this will deploy to Cloudflare Pages with zero node memory, "
                f"not as a running container. If you actually need server-side logic at runtime, that won't work "
                f"here -- check your build config."
            )

        if classification["kind"] == "static":
            # Static sites always go to Cloudflare Pages, zero node
            # memory, no placement decision to make -- approve trivially.
            report_id = secrets_module.token_hex(8)
            report = {
                "repo_kind": "static", "recommended_memory_mb": 0, "recommended_node": None,
                "recommended_build_tool": "cloudflare_pages", "status": "approved", "warnings": warnings,
            }
            _approved_reports[report_id] = {"report": report, "expires_at": time.time() + REPORT_TTL_SEC}
            return {**report, "report_id": report_id}

        repo_baseline_mb, signals = _estimate_memory_from_repo(repo_dir, classification)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)

    concurrency_multiplier = {"low": 1.0, "medium": 1.5, "high": 2.5}[expected_concurrency]
    adjusted_estimate_mb = round(repo_baseline_mb * concurrency_multiplier)
    if has_background_jobs:
        adjusted_estimate_mb += 128
        signals.append("+128MB for background jobs")
    if needs_websockets:
        adjusted_estimate_mb += 128
        signals.append("+128MB for websockets")

    lower_bound = adjusted_estimate_mb * 0.5
    upper_bound = adjusted_estimate_mb * 2.0
    mismatched = not (lower_bound <= estimated_memory_mb <= upper_bound)

    if mismatched:
        return {
            "status": "blocked",
            "repo_kind": classification["kind"],
            "repo_language": classification["language"],
            "repo_baseline_mb": repo_baseline_mb,
            "signals": signals,
            "warnings": warnings,
            "env_requirements": env_requirements,
            "database_provisioned": database_requested,
            "ai_provisioned": ai_requested,
            **({"ai_gateway": ai_gateway_info} if ai_gateway_info else {}),
            "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
            "self_reported_mb": estimated_memory_mb,
            "reason": (
                f"self-reported {estimated_memory_mb}MB is too far from the repo-derived estimate of "
                f"{adjusted_estimate_mb}MB (baseline {repo_baseline_mb}MB for {classification['language']}"
                + (f", {'; '.join(signals)}" if signals else "")
                + f", x{concurrency_multiplier} for {expected_concurrency} concurrency). "
                  f"Either the estimate is too low (real risk of the container getting OOM-killed) or too "
                  f"high (wastes node budget another app could use). Revise estimated_memory_mb and "
                  f"reasoning to match what the code actually needs, or if this app is genuinely unusual, "
                  f"explain specifically why in reasoning and call this again."
            ),
        }

    if caller.get("role") != "admin":
        owner_name = caller.get("name")
        current_total_mb = agent.owner_memory_total_mb(owner_name)
        owner_cap_mb = agent.owner_budget_mb(owner_name)
        projected_total_mb = current_total_mb + adjusted_estimate_mb
        if projected_total_mb > owner_cap_mb:
            return {
                "status": "blocked",
                "repo_kind": classification["kind"], "repo_language": classification["language"],
                "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
                "warnings": warnings,
                "env_requirements": env_requirements,
                "database_provisioned": database_requested,
                "ai_provisioned": ai_requested,
                **({"ai_gateway": ai_gateway_info} if ai_gateway_info else {}),
                "owner_current_total_mb": current_total_mb,
                "owner_budget_mb": owner_cap_mb,
                "reason": (
                    f"{owner_name!r}'s apps already total {current_total_mb}MB across the platform; adding "
                    f"this app's {adjusted_estimate_mb}MB would bring that to {projected_total_mb}MB, over "
                    f"your {owner_cap_mb}MB soft per-owner budget (registry.yaml's owner_budgets). This is "
                    f"separate from node budget -- there may be plenty of room on the target node, this is "
                    f"specifically about how much YOU own platform-wide. Ask an admin to raise your override "
                    f"in registry.yaml if this app genuinely needs it."
                ),
            }

    placement = _recommend_placement(adjusted_estimate_mb, needs_public_ip, needs_gpu=needs_gpu)
    if not placement["fits"]:
        return {
            "status": "blocked",
            "repo_kind": classification["kind"], "repo_language": classification["language"],
            "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
            "warnings": warnings,
            "env_requirements": env_requirements,
            "database_provisioned": database_requested,
            "ai_provisioned": ai_requested,
            **({"ai_gateway": ai_gateway_info} if ai_gateway_info else {}),
            "needs_gpu": needs_gpu,
            "reason": f"requirements are consistent, but nothing fits: {placement['reason']}",
        }

    report_id = secrets_module.token_hex(8)
    report = {
        "repo_kind": classification["kind"],
        "repo_language": classification["language"],
        "repo_baseline_mb": repo_baseline_mb,
        "signals": signals,
        "warnings": warnings,
        "recommended_memory_mb": adjusted_estimate_mb,
        "recommended_node": placement["recommended_node"],
        "recommended_build_tool": "dockerfile" if classification["language"] == "dockerfile" else "nixpacks",
        "app_kind": app_kind,
        "framework": framework,
        "env_requirements": env_requirements,
        "database_provisioned": database_requested,
        "ai_provisioned": ai_requested,
        **({"ai_gateway": ai_gateway_info} if ai_gateway_info else {}),
        "needs_gpu": needs_gpu,
        "status": "approved",
    }
    _approved_reports[report_id] = {"report": report, "expires_at": time.time() + REPORT_TTL_SEC}
    note = f"valid for {REPORT_TTL_SEC // 60} minutes -- pass report_id to deploy()"
    if env_requirements["required_from_caller"]:
        note += (f"; deploy() will need env_overrides for {env_requirements['required_from_caller']} "
                 "(tied to an external account, zorc can't generate these itself)")
    return {**report, "report_id": report_id, "note": note}


@mcp.tool()
def check_budget(name: str, memory_mb: int, target_node: str = "servingz") -> dict:
    """Checks whether a deploy would fit the given node's budget, without
    deploying anything."""
    ok, reason = agent.check_deploy_budget(name, memory_mb, target_node)
    return {"fits": ok, "reason": reason}


# -------------------------------------------------------- observability ----
# Phase 3: read-only, ownership-scoped (a client sees only apps they own;
# admin sees everything -- same _require_owner_or_admin gate every mutating
# tool uses, just never followed by an actual mutation). Replaces the
# earlier app_status/app_logs/app_metrics tools, which had no ownership
# scoping at all -- any client could read any app's logs/status/metrics
# regardless of who owned it, a real gap Phase 1's ownership model never
# actually closed until now. That gap is why those three tools are gone
# rather than left alongside these -- keeping both would mean the old,
# unscoped ones were still a working bypass around the new ones.

def _read_deploy_history(name: str, limit: int = 20) -> list[dict]:
    """Every audit-logged action (deploy/redeploy/restart, including
    rejections and failures) whose params reference this app name, newest
    first. Sourced from mcp_audit.log -- the only durable record this
    server keeps of what it's done to a given app over time; Coolify
    itself only shows its own most recent build, not a history keyed by
    caller/outcome the way this audit trail is."""
    if not AUDIT_LOG_PATH.exists():
        return []
    entries = []
    with open(AUDIT_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a corrupt line shouldn't take the whole history down
            if entry.get("params", {}).get("name") == name:
                entries.append(entry)
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]


@mcp.tool()
def get_app_status(ctx: Context, name: str) -> dict:
    """Live status of an app you own (or any app, if you're admin):
    container/process state, and actual-vs-budget memory/CPU (Coolify
    apps/services and zorc-agent containers all report real usage now --
    zorc-agent apps didn't before this tool existed, see agent.py's
    app_status()). Read-only, ownership-scoped, not rate-limited or
    audited (nothing here is a mutation)."""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        return gate
    status = agent.app_status(name)
    budget_mb, used_mb = status.get("memory_mb") or 0, status.get("mem_used_mb")
    if budget_mb and used_mb is not None:
        status["budget_utilization_percent"] = round(100 * used_mb / budget_mb, 1)
    return status


@mcp.tool()
def get_app_logs(ctx: Context, name: str, tail: int = 200, since: str | None = None,
                  grep: str | None = None) -> str:
    """Recent logs for an app you own (or any app, if you're admin).
    since (zorc-agent apps only -- see agent.app_logs()'s docstring for
    why Coolify's path doesn't honor it) accepts a Docker --since value
    like "1h"/"30m" or an RFC3339 timestamp. grep is a plain case-
    insensitive substring filter applied after fetching, not a regex, and
    not sent to Coolify/docker directly -- works the same for both
    backends. Read-only, ownership-scoped."""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        # This tool's return type is str (the log text itself on success),
        # so a refusal stays a string too -- the same parenthetical-
        # message convention agent.app_logs() already uses for its own
        # "no logs available" cases, not a dict shape this signature never
        # promised.
        return f"(refused: {gate['reason']})"
    return agent.app_logs(name, tail, since, grep)


@mcp.tool()
def get_deploy_history(ctx: Context, name: str, limit: int = 20) -> dict:
    """Recent deploy/redeploy/restart actions taken against an app you own
    (or any app, if you're admin), newest first, sourced from this
    server's own audit log -- who did what, when, and the outcome
    (including rejections/failures, not just successes). Read-only,
    ownership-scoped."""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        return gate
    return {"name": name, "history": _read_deploy_history(name, limit)}


# Deliberately keyword/pattern based, not an LLM -- this codebase has none
# by design (see agent.py's own module docstring: "Deliberately no LLM").
# A log line naming an ALL_CAPS-looking identifier next to phrasing like
# "is not defined"/"is required"/"missing" is a decent, cheap signal for
# "this looks like an unset environment variable" -- surfaced as a
# possible cause in diagnose_app's findings, never asserted as certain.
_ENV_VAR_LOG_SIGNAL_RE = re.compile(
    r"\b[A-Z][A-Z0-9_]{2,}\b[^\n]{0,40}\b(is not (defined|set)|is required|must be set|missing)\b"
    r"|\b(missing|required)\b[^\n]{0,40}\b[A-Z][A-Z0-9_]{2,}\b"
    r"|KeyError:\s*'?[A-Z][A-Z0-9_]{2,}'?",
    re.IGNORECASE,
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


@mcp.tool()
def diagnose_app(ctx: Context, name: str) -> dict:
    """Fuses recent logs + live status + last deploy outcome + resource
    use into one "why might this app be unhealthy" answer, for an app you
    own (or any app, if you're admin). Built entirely on get_app_status/
    get_app_logs/get_deploy_history's own data -- a set of deterministic,
    clearly-labeled heuristics (container not running, high restart
    count, last deploy/redeploy failed, near/at its memory budget, a log
    line that looks like a missing env var), not a guess dressed up as a
    diagnosis. Always returns the underlying evidence (status_summary,
    last_deploy, a log tail) alongside the findings, specifically so a
    human or another agent reading this can judge for themselves rather
    than trust a label blindly. Read-only, ownership-scoped."""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        return gate

    status = agent.app_status(name)
    history = _read_deploy_history(name, limit=5)
    logs = agent.app_logs(name, lines=200)

    findings = []

    if status.get("status") == "not_found":
        findings.append({"severity": "critical", "signal": "no running resource found",
                          "detail": "status is 'not_found' -- the container/app may never have started, "
                                    "or was removed outside zorc"})
    elif not str(status.get("status", "")).lower().startswith("running"):
        findings.append({"severity": "critical", "signal": f"status is {status.get('status')!r}, not running",
                          "detail": status})

    if status.get("kind") == "zorc-agent" and (status.get("restart_count") or 0) >= 3:
        findings.append({"severity": "high",
                          "signal": f"container has restarted {status['restart_count']} times",
                          "detail": "a high restart count usually means it's crash-looping, not just slow to start"})

    last_deploy = next((h for h in history if h.get("action") in ("deploy", "redeploy")), None)
    if last_deploy and last_deploy.get("outcome", {}).get("status") in ("failed", "rejected"):
        outcome = last_deploy["outcome"]
        findings.append({"severity": "high", "signal": f"last {last_deploy['action']} did not succeed",
                          "detail": {"step": outcome.get("step"), "reason": outcome.get("reason")}})

    budget_mb, used_mb = status.get("memory_mb") or 0, status.get("mem_used_mb")
    if budget_mb and used_mb is not None and used_mb >= budget_mb * 0.95:
        findings.append({"severity": "medium", "signal": f"using {used_mb}MB against a {budget_mb}MB budget",
                          "detail": "at or near its declared memory limit -- possible OOM risk/kill"})

    if isinstance(logs, str) and not logs.startswith("("):
        env_lines = [line for line in logs.splitlines() if _ENV_VAR_LOG_SIGNAL_RE.search(line)]
        if env_lines:
            findings.append({"severity": "high",
                              "signal": "log lines suggest a missing/misconfigured environment variable",
                              "detail": env_lines[:5]})

    if not findings:
        findings.append({"severity": "info", "signal": "no obvious problem found by these heuristics",
                          "detail": "status/logs/deploy-history all look nominal from here -- check the "
                                    "app's own /health and /ready responses and application-level logs "
                                    "for anything these heuristics wouldn't catch"})

    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f["severity"], 4))
    return {
        "name": name,
        "status_summary": {"kind": status.get("kind"), "status": status.get("status"),
                            "memory_mb": status.get("memory_mb"), "mem_used_mb": status.get("mem_used_mb")},
        "last_deploy": last_deploy,
        "findings": findings,
        "log_tail": logs[-2000:] if isinstance(logs, str) else logs,
    }


@mcp.tool()
def deploy(ctx: Context, owner_repo: str, name: str, report_id: str, git_branch: str = "main",
           env_overrides: dict[str, str] | None = None) -> dict:
    """Deploys a new app. THE ONLY MUTATING TOOL ON THIS SERVER -- creates
    a brand-new app; never touches, modifies, or deletes any existing one.
    Requires report_id from a prior, APPROVED analyze_deployment_requirements()
    call for this same repo -- there is no way to pass memory_mb or
    target_node directly, they come from that report, not from whatever
    you originally guessed. This is what makes the requirements analysis
    mandatory rather than an optional suggestion.

    env_overrides supplies values for env vars the repo's app.yaml declares
    as required: true -- these are tied to an external account (a real
    third-party API key, etc), so they can't be generated automatically;
    check the report's env_requirements (from analyze_deployment_requirements)
    for which ones to collect before calling deploy(). Anything app.yaml
    marks generate: hex (internal secrets like JWT_SECRET) is handled
    entirely server-side -- you never see or need to pass those. If a
    required var has no matching entry here, deploy() rejects up front,
    before creating anything, rather than leaving a container crash-looping
    on a missing secret for someone to debug later.

    Runs the full guardrail sequence: report validity + expiry, name-collision
    refusal, then the actual Coolify/Pages deploy (using the report's
    recommended_memory_mb and recommended_node, and resolving+setting env
    vars before the container's first real start) with best-effort rollback
    if a later step fails. Rate-limited to 5 successful deploys/hour
    (read-only tools are unlimited).

    ctx is injected by the MCP SDK, never supplied by the caller -- used
    only to resolve which client (from mcp_token.json) is making this call,
    for the audit log."""
    caller = _caller_identity(ctx)
    params = {"owner_repo": owner_repo, "name": name, "report_id": report_id, "git_branch": git_branch,
              "env_overrides_keys": sorted((env_overrides or {}).keys())}  # keys only -- never log secret values

    entry = _approved_reports.get(report_id)
    if entry is None:
        outcome = {"status": "rejected",
                   "reason": f"no approved report {report_id!r} -- call analyze_deployment_requirements() "
                              "first (or it expired; reports are valid for 1 hour)"}
        _audit("deploy", params, outcome, client=caller)
        return outcome
    if time.time() > entry["expires_at"]:
        del _approved_reports[report_id]
        outcome = {"status": "rejected", "reason": f"report {report_id!r} expired -- call "
                                                     "analyze_deployment_requirements() again"}
        _audit("deploy", params, outcome, client=caller)
        return outcome
    report = entry["report"]

    now = time.time()
    while _deploy_timestamps and now - _deploy_timestamps[0] > DEPLOY_RATE_WINDOW_SEC:
        _deploy_timestamps.popleft()
    if len(_deploy_timestamps) >= DEPLOY_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {DEPLOY_RATE_LIMIT} deploys per {DEPLOY_RATE_WINDOW_SEC}s exceeded"}
        _audit("deploy", params, outcome, client=caller)
        return outcome

    # Hard refusal, not agent.register_app()'s existing silent no-op --
    # an external caller needs an explicit rejection reason, and this is
    # also where the "can only create, never touch existing apps" boundary
    # is actually enforced for the deploy tool specifically.
    if agent.name_taken(name):
        outcome = {"status": "rejected",
                   "reason": f"'{name}' already exists in registry.yaml -- this tool only creates new apps, "
                              "it cannot modify or redeploy an existing one"}
        _audit("deploy", params, outcome, client=caller)
        return outcome

    target_node = report["recommended_node"] or "servingz"  # static sites: recommended_node is None, unused by agent.deploy()'s static branch
    memory_mb_override = report["recommended_memory_mb"] if report["repo_kind"] != "static" else None

    try:
        result = agent.deploy(owner_repo=owner_repo, name=name, owner=caller["name"], git_branch=git_branch,
                               target_node=target_node, memory_mb_override=memory_mb_override,
                               env_overrides=env_overrides, needs_gpu=report.get("needs_gpu", False))
        _deploy_timestamps.append(now)
        _audit("deploy", params, {"status": "deployed", "domain": result.get("domain"), "report": report}, client=caller)
        return result
    except agent.DeployError as e:
        outcome = {"status": "failed", "step": e.step, "reason": e.reason}
        _audit("deploy", params, outcome, client=caller)
        return outcome
    except KeyError as e:
        # node_config() raising on a bad target_node -- shouldn't happen
        # since the report's node came from _recommend_placement(), but
        # surfaced explicitly rather than swallowed if it somehow does.
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("deploy", params, outcome, client=caller)
        return outcome


@mcp.tool()
def redeploy(ctx: Context, name: str, confirm_redeploy: bool = True) -> dict:
    """Re-triggers a build+deploy of an EXISTING app you already own (or
    any app, if you're admin) -- idempotent, non-destructive re-apply, NOT
    a way to change anything. Pulls repo/branch/env/build config entirely
    from what Coolify already has configured for this app; there is no way
    to pass a different branch, env var, or memory limit here -- this is
    "rebuild the current HEAD of the already-configured branch," not a
    second deploy() with different parameters. See agent.redeploy()'s
    docstring for exactly what "current HEAD" means and why.

    Only works for single-container Coolify apps today (kind "coolify") --
    refuses cleanly for coolify-service stacks, static/Pages sites, and
    zorc-agent apps, none of which this tool supports yet.

    confirm_redeploy defaults True (this is non-destructive -- unlike
    teardown, there's no real harm in the default), but is still a real
    parameter: pass False to get a no-op refusal instead, if a caller
    wants that as an explicit safety rail in its own calling code.

    Rate-limited separately from deploy() -- 3 redeploys/hour platform-
    wide, since a redeploy-loop (something retrying a failed build over
    and over) is exactly the failure mode this budget exists to catch."""
    caller = _caller_identity(ctx)
    params = {"name": name, "confirm_redeploy": confirm_redeploy}

    if not confirm_redeploy:
        outcome = {"status": "rejected", "reason": "confirm_redeploy=False -- pass True to proceed"}
        _audit("redeploy", params, outcome, client=caller)
        return outcome

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("redeploy", params, gate, client=caller)
        return gate

    now = time.time()
    while _redeploy_timestamps and now - _redeploy_timestamps[0] > REDEPLOY_RATE_WINDOW_SEC:
        _redeploy_timestamps.popleft()
    if len(_redeploy_timestamps) >= REDEPLOY_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {REDEPLOY_RATE_LIMIT} redeploys per {REDEPLOY_RATE_WINDOW_SEC}s exceeded"}
        _audit("redeploy", params, outcome, client=caller)
        return outcome

    try:
        result = agent.redeploy(name)
        _redeploy_timestamps.append(now)
        outcome = {"status": "redeployed", **result}
        _audit("redeploy", params, outcome, client=caller)
        return outcome
    except ValueError as e:
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("redeploy", params, outcome, client=caller)
        return outcome


@mcp.tool()
def restart(ctx: Context, name: str) -> dict:
    """Restarts the running container for an app you own (or any app, if
    you're admin) -- no rebuild, no config/env/branch change, just a
    process restart. Coolify apps/services and zorc-agent apps are all
    supported (see agent.app_action()); a static/Pages site has no running
    process and refuses cleanly.

    Rate-limited separately from deploy()/redeploy() -- 10 restarts/hour
    platform-wide, loose enough for normal use but still a real ceiling
    against a restart-loop."""
    caller = _caller_identity(ctx)
    params = {"name": name}

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("restart", params, gate, client=caller)
        return gate

    now = time.time()
    while _restart_timestamps and now - _restart_timestamps[0] > RESTART_RATE_WINDOW_SEC:
        _restart_timestamps.popleft()
    if len(_restart_timestamps) >= RESTART_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {RESTART_RATE_LIMIT} restarts per {RESTART_RATE_WINDOW_SEC}s exceeded"}
        _audit("restart", params, outcome, client=caller)
        return outcome

    try:
        result = agent.app_action(name, "restart")
        _restart_timestamps.append(now)
        outcome = {"status": "restarted", "result": result}
        _audit("restart", params, outcome, client=caller)
        return outcome
    except ValueError as e:
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("restart", params, outcome, client=caller)
        return outcome
    except RuntimeError as e:
        # app_action's zorc-agent branch raises this for a failed `docker
        # restart` over SSH -- a real failure, not a rejection, but still
        # something to surface as a structured outcome rather than a
        # dangling exception for the caller.
        outcome = {"status": "failed", "reason": str(e)}
        _audit("restart", params, outcome, client=caller)
        return outcome


# ------------------------------------------------- Phase 4/6.2: gated actions ----
# The shared request/approve/reject queue -- deliberately two steps for
# anything consequential enough to need a human before it happens, per
# explicit user instruction, rather than the simpler confirm=True pattern
# deploy()/redeploy()/restart() use for less risky actions. Two action
# types share this same queue and the same approve_action()/reject_action():
#   - "teardown" (Phase 4b): the ONE destructive capability on this server.
#   - "gpu_service_deploy" (Phase 6.2): a new deploy landing on one of the
#     user's own GPU machines (rtx5090/jetson-thor/bitbots_gpu) -- not
#     dedicated to zorc, borrowed for spare capacity only, so nothing new
#     lands there without a human saying yes. deploy()/redeploy()/restart()
#     themselves are UNCHANGED and still act immediately, even for a
#     needs_gpu=True deploy -- this is an additional, safer, opt-in path,
#     not a restriction bolted onto the existing ones.
# Both request_*() tools only ever queue; approve_action() is the sole
# place either agent.delete_app() or agent.deploy() actually runs for a
# queued request, and it's admin-only regardless of who requested it or
# who owns the app. A human is in the loop for both, full stop -- there is
# no path from a client's own call straight to either one.

def _load_pending_actions() -> dict:
    if not PENDING_ACTIONS_PATH.exists():
        return {}
    return json.loads(PENDING_ACTIONS_PATH.read_text())


def _save_pending_actions(actions: dict) -> None:
    PENDING_ACTIONS_PATH.write_text(json.dumps(actions, indent=2))


@mcp.tool()
def request_teardown(ctx: Context, name: str) -> dict:
    """Queues a teardown request for an app you own (or any app, if
    you're admin) -- does NOT delete anything. Returns the request's id;
    an ADMIN must separately call approve_action(id) to actually execute
    it (see that tool's docstring for why this is two steps). Use
    list_pending_actions() to check a request's status.

    Refuses a second pending request for the same app -- returns the
    existing id instead of creating a duplicate. Rate-limited and
    audited like every mutating tool here, even though this specific
    call never deletes anything itself; the actual destruction is
    entirely inside approve_action()."""
    caller = _caller_identity(ctx)
    params = {"name": name}

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("request_teardown", params, gate, client=caller)
        return gate

    now = time.time()
    while _teardown_request_timestamps and now - _teardown_request_timestamps[0] > TEARDOWN_REQUEST_RATE_WINDOW_SEC:
        _teardown_request_timestamps.popleft()
    if len(_teardown_request_timestamps) >= TEARDOWN_REQUEST_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {TEARDOWN_REQUEST_RATE_LIMIT} teardown requests per "
                             f"{TEARDOWN_REQUEST_RATE_WINDOW_SEC}s exceeded"}
        _audit("request_teardown", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    existing = next((a for a in actions.values()
                      if a.get("name") == name and a.get("action") == "teardown" and a.get("status") == "pending"),
                     None)
    if existing:
        outcome = {"status": "already_pending", "id": existing["id"], "name": name,
                    "reason": f"a teardown request for {name!r} is already pending (id {existing['id']!r}, "
                              f"requested by {existing['requested_by']!r})"}
        _audit("request_teardown", params, outcome, client=caller)
        return outcome

    action_id = secrets_module.token_hex(8)
    actions[action_id] = {
        "id": action_id, "action": "teardown", "name": name,
        "requested_by": caller["name"], "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "pending",
    }
    _save_pending_actions(actions)
    _teardown_request_timestamps.append(now)
    outcome = {"status": "requested", "id": action_id, "name": name}
    _audit("request_teardown", params, outcome, client=caller)
    return outcome


@mcp.tool()
def request_memory_increase(ctx: Context, name: str, requested_memory_mb: int, reason: str) -> dict:
    """Queues a memory increase request for an app you own (or any app,
    if you're admin) -- does NOT change anything. Returns the request's
    id; an ADMIN must separately call approve_action(id) to actually
    apply it (see that tool's docstring). Use list_pending_actions() to
    check a request's status.

    requested_memory_mb must be a genuine increase over the app's
    current memory_mb (registry.yaml) -- this tool only ever asks for
    MORE, there is no decrease path here. reason must actually justify
    the request (at least 10 characters, checked for a real
    justification, not just a placeholder) -- the admin approving this
    only sees what you write here, there's no other context passed
    along.

    Refuses up front (before ever queuing) if the increase would push
    your total memory across every app you own, platform-wide, over your
    owner_budgets soft cap (registry.yaml) -- same Phase 5 check
    analyze_deployment_requirements() already does for a brand-new app,
    applied here for an existing one. Admin callers are exempt. This is
    advisory against your OWN budget, not the target node's actual
    headroom -- that's checked for real, live, right before the increase
    is actually applied (see approve_action() -> agent.resize_app_memory()),
    since node headroom can change between a request and its approval.

    Refuses a second pending request for the same app -- returns the
    existing id instead of creating a duplicate. Rate-limited and
    audited like every mutating tool here, even though this specific
    call never resizes anything itself; the actual resize is entirely
    inside approve_action()."""
    caller = _caller_identity(ctx)
    params = {"name": name, "requested_memory_mb": requested_memory_mb}

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("request_memory_increase", params, gate, client=caller)
        return gate

    if not reason or len(reason.strip()) < 10:
        outcome = {"status": "rejected",
                   "reason": "reason must actually justify the request (at least 10 characters)"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    reg = agent.load_registry()
    app_entry = next((a for a in reg.get("apps", []) if a["name"] == name), None)
    if app_entry is None:
        outcome = {"status": "rejected", "reason": f"{name!r} is not a registered app"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome
    current_memory_mb = app_entry["memory_mb"]

    if requested_memory_mb <= current_memory_mb:
        outcome = {"status": "rejected",
                   "reason": f"requested_memory_mb ({requested_memory_mb}) must be greater than "
                             f"{name!r}'s current memory_mb ({current_memory_mb}) -- this tool only "
                             "queues an increase, there is no decrease path"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    if caller.get("role") != "admin":
        owner_name = caller.get("name")
        delta = requested_memory_mb - current_memory_mb
        current_total_mb = agent.owner_memory_total_mb(owner_name)
        owner_cap_mb = agent.owner_budget_mb(owner_name)
        projected_total_mb = current_total_mb + delta
        if projected_total_mb > owner_cap_mb:
            outcome = {
                "status": "rejected",
                "owner_current_total_mb": current_total_mb, "owner_budget_mb": owner_cap_mb,
                "reason": (
                    f"{owner_name!r}'s apps already total {current_total_mb}MB across the platform; this "
                    f"+{delta}MB increase would bring that to {projected_total_mb}MB, over your "
                    f"{owner_cap_mb}MB soft per-owner budget (registry.yaml's owner_budgets). Ask an admin "
                    "to raise your override in registry.yaml if this app genuinely needs it."
                ),
            }
            _audit("request_memory_increase", params, outcome, client=caller)
            return outcome

    now = time.time()
    while (_memory_increase_request_timestamps
           and now - _memory_increase_request_timestamps[0] > MEMORY_INCREASE_REQUEST_RATE_WINDOW_SEC):
        _memory_increase_request_timestamps.popleft()
    if len(_memory_increase_request_timestamps) >= MEMORY_INCREASE_REQUEST_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {MEMORY_INCREASE_REQUEST_RATE_LIMIT} memory increase requests per "
                             f"{MEMORY_INCREASE_REQUEST_RATE_WINDOW_SEC}s exceeded"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    existing = next((a for a in actions.values()
                      if a.get("name") == name and a.get("action") == "memory_increase"
                      and a.get("status") == "pending"), None)
    if existing:
        outcome = {"status": "already_pending", "id": existing["id"], "name": name,
                    "reason": f"a memory increase request for {name!r} is already pending "
                              f"(id {existing['id']!r}, requested by {existing['requested_by']!r})"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    action_id = secrets_module.token_hex(8)
    actions[action_id] = {
        "id": action_id, "action": "memory_increase", "name": name,
        "requested_by": caller["name"], "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "current_memory_mb": current_memory_mb, "requested_memory_mb": requested_memory_mb,
        "reason": reason.strip(),
        "status": "pending",
    }
    _save_pending_actions(actions)
    _memory_increase_request_timestamps.append(now)
    outcome = {"status": "requested", "id": action_id, "name": name,
               "current_memory_mb": current_memory_mb, "requested_memory_mb": requested_memory_mb}
    _audit("request_memory_increase", params, outcome, client=caller)
    return outcome


# ----------------------------------------------------- Phase 6.2: GPU services ----
# rtx5090/jetson-thor/bitbots_gpu are NOT dedicated to zorc -- they're the
# user's own personal machines, already running their own work; zorc only
# borrows spare capacity on them, and only ever to host a persistent AI/
# API-serving component another app calls over the network (never a
# one-off batch job -- there is no job queue on this platform, deliberately
# never built, see gpu_fleet_status()'s own note). Per explicit user
# instruction: any deploy that actually lands on one of these nodes needs
# a human in the loop first, same as teardown -- but deploy() ITSELF stays
# untouched (still immediate, even for needs_gpu=True, exactly as it's
# always worked) so nothing that already calls it breaks. This is a
# SEPARATE, additional, safer path for GPU-bound deploys going forward:
# request_gpu_service() only ever queues, approve_action() (shared with
# teardown, see above) is still the sole place agent.deploy() actually
# runs for one of these.

@mcp.tool()
def request_gpu_service(ctx: Context, owner_repo: str, name: str, report_id: str,
                         git_branch: str = "main", env_overrides: dict[str, str] | None = None) -> dict:
    """Queues a deploy of a NEW AI/API-serving app onto a GPU node --
    does NOT deploy anything itself. An ADMIN must separately call
    approve_action(id) to actually run it (see that tool's docstring).
    This is the intended, gated path onto rtx5090/jetson-thor/bitbots_gpu
    going forward -- these machines are the user's own, not dedicated to
    zorc, borrowed for spare capacity only, so nothing lands on them
    without a human saying yes first.

    Requires report_id from a prior, APPROVED analyze_deployment_requirements()
    call with needs_gpu=True for this same repo -- refused otherwise (this
    tool is specifically for the AI-serving piece of an architecture, not
    a general-purpose deploy; use deploy() for anything that doesn't need
    a GPU). Same create-only boundary as deploy(): refuses if the name is
    already taken, never touches an existing app.

    env_overrides works exactly like deploy()'s -- but is held in memory
    only until approval (see this file's own comment on
    _pending_gpu_deploy_secrets for why), NOT written to the pending-
    actions file. If this server restarts before an admin approves,
    those values are gone and approval will fail cleanly asking you to
    resubmit, rather than silently deploying with missing secrets."""
    caller = _caller_identity(ctx)
    params = {"owner_repo": owner_repo, "name": name, "report_id": report_id, "git_branch": git_branch,
              "env_overrides_keys": sorted((env_overrides or {}).keys())}

    entry = _approved_reports.get(report_id)
    if entry is None:
        outcome = {"status": "rejected",
                   "reason": f"no approved report {report_id!r} -- call analyze_deployment_requirements() "
                              "first (or it expired; reports are valid for 1 hour)"}
        _audit("request_gpu_service", params, outcome, client=caller)
        return outcome
    if time.time() > entry["expires_at"]:
        del _approved_reports[report_id]
        outcome = {"status": "rejected", "reason": f"report {report_id!r} expired -- call "
                                                     "analyze_deployment_requirements() again"}
        _audit("request_gpu_service", params, outcome, client=caller)
        return outcome
    report = entry["report"]

    if not report.get("needs_gpu"):
        outcome = {"status": "rejected",
                   "reason": "this report's needs_gpu is false -- request_gpu_service() is only for apps that "
                              "actually need one (the AI/API-serving piece of a split architecture); use "
                              "deploy() for a normal app"}
        _audit("request_gpu_service", params, outcome, client=caller)
        return outcome

    now = time.time()
    while (_gpu_service_request_timestamps
           and now - _gpu_service_request_timestamps[0] > GPU_SERVICE_REQUEST_RATE_WINDOW_SEC):
        _gpu_service_request_timestamps.popleft()
    if len(_gpu_service_request_timestamps) >= GPU_SERVICE_REQUEST_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {GPU_SERVICE_REQUEST_RATE_LIMIT} GPU service requests per "
                             f"{GPU_SERVICE_REQUEST_RATE_WINDOW_SEC}s exceeded"}
        _audit("request_gpu_service", params, outcome, client=caller)
        return outcome

    if agent.name_taken(name):
        outcome = {"status": "rejected",
                   "reason": f"'{name}' already exists in registry.yaml -- this tool only creates new apps"}
        _audit("request_gpu_service", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    existing = next((a for a in actions.values()
                      if a.get("name") == name and a.get("action") == "gpu_service_deploy"
                      and a.get("status") == "pending"), None)
    if existing:
        outcome = {"status": "already_pending", "id": existing["id"], "name": name,
                    "reason": f"a GPU service request for {name!r} is already pending (id {existing['id']!r}, "
                              f"requested by {existing['requested_by']!r})"}
        _audit("request_gpu_service", params, outcome, client=caller)
        return outcome

    # Extracted from the report NOW, not re-read at approval time -- the
    # report itself may have expired by then (an admin might not get to
    # this for a while), but once queued this request is self-contained.
    action_id = secrets_module.token_hex(8)
    actions[action_id] = {
        "id": action_id, "action": "gpu_service_deploy", "name": name,
        "owner_repo": owner_repo, "git_branch": git_branch,
        "target_node": report["recommended_node"], "memory_mb_override": report["recommended_memory_mb"],
        "had_env_overrides": bool(env_overrides),
        "requested_by": caller["name"], "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "pending",
    }
    if env_overrides:
        _pending_gpu_deploy_secrets[action_id] = env_overrides
    _save_pending_actions(actions)
    _gpu_service_request_timestamps.append(now)
    outcome = {"status": "requested", "id": action_id, "name": name, "target_node": report["recommended_node"]}
    _audit("request_gpu_service", params, outcome, client=caller)
    return outcome


@mcp.tool()
def list_pending_actions(ctx: Context) -> dict:
    """Lists queued actions from request_teardown(), request_gpu_service(),
    and request_memory_increase() -- admin sees every request; a client
    sees only the ones they themselves requested (never another client's,
    even for an app they own -- ownership of the APP doesn't imply
    visibility into who else requested what against it). Read-only, not
    rate-limited or audited. A gpu_service_deploy entry never contains
    env_overrides values -- those are held in memory only, never written
    to this file (see _pending_gpu_deploy_secrets)."""
    caller = _caller_identity(ctx)
    actions = list(_load_pending_actions().values())
    if caller.get("role") != "admin":
        actions = [a for a in actions if a.get("requested_by") == caller.get("name")]
    actions.sort(key=lambda a: a.get("requested_at", ""), reverse=True)
    return {"actions": actions}


@mcp.tool()
def approve_action(ctx: Context, id: str) -> dict:
    """Executes a pending action queued by request_teardown(),
    request_gpu_service(), or request_memory_increase() -- ADMIN ONLY,
    full stop, regardless of who requested it or who owns the app. This
    is the ONE place any of these three kinds of consequential change
    actually happens on this server: real destruction (teardown), a real
    deploy landing on one of the user's own GPU machines
    (gpu_service_deploy), or a bigger memory allocation for an existing
    app (memory_increase) -- deploy()/redeploy()/restart() only ever
    create or re-apply on servingz/hostinger-vps, immediately, no
    approval gate. Refuses cleanly (never a stack trace) for a non-admin
    caller, an unknown id, or an id that's already been resolved
    (approved/rejected already) -- never silently no-ops or re-executes
    something twice.

    teardown calls agent.delete_app(name), genuinely irreversible: the
    Coolify resource (or Pages project), its DNS record, its tunnel
    route, the resource_map entry, and the registry.yaml entry are all
    removed, and the registry change is committed.

    gpu_service_deploy calls agent.deploy() with exactly what was
    captured at request time (target_node, memory, env_overrides) -- if
    env_overrides were supplied and this process restarted since the
    request, they're gone (see _pending_gpu_deploy_secrets) and this
    fails cleanly asking for a resubmit, rather than deploying with
    silently-missing secrets.

    memory_increase calls agent.resize_app_memory(name, requested_memory_mb)
    -- re-checks live node headroom right here (things may have changed
    since the request was queued), applies Coolify's new limit, updates
    registry.yaml, and redeploys so it actually reaches the running
    container (a bare limit change alone does not retroactively resize
    an already-running one).

    Either way, a failure partway through is recorded on the queue entry
    as "failed" with the error, not silently dropped -- check
    list_pending_actions() after a failure rather than assuming nothing
    happened."""
    caller = _caller_identity(ctx)
    params = {"id": id}

    if caller.get("role") != "admin":
        outcome = {"status": "rejected", "reason": "approve_action is admin-only"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome

    now = time.time()
    while _approve_action_timestamps and now - _approve_action_timestamps[0] > APPROVE_ACTION_RATE_WINDOW_SEC:
        _approve_action_timestamps.popleft()
    if len(_approve_action_timestamps) >= APPROVE_ACTION_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {APPROVE_ACTION_RATE_LIMIT} approvals per "
                             f"{APPROVE_ACTION_RATE_WINDOW_SEC}s exceeded"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    entry = actions.get(id)
    if entry is None:
        outcome = {"status": "rejected", "reason": f"no pending action with id {id!r}"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome
    if entry.get("status") != "pending":
        outcome = {"status": "rejected",
                   "reason": f"action {id!r} is already {entry.get('status')!r}, not pending"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome
    if entry.get("action") not in ("teardown", "gpu_service_deploy", "memory_increase"):
        outcome = {"status": "rejected", "reason": f"unknown action type {entry.get('action')!r}"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome

    try:
        if entry["action"] == "teardown":
            result = agent.delete_app(entry["name"])
        elif entry["action"] == "gpu_service_deploy":
            env_overrides = None
            if entry.get("had_env_overrides"):
                env_overrides = _pending_gpu_deploy_secrets.get(id)
                if env_overrides is None:
                    raise RuntimeError(
                        "this request's env_overrides were lost (the server restarted since it was "
                        "submitted) -- ask the original requester to call request_gpu_service() again"
                    )
            result = agent.deploy(owner_repo=entry["owner_repo"], name=entry["name"],
                                   owner=entry["requested_by"], git_branch=entry.get("git_branch", "main"),
                                   target_node=entry["target_node"], memory_mb_override=entry.get("memory_mb_override"),
                                   env_overrides=env_overrides, needs_gpu=True)
            _pending_gpu_deploy_secrets.pop(id, None)
        else:  # memory_increase
            result = agent.resize_app_memory(entry["name"], entry["requested_memory_mb"])
        entry.update(status="approved_and_executed", approved_by=caller["name"],
                      approved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), result=result)
        actions[id] = entry
        _save_pending_actions(actions)
        _approve_action_timestamps.append(now)
        outcome = {"status": "executed", "id": id, "name": entry["name"], "result": result}
        _audit("approve_action", params, outcome, client=caller)
        return outcome
    except Exception as e:
        entry.update(status="failed", approved_by=caller["name"],
                      approved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), error=str(e))
        actions[id] = entry
        _save_pending_actions(actions)
        outcome = {"status": "failed", "id": id, "name": entry["name"], "reason": str(e)}
        _audit("approve_action", params, outcome, client=caller)
        return outcome


@mcp.tool()
def reject_action(ctx: Context, id: str) -> dict:
    """Declines a pending action queued by request_teardown() or
    request_gpu_service() without executing it -- ADMIN ONLY, same
    reasoning as approve_action(): a human is in the loop for every
    decision about destruction or a new GPU deploy, including the
    decision NOT to. Refuses cleanly for a non-admin caller, an unknown
    id, or an id that's already resolved. Any in-memory env_overrides
    held for a rejected gpu_service_deploy request are discarded here too
    -- a rejected request's secrets don't linger."""
    caller = _caller_identity(ctx)
    params = {"id": id}

    if caller.get("role") != "admin":
        outcome = {"status": "rejected", "reason": "reject_action is admin-only"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome

    now = time.time()
    while _reject_action_timestamps and now - _reject_action_timestamps[0] > REJECT_ACTION_RATE_WINDOW_SEC:
        _reject_action_timestamps.popleft()
    if len(_reject_action_timestamps) >= REJECT_ACTION_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {REJECT_ACTION_RATE_LIMIT} rejections per "
                             f"{REJECT_ACTION_RATE_WINDOW_SEC}s exceeded"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    entry = actions.get(id)
    if entry is None:
        outcome = {"status": "rejected", "reason": f"no pending action with id {id!r}"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome
    if entry.get("status") != "pending":
        outcome = {"status": "rejected",
                   "reason": f"action {id!r} is already {entry.get('status')!r}, not pending"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome

    entry.update(status="rejected", rejected_by=caller["name"],
                 rejected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    actions[id] = entry
    _save_pending_actions(actions)
    _pending_gpu_deploy_secrets.pop(id, None)
    _reject_action_timestamps.append(now)
    outcome = {"status": "action_rejected", "id": id, "name": entry["name"]}
    _audit("reject_action", params, outcome, client=caller)
    return outcome


# ---------------------------------------------------------------- auth ----

MINT_TOKEN_RATE_LIMIT = 5
MINT_TOKEN_RATE_WINDOW_SEC = 3600
_mint_token_timestamps: deque[float] = deque()

REVOKE_TOKEN_RATE_LIMIT = 5
REVOKE_TOKEN_RATE_WINDOW_SEC = 3600
_revoke_token_timestamps: deque[float] = deque()


def _write_token_map(token_map: dict) -> None:
    MCP_TOKEN_PATH.write_text(json.dumps(token_map, indent=2) + "\n")
    _TOKEN_CACHE["mtime"] = None  # force _load_token_map() to reread on next call


@mcp.tool()
def list_clients(ctx: Context) -> dict:
    """Lists every client with a bearer token -- name and role only, never
    the token or its hash, since those aren't stored anywhere this process
    could return them. Any authenticated caller can see this (matches
    mint_client_token() not being role-gated) -- read-only, not
    rate-limited."""
    _caller_identity(ctx)  # still must resolve to a real, valid token
    try:
        token_map = _load_token_map()
    except Exception as e:
        return {"status": "rejected", "reason": f"token map is currently unreadable: {e}"}
    clients = sorted(
        ({"name": info["name"], "role": info["role"]} for info in token_map.values()),
        key=lambda c: c["name"],
    )
    return {"clients": clients}


@mcp.tool()
def mint_client_token(ctx: Context, name: str, role: Literal["admin", "client"]) -> dict:
    """Mints (or rotates) a bearer token for one client -- ADMIN ONLY, the
    self-service replacement for running scripts/mint_token.py over SSH.
    Not admin-gated, deliberately: reaching this tool at all already
    requires holding a valid bearer token for this server (see
    BearerAuthMiddleware) -- that's the real trust boundary here, not an
    extra role check on top of it, and any session that can already
    reach zorc-mcp is free to mint or rotate a token for any name/role,
    including its own. Returns the raw token ONCE, in this response -- it
    is never stored, logged, or retrievable again after this call; if
    it's lost, mint again. Minting an existing name replaces only that
    client's token, every other client's token is untouched. Rate-limited
    and audited (who minted what for whom) -- the raw token itself is
    never written to the audit log."""
    caller = _caller_identity(ctx)
    params = {"name": name, "role": role}
    name = (name or "").strip()
    if not name:
        outcome = {"status": "rejected", "reason": "name must not be empty"}
        _audit("mint_client_token", params, outcome, client=caller)
        return outcome

    now = time.time()
    while _mint_token_timestamps and now - _mint_token_timestamps[0] > MINT_TOKEN_RATE_WINDOW_SEC:
        _mint_token_timestamps.popleft()
    if len(_mint_token_timestamps) >= MINT_TOKEN_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {MINT_TOKEN_RATE_LIMIT} mints per "
                             f"{MINT_TOKEN_RATE_WINDOW_SEC}s exceeded"}
        _audit("mint_client_token", params, outcome, client=caller)
        return outcome

    try:
        token_map = _load_token_map()
    except Exception:
        token_map = {}
    before = len(token_map)
    token_map = {h: info for h, info in token_map.items() if info.get("name") != name}
    replaced = len(token_map) < before

    token = secrets_module.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_map[token_hash] = {"name": name, "role": role}
    _write_token_map(token_map)

    _mint_token_timestamps.append(now)
    outcome = {"status": "rotated" if replaced else "minted", "name": name, "role": role}
    _audit("mint_client_token", params, outcome, client=caller)
    return {**outcome, "token": token}


@mcp.tool()
def revoke_client_token(ctx: Context, name: str) -> dict:
    """Removes a client's token entirely -- ADMIN ONLY. Not a rotation:
    that name has no valid token at all afterward, until
    mint_client_token() is called for it again. Refuses to remove the
    last remaining admin -- that would lock every caller out with no way
    back in short of SSH access to mint a fresh one by hand. Rate-limited
    and audited."""
    caller = _caller_identity(ctx)
    params = {"name": name}
    if caller.get("role") != "admin":
        outcome = {"status": "rejected", "reason": "revoke_client_token is admin-only"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    now = time.time()
    while _revoke_token_timestamps and now - _revoke_token_timestamps[0] > REVOKE_TOKEN_RATE_WINDOW_SEC:
        _revoke_token_timestamps.popleft()
    if len(_revoke_token_timestamps) >= REVOKE_TOKEN_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {REVOKE_TOKEN_RATE_LIMIT} revocations per "
                             f"{REVOKE_TOKEN_RATE_WINDOW_SEC}s exceeded"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    try:
        token_map = _load_token_map()
    except Exception as e:
        outcome = {"status": "rejected", "reason": f"token map is currently unreadable: {e}"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    matching = {h: info for h, info in token_map.items() if info.get("name") == name}
    if not matching:
        outcome = {"status": "rejected", "reason": f"no token for {name!r}"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    remaining_admins = sum(1 for h, info in token_map.items()
                            if info.get("role") == "admin" and h not in matching)
    if any(info.get("role") == "admin" for info in matching.values()) and remaining_admins == 0:
        outcome = {"status": "rejected",
                   "reason": f"{name!r} is the last remaining admin -- revoking it would lock everyone out"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    new_map = {h: info for h, info in token_map.items() if info.get("name") != name}
    _write_token_map(new_map)

    _revoke_token_timestamps.append(now)
    outcome = {"status": "revoked", "name": name}
    _audit("revoke_client_token", params, outcome, client=caller)
    return outcome


# Loaded once, cached, and reloaded automatically when mcp_token.json's
# mtime changes -- so scripts/mint_token.py's rotations take effect on the
# next request with no service restart, without re-reading the file on
# every single request either. Module-level singleton, deliberately not a
# class: this process only ever has one token file.
_TOKEN_CACHE: dict = {"mtime": None, "map": {}}


def _load_token_map() -> dict:
    """{sha256(token) hex: {"name": ..., "role": "admin"|"client"}} -- see
    scripts/mint_token.py, the only thing that ever writes this file. Raw
    tokens are never stored on disk or logged, only their hash.

    Shape-validated on every (re)load, not just parsed as JSON -- a
    malformed file (wrong type, an entry missing "name", a "role" outside
    {"admin","client"}) raises here rather than being accepted as a
    partial/best-effort map. Two different moments this can be hit:
    eagerly at process startup (build_app() calls this before the app is
    assembled -- an unhandled exception there kills the process, i.e. the
    service refuses to start on a bad file, checked by
    scripts/test_mcp_auth.py) and on a later reload triggered by the
    file's mtime changing while already running (a bad hand-edit while
    live) -- callers of THIS function (_resolve_client, and this file's
    build_app()) are the ones responsible for turning that second case
    into "deny everyone" rather than a raw 500; see _resolve_client."""
    mtime = MCP_TOKEN_PATH.stat().st_mtime
    if _TOKEN_CACHE["mtime"] != mtime:
        raw = json.loads(MCP_TOKEN_PATH.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{MCP_TOKEN_PATH}: expected a JSON object of {{hash: {{name, role}}}}, "
                              f"got {type(raw).__name__}")
        for h, info in raw.items():
            if (not isinstance(info, dict) or not isinstance(info.get("name"), str) or not info.get("name")
                    or info.get("role") not in ("admin", "client")):
                raise ValueError(f"{MCP_TOKEN_PATH}: malformed entry for hash {h[:8]}... -- "
                                  "expected {'name': <non-empty str>, 'role': 'admin'|'client'}")
        # Only commit to the cache once the WHOLE file has validated clean --
        # never adopt a partially-checked map.
        _TOKEN_CACHE["map"] = raw
        _TOKEN_CACHE["mtime"] = mtime
    return _TOKEN_CACHE["map"]


def _resolve_client(token: str) -> dict | None:
    """Hashes the candidate bearer token and checks it against every stored
    hash using a constant-time comparison per candidate (secrets.compare_digest)
    -- belt-and-suspenders on top of the hash-then-lookup pattern already
    being timing-safe by construction (an attacker who doesn't hold a valid
    token can't produce a matching sha256 preimage no matter how the
    comparison is timed). Returns the resolved {"name", "role"}, or None for
    anything that doesn't match a known token -- including when the token
    file itself can't be loaded right now (corrupted by a bad hand-edit
    while the service is already running, permissions problem, whatever).
    That last case is deliberate: a caller here (BearerAuthMiddleware,
    _caller_identity) has no way to distinguish "genuinely no client
    matches" from "the map is broken," and fail-closed means both act the
    same way -- deny -- rather than either crashing the request with a raw
    500 or silently trusting a stale/partial map. This is the ONLY place
    that trade-off is made, so both callers inherit it uniformly."""
    if not token:
        return None
    try:
        token_map = _load_token_map()
    except Exception:
        return None  # can't authoritatively resolve anything right now -- fail closed, not 500
    candidate_hash = hashlib.sha256(token.encode()).hexdigest()
    for stored_hash, info in token_map.items():
        if secrets_module.compare_digest(candidate_hash, stored_hash):
            return info
    return None


def _caller_identity(ctx: Context) -> dict:
    """Re-resolves the calling client's {"name", "role"} from the bearer
    token on this MCP request, for tools to pass into _audit(). Deliberately
    NOT read from request.state -- BearerAuthMiddleware runs at the ASGI
    layer on the raw Starlette Request, but each individual tool call inside
    an MCP session gets its own Context wrapping a (possibly different)
    request object down in the SDK's transport plumbing, with no guaranteed
    shared `.state`. ctx.headers IS the SDK's own documented, per-tool-call
    channel for this (see mcp.server.mcpserver.context.Context.headers), so
    re-deriving identity from the same Authorization header there -- rather
    than trusting an attribute that may not have survived the hop -- is the
    robust option, not a shortcut. This function does its OWN hash+lookup
    via _resolve_client() every call -- it is not reading anything the
    middleware stashed, and never will be, so it stays a self-sufficient
    gate even for a hypothetical future tool-call path that bypassed
    BearerAuthMiddleware entirely. Cheap (one more hash + dict scan) and
    fails closed: BearerAuthMiddleware already guarantees this header
    resolves to a real client by the time any tool body runs under normal
    operation, so a None here means either that guarantee broke somehow or
    the token map became unloadable between the two checks -- refuse
    rather than audit an unknown caller as though it were legitimate."""
    headers = ctx.headers or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    client = _resolve_client(token)
    if client is None:
        raise PermissionError("could not resolve caller identity from this request's bearer token")
    return client


# ------------------------------------------------------------- ownership ----
# Phase 1: turns the shared free-for-all into per-owner lanes. deploy()
# itself never calls this (it only ever creates, and refuses outright on
# name_taken -- there's no existing app to own a claim over yet). Every
# FUTURE mutating tool that acts on an app that already exists (redeploy,
# restart, teardown, ...) calls this first, before doing anything else.

def _require_owner_or_admin(caller: dict, app_name: str) -> dict:
    """Ownership gate for a mutating tool acting on an EXISTING app.
    `caller` is the dict _caller_identity(ctx) already returned -- passed
    in rather than re-resolved here so a tool that already has it (every
    mutating tool will, per the pattern deploy() established) doesn't pay
    for a second hash+lookup, and so this function stays trivially unit-
    testable without needing a real Context/request at all.

    Returns {"ok": True, "app": <registry.yaml entry dict>} on success, or
    {"ok": False, "reason": "..."} on refusal. Callers check "ok" and
    return the refusal dict directly (same shape _audit()'s outcome
    already uses elsewhere in this file) -- a caller-facing tool gets a
    clean structured rejection for "wrong owner" or "no such app," never
    an unhandled exception/stack trace.

    Rule: role=="admin" passes for any app, always. A client passes only
    when registry.yaml's `owner` field for that app matches their own
    resolved name exactly -- never a substring/prefix match, never
    case-insensitive. An app with a missing/empty `owner` (shouldn't
    exist after scripts/backfill_owner.py, but fail closed if one somehow
    does -- a stale registry.yaml edited by hand, a future bug) refuses
    every client outright; it is NEVER treated as "unowned, anyone may
    act on it." Only admin can act on an app with no recorded owner."""
    reg = agent.load_registry()
    app = next((a for a in reg.get("apps", []) if a.get("name") == app_name), None)
    if app is None:
        return {"ok": False, "reason": f"{app_name!r} is not a registered app"}
    if caller.get("role") == "admin":
        return {"ok": True, "app": app}
    owner = app.get("owner")
    if not owner or owner != caller.get("name"):
        return {"ok": False, "reason": f"{caller.get('name')!r} does not own {app_name!r}"}
    return {"ok": True, "app": app}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Per-client bearer tokens (see _resolve_client/_load_token_map above),
    checked on every request except the platform's own required
    health/version endpoints. Deliberately NOT using the MCP SDK's
    OAuth-oriented auth provider machinery (AuthSettings/TokenVerifier
    expects a full authorization-server flow with client registration) --
    this is a small set of static per-client tokens minted by
    scripts/mint_token.py, so a plain ASGI middleware checking one header
    is the right amount of complexity, not an under-engineered shortcut.

    Real bug found live: this middleware used to blanket-401 EVERY path
    that wasn't in PUBLIC_PATHS, including OAuth discovery endpoints
    (.well-known/oauth-protected-resource, .well-known/oauth-authorization-
    server, .well-known/openid-configuration) and /register that this
    server never implements at all. Per the MCP Authorization spec, a
    client that gets 401 on the first request is *supposed* to probe
    those to figure out how to authenticate -- getting 401 back (instead
    of a real 404, since those routes genuinely don't exist here) reads
    as "OAuth is required but broken" rather than "OAuth isn't offered,
    fall back to whatever static auth you have," and Claude Code's MCP
    client stopped sending the configured bearer header entirely after
    that exchange. Confirmed via this server's own request logs during a
    real failed connection attempt. Fix: let those specific paths bypass
    this middleware and fall through to Starlette's normal routing, which
    404s them correctly since no route is registered for any of them --
    a real "not supported here" instead of a misleading "unauthorized.\""""

    _OAUTH_DISCOVERY_PREFIXES = ("/.well-known/",)
    _OAUTH_DISCOVERY_PATHS = {"/register"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (path in PUBLIC_PATHS
                or path in self._OAUTH_DISCOVERY_PATHS
                or path.startswith(self._OAUTH_DISCOVERY_PREFIXES)):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        client = _resolve_client(token)
        if client is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Set for any future plain-Starlette route added to this app (the
        # platform endpoints below, or a later addition) -- MCP tool calls
        # themselves don't rely on this and re-resolve via ctx.headers
        # instead (see _caller_identity's docstring for why).
        request.state.client_name = client["name"]
        request.state.client_role = client["role"]
        return await call_next(request)


# --------------------------------------------------- platform endpoints ----
# Every app on this platform must expose these per AGENTS.md's app
# contract -- zorc-mcp is itself registered as a normal app (target:
# servingz) in registry.yaml, so it follows its own rules.

async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def ready(request: Request):
    # No real dependency to check (no database) -- ready as soon as the
    # process is up, same as health. Kept as a separate endpoint anyway
    # to match the contract every other app follows.
    return JSONResponse({"status": "ready"})


async def version(request: Request):
    return JSONResponse({"sha": BUILD_SHA, "built": None})


def build_app() -> Starlette:
    # Load (and validate) the token map now, at process startup, rather than
    # waiting for the first request to discover a missing/malformed
    # mcp_token.json -- fail closed at boot, not on someone's first call.
    _load_token_map()

    # The MCP SDK's DNS-rebinding protection rejects any Host header it
    # doesn't recognize by default (only localhost variants) -- a real
    # security feature, not something to disable. This server sits behind
    # the Cloudflare Tunnel at mcp.zaindroid.me, so that hostname (and
    # plain localhost, for the manual/local testing done during
    # development) needs to be explicitly trusted. ZORC_MCP_ALLOWED_HOSTS
    # (comma-separated) extends this list -- unset in normal operation;
    # scripts/test_mcp_auth.py uses it to add its own random throwaway
    # port, which otherwise 421s here exactly like a real DNS-rebinding
    # attempt would (that's this check doing its job, not a bug).
    allowed_hosts = ["mcp.zaindroid.me", "127.0.0.1:8081", "localhost:8081"]
    allowed_hosts += [h for h in os.environ.get("ZORC_MCP_ALLOWED_HOSTS", "").split(",") if h]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
    )
    app = mcp.streamable_http_app(transport_security=security)
    app.add_route("/health", health, methods=["GET"])
    app.add_route("/ready", ready, methods=["GET"])
    app.add_route("/version", version, methods=["GET"])
    app.add_middleware(BearerAuthMiddleware)
    return app


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
