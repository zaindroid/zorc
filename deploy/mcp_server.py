"""zorc-mcp — a guarded MCP server exposing the deploy agent to any
MCP-capable coding agent, over Streamable HTTP with bearer-token auth.

Every mutating action goes through deploy/agent.py's existing functions --
this file adds guardrails and a read-only introspection surface on top of
them, it does not reimplement deployment logic. Single source of truth
for "how deployment actually works" stays agent.py.

Hard boundary: the only mutating tool is deploy(), which can only CREATE
new apps. It cannot delete, stop, restart, or modify any existing app's
config, whether or not that app was created through this server. That
boundary is what limits this server's blast radius to "a new resource
might exist" rather than "an existing app might break."
"""
import json
import shutil
import time
from collections import deque
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

import agent  # deploy/agent.py, sibling module

MCP_SECRETS = Path(__file__).parent / "secrets"
MCP_TOKEN_PATH = MCP_SECRETS / "mcp_token.json"
AUDIT_LOG_PATH = Path(__file__).parent / "mcp_audit.log"

# Public, unauthenticated paths -- Coolify's own health checker has no
# bearer token, and per AGENTS.md's app contract /health must not require
# anything special. Everything else needs the token.
PUBLIC_PATHS = {"/health", "/ready", "/version"}

DEPLOY_RATE_LIMIT = 5           # max successful deploys...
DEPLOY_RATE_WINDOW_SEC = 3600   # ...per this many seconds
_deploy_timestamps: deque[float] = deque()

BUILD_SHA = "dev"  # overwritten by Coolify's build-arg injection if configured; fine as a static fallback


def _audit(action: str, params: dict, outcome: dict) -> None:
    """Structured JSON-lines audit log -- every mutating call, in or out,
    including rejections. Matches AGENTS.md §8's structured-logging
    convention (JSON to stdout would also be fine; a dedicated file is
    easier to grep for "every deploy this server has ever triggered")."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
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
        "two nodes: servingz and hostinger-vps). Call get_platform_contract() "
        "first to learn the required app shape before writing any code. Then "
        "recommend_placement() and check_budget() before calling deploy() -- "
        "deploy() runs those checks again itself, but previewing first avoids "
        "wasted work if something won't fit. deploy() only ever creates a new "
        "app; it cannot touch, modify, or delete anything that already exists."
    ),
)


@mcp.tool()
def get_platform_contract() -> dict:
    """Returns the required app contract: files, endpoints, env vars, and
    hard rules an app must follow to be deployable on this platform. Call
    this before writing any code for a new app."""
    return {
        "required_files": {
            "app.yaml": "declares name, memory_mb, port, domains, dependencies",
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
        "env_vars_provided_at_deploy": ["DATABASE_URL", "APP_ENV", "LOG_LEVEL"],
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
            "provider": node.get("provider"),
            "app_count": app_count,
        })
    return out


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
    anything. Use this to preview before committing to a real deploy()."""
    repo_dir = agent.clone_repo(owner_repo)
    try:
        return agent.classify(repo_dir)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


@mcp.tool()
def recommend_placement(memory_mb: int, needs_public_ip: bool = False) -> dict:
    """Recommends which node to target, implementing AGENTS.md's written
    decision procedure: default to servingz; move to hostinger-vps only if
    direct public-IP ingress is needed or servingz doesn't have headroom.
    Pure recommendation, no side effects -- deploy() re-checks this for
    real at deploy time regardless of what you pass as target_node."""
    reg = agent.load_registry()
    nodes = reg["nodes"]

    if needs_public_ip:
        candidates = [n for n, cfg in nodes.items() if cfg.get("has_public_ip")]
        if not candidates:
            return {"recommended_node": None, "fits": False, "reason": "no node with a public IP exists"}
        node = candidates[0]
        headroom = agent.budget_headroom_mb(node)
        fits = memory_mb <= headroom
        return {
            "recommended_node": node if fits else None, "fits": fits,
            "reason": f"only node with a public IP; headroom {headroom:.0f}MB, needs {memory_mb}MB",
        }

    servingz_headroom = agent.budget_headroom_mb("servingz")
    if memory_mb <= servingz_headroom:
        return {"recommended_node": "servingz", "fits": True,
                "reason": f"default node, fits ({servingz_headroom:.0f}MB headroom)"}
    for node_name in nodes:
        if node_name == "servingz":
            continue
        headroom = agent.budget_headroom_mb(node_name)
        if memory_mb <= headroom:
            return {"recommended_node": node_name, "fits": True,
                    "reason": f"servingz doesn't have headroom ({servingz_headroom:.0f}MB); "
                              f"{node_name} does ({headroom:.0f}MB)"}
    return {"recommended_node": None, "fits": False,
            "reason": f"doesn't fit on any node (needs {memory_mb}MB, servingz has {servingz_headroom:.0f}MB)"}


