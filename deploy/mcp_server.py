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
import hashlib
import json
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
        "credentials yourself. If your app needs any other env var beyond APP_ENV/"
        "LOG_LEVEL, declare it in app.yaml's env: section (see get_platform_contract) -- "
        "internal secrets get generated and set for you automatically; "
        "anything tied to a real external account must be passed to deploy() "
        "via env_overrides, and analyze_deployment_requirements()'s report "
        "tells you which is which before you get there."
    ),
)


@mcp.tool()
def get_platform_contract() -> dict:
    """Returns the required app contract: files, endpoints, env vars, and
    hard rules an app must follow to be deployable on this platform. Call
    this before writing any code for a new app."""
    return {
        "required_files": {
            "app.yaml": "declares name, memory_mb, port, domains, dependencies, an "
                        "(optional) database: true flag -- see database_provisioning "
                        "below -- and an (optional) env: section for anything beyond "
                        "the auto-provided vars -- see env_vars_beyond_defaults below",
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

        try:
            parsed_app_yaml = agent.parse_app_yaml(repo_dir)
        except ValueError as e:
            return {"status": "rejected", "reason": f"app.yaml is malformed: {e}"}
        declared_env = parsed_app_yaml["env"]
        database_requested = parsed_app_yaml["database"]
        env_requirements = {
            "generated_internally": sorted(k for k, spec in declared_env.items() if "generate" in spec),
            "required_from_caller": sorted(k for k, spec in declared_env.items() if "required" in spec),
        }

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
            "env_requirements": env_requirements,
            "database_provisioned": database_requested,
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

    placement = _recommend_placement(adjusted_estimate_mb, needs_public_ip, needs_gpu=needs_gpu)
    if not placement["fits"]:
        return {
            "status": "blocked",
            "repo_kind": classification["kind"], "repo_language": classification["language"],
            "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
            "warnings": warnings,
            "env_requirements": env_requirements,
            "database_provisioned": database_requested,
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
        result = agent.deploy(owner_repo=owner_repo, name=name, git_branch=git_branch,
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


# ---------------------------------------------------------------- auth ----

# Loaded once, cached, and reloaded automatically when mcp_token.json's
# mtime changes -- so scripts/mint_token.py's rotations take effect on the
# next request with no service restart, without re-reading the file on
# every single request either. Module-level singleton, deliberately not a
# class: this process only ever has one token file.
_TOKEN_CACHE: dict = {"mtime": None, "map": {}}


def _load_token_map() -> dict:
    """{sha256(token) hex: {"name": ..., "role": "admin"|"client"}} -- see
    scripts/mint_token.py, the only thing that ever writes this file. Raw
    tokens are never stored on disk or logged, only their hash."""
    mtime = MCP_TOKEN_PATH.stat().st_mtime
    if _TOKEN_CACHE["mtime"] != mtime:
        _TOKEN_CACHE["map"] = json.loads(MCP_TOKEN_PATH.read_text())
        _TOKEN_CACHE["mtime"] = mtime
    return _TOKEN_CACHE["map"]


def _resolve_client(token: str) -> dict | None:
    """Hashes the candidate bearer token and checks it against every stored
    hash using a constant-time comparison per candidate (secrets.compare_digest)
    -- belt-and-suspenders on top of the hash-then-lookup pattern already
    being timing-safe by construction (an attacker who doesn't hold a valid
    token can't produce a matching sha256 preimage no matter how the
    comparison is timed). Returns the resolved {"name", "role"}, or None for
    anything that doesn't match a known token."""
    if not token:
        return None
    candidate_hash = hashlib.sha256(token.encode()).hexdigest()
    for stored_hash, info in _load_token_map().items():
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
    robust option, not a shortcut. Cheap (one more hash + dict scan) and
    fails closed: BearerAuthMiddleware already guarantees this header
    resolves to a real client by the time any tool body runs, so a None
    here means the SDK's request-object wiring changed under us -- refuse
    rather than audit an unknown caller as though it were legitimate."""
    headers = ctx.headers or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    client = _resolve_client(token)
    if client is None:
        raise PermissionError("could not resolve caller identity from this request's bearer token")
    return client


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
