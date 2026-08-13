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
import secrets as secrets_module
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Literal

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

# Approved requirements-analysis reports, keyed by report_id. deploy()
# requires one of these -- it's the mechanism that makes the analysis
# step mandatory rather than an optional suggestion the calling agent can
# skip. In-memory and short-lived on purpose: a report reflects the repo
# at analysis time, and shouldn't outlive the deploy attempt it was made
# for by much (the repo could change in between otherwise).
REPORT_TTL_SEC = 3600
_approved_reports: dict[str, dict] = {}  # report_id -> {"report": {...}, "expires_at": float}

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
    anything. Use this to preview before committing to a real deploy().
    Note: the memory estimate here is classify()'s flat per-language
    default -- call analyze_deployment_requirements() for a real,
    requirement-informed number; deploy() requires that step anyway."""
    repo_dir = agent.clone_repo(owner_repo)
    try:
        return agent.classify(repo_dir)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def _recommend_placement(memory_mb: int, needs_public_ip: bool) -> dict:
    """AGENTS.md's written decision procedure, as code: default to
    servingz; move to hostinger-vps only if direct public-IP ingress is
    needed or servingz doesn't have headroom."""
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
def recommend_placement(memory_mb: int, needs_public_ip: bool = False) -> dict:
    """Recommends which node to target, implementing AGENTS.md's written
    decision procedure. Pure recommendation, no side effects -- a quick
    preview tool. For an actual deploy(), use
    analyze_deployment_requirements() instead, which does this same
    placement step but from a properly-derived memory estimate rather
    than a number you supply directly."""
    return _recommend_placement(memory_mb, needs_public_ip)


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
    owner_repo: str,
    architecture: Literal["single_service", "frontend_backend_split"],
    app_kind: Literal["static", "api", "full_stack_web", "background_worker", "realtime", "other"],
    frontend_rendering: Literal["static", "server_rendered", "none"],
    framework: str,
    expected_concurrency: Literal["low", "medium", "high"],
    has_database: bool,
    has_background_jobs: bool,
    needs_websockets: bool,
    needs_persistent_storage: bool,
    needs_public_ip: bool,
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
    recommended_node, not whatever you originally guessed."""
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

        warnings = []
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

    placement = _recommend_placement(adjusted_estimate_mb, needs_public_ip)
    if not placement["fits"]:
        return {
            "status": "blocked",
            "repo_kind": classification["kind"], "repo_language": classification["language"],
            "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
            "warnings": warnings,
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
        "status": "approved",
    }
    _approved_reports[report_id] = {"report": report, "expires_at": time.time() + REPORT_TTL_SEC}
    return {**report, "report_id": report_id,
            "note": f"valid for {REPORT_TTL_SEC // 60} minutes -- pass report_id to deploy()"}


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
def deploy(owner_repo: str, name: str, report_id: str, git_branch: str = "main") -> dict:
    """Deploys a new app. THE ONLY MUTATING TOOL ON THIS SERVER -- creates
    a brand-new app; never touches, modifies, or deletes any existing one.
    Requires report_id from a prior, APPROVED analyze_deployment_requirements()
    call for this same repo -- there is no way to pass memory_mb or
    target_node directly, they come from that report, not from whatever
    you originally guessed. This is what makes the requirements analysis
    mandatory rather than an optional suggestion.

    Runs the full guardrail sequence: report validity + expiry, name-collision
    refusal, then the actual Coolify/Pages deploy (using the report's
    recommended_memory_mb and recommended_node) with best-effort rollback if a
    later step fails. Rate-limited to 5 successful deploys/hour (read-only
    tools are unlimited)."""
    params = {"owner_repo": owner_repo, "name": name, "report_id": report_id, "git_branch": git_branch}

    entry = _approved_reports.get(report_id)
    if entry is None:
        outcome = {"status": "rejected",
                   "reason": f"no approved report {report_id!r} -- call analyze_deployment_requirements() "
                              "first (or it expired; reports are valid for 1 hour)"}
        _audit("deploy", params, outcome)
        return outcome
    if time.time() > entry["expires_at"]:
        del _approved_reports[report_id]
        outcome = {"status": "rejected", "reason": f"report {report_id!r} expired -- call "
                                                     "analyze_deployment_requirements() again"}
        _audit("deploy", params, outcome)
        return outcome
    report = entry["report"]

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

    target_node = report["recommended_node"] or "servingz"  # static sites: recommended_node is None, unused by agent.deploy()'s static branch
    memory_mb_override = report["recommended_memory_mb"] if report["repo_kind"] != "static" else None

    try:
        result = agent.deploy(owner_repo=owner_repo, name=name, git_branch=git_branch,
                               target_node=target_node, memory_mb_override=memory_mb_override)
        _deploy_timestamps.append(now)
        _audit("deploy", params, {"status": "deployed", "domain": result.get("domain"), "report": report})
        return result
    except agent.DeployError as e:
        outcome = {"status": "failed", "step": e.step, "reason": e.reason}
        _audit("deploy", params, outcome)
        return outcome
    except KeyError as e:
        # node_config() raising on a bad target_node -- shouldn't happen
        # since the report's node came from _recommend_placement(), but
        # surfaced explicitly rather than swallowed if it somehow does.
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
    the right amount of complexity, not an under-engineered shortcut.

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