@mcp.tool()
def check_budget(name: str, memory_mb: int, target_node: str = "servingz") -> dict:
    """Checks whether a deploy would fit the given node's budget, without
    deploying anything."""
    ok, reason = agent.check_deploy_budget(name, memory_mb, target_node)
    return {"fits": ok, "reason": reason}


@mcp.tool()
def app_status(name: str) -> dict:
    """Live status of an existing, already-deployed app. Read-only --
    does not create, modify, or affect anything."""
    return agent.app_status(name)


@mcp.tool()
def app_logs(name: str, lines: int = 200) -> str:
    """Recent logs for an existing, already-deployed app. Read-only."""
    return agent.app_logs(name, lines)


@mcp.tool()
def app_metrics(name: str) -> dict:
    """Live CPU/memory usage for an existing, already-deployed app.
    Read-only -- this is the ongoing resource-tracking data source
    (backed by Coolify's own container stats, the same numbers the
    platform's own memory-pressure alerting uses)."""
    return agent.app_resources(name)


@mcp.tool()
def deploy(owner_repo: str, name: str, target_node: str = "servingz", git_branch: str = "main") -> dict:
    """Deploys a new app. THE ONLY MUTATING TOOL ON THIS SERVER -- creates
    a brand-new app; never touches, modifies, or deletes any existing one.
    Runs the full guardrail sequence: name-collision refusal, classify,
    static + live budget checks, then the actual Coolify/Pages deploy with
    best-effort rollback if a later step fails. Rate-limited to 5
    successful deploys/hour (read-only tools above are unlimited). Call
    get_platform_contract() and recommend_placement() first if you
    haven't -- this tool re-checks everything for real regardless, but
    previewing avoids wasted clone/build time on something that won't fit
    or already exists."""
    params = {"owner_repo": owner_repo, "name": name, "target_node": target_node, "git_branch": git_branch}

    now = time.time()
    while _deploy_timestamps and now - _deploy_timestamps[0] > DEPLOY_RATE_WINDOW_SEC:
        _deploy_timestamps.popleft()
    if len(_deploy_timestamps) >= DEPLOY_RATE_LIMIT:
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {DEPLOY_RATE_LIMIT} deploys per {DEPLOY_RATE_WINDOW_SEC}s exceeded"}
        _audit("deploy", params, outcome)
        return outcome

    # Hard refusal, not agent.register_app()'s existing silent no-op --
    # an external caller needs an explicit rejection reason, and this is
    # also where the "can only create, never touch existing apps" boundary
    # is actually enforced for the deploy tool specifically.
    if agent.name_taken(name):
        outcome = {"status": "rejected",
                   "reason": f"'{name}' already exists in registry.yaml -- this tool only creates new apps, "
                              "it cannot modify or redeploy an existing one"}
        _audit("deploy", params, outcome)
        return outcome

    try:
        result = agent.deploy(owner_repo=owner_repo, name=name, git_branch=git_branch, target_node=target_node)
        _deploy_timestamps.append(now)
        _audit("deploy", params, {"status": "deployed", "domain": result.get("domain")})
        return result
    except agent.DeployError as e:
        outcome = {"status": "failed", "step": e.step, "reason": e.reason}
        _audit("deploy", params, outcome)
        return outcome
    except KeyError as e:
        # node_config() raising on a bad target_node
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("deploy", params, outcome)
        return outcome


# ---------------------------------------------------------------- auth ----

def _load_token() -> str:
    return json.loads(MCP_TOKEN_PATH.read_text())["token"]


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """One shared secret, checked on every request except the platform's
    own required health/version endpoints. Deliberately NOT using the MCP
    SDK's OAuth-oriented auth provider machinery (AuthSettings/
    TokenVerifier expects a full authorization-server flow with client
    registration) -- this is a single static token shared across the
    user's own agents, so a plain ASGI middleware checking one header is
    the right amount of complexity, not an under-engineered shortcut."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        expected = f"Bearer {_load_token()}"
        if request.headers.get("authorization", "") != expected:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
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
    # The MCP SDK's DNS-rebinding protection rejects any Host header it
    # doesn't recognize by default (only localhost variants) -- a real
    # security feature, not something to disable. This server sits behind
    # the Cloudflare Tunnel at mcp.zaindroid.me, so that hostname (and
    # plain localhost, for the manual/local testing done during
    # development) needs to be explicitly trusted.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["mcp.zaindroid.me", "127.0.0.1:8081", "localhost:8081"],
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
