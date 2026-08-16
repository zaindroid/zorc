"""servingz deploy agent — hand it a git repo, it decides how to deploy it.

Deliberately no LLM. Classification is deterministic manifest-file
detection (same order Nixpacks itself uses), resource-awareness is plain
registry.yaml arithmetic (same math as scripts/check_budget.py), and
"should this extend an existing app" follows AGENTS.md's own written
criteria rather than a model guessing. Ambiguous cases are surfaced to a
human, never silently decided.
"""
import json
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import yaml

ZORC_DIR = Path(__file__).parent.parent
REGISTRY_PATH = ZORC_DIR / "registry.yaml"
SECRETS = Path(__file__).parent / "secrets"
COOLIFY_TOKEN_PATH = SECRETS / "coolify.json"
COOLIFY_URL = "http://localhost:8000/api/v1"

# Deploy-agent-internal bookkeeping: which registry.yaml app name maps to
# which actual Coolify UUID / Pages project. Deliberately NOT part of
# registry.yaml itself -- that file is the documented, stable platform
# contract (AGENTS.md §5); this is implementation plumbing so the /apps
# dashboard can find the real resource behind a name.
RESOURCE_MAP_PATH = Path(__file__).parent / "resource_map.json"


def _load_resource_map() -> dict:
    if RESOURCE_MAP_PATH.exists():
        return json.loads(RESOURCE_MAP_PATH.read_text())
    return {}


def record_resource(name: str, *, kind: str, coolify_uuid: str | None = None,
                     domains: list[str] | None = None, coolify_postgres_uuid: str | None = None) -> None:
    """kind: 'coolify' | 'coolify-service' | 'pages'. domains is only
    needed for 'coolify-service' -- a docker-compose stack can expose
    several subdomains that don't derive from `name` the way a normal
    app's single hostname does, so delete_app needs them listed explicitly
    to clean DNS up properly. coolify_postgres_uuid is set when
    provision_dedicated_postgres() created a database for this app --
    kept alongside so a future teardown/backup step can find it without
    re-deriving the name."""
    m = _load_resource_map()
    entry = {"kind": kind, "coolify_uuid": coolify_uuid, "domains": domains or []}
    if coolify_postgres_uuid:
        entry["coolify_postgres_uuid"] = coolify_postgres_uuid
    m[name] = entry
    RESOURCE_MAP_PATH.write_text(json.dumps(m, indent=2))

# Stable platform config (not secrets) -- the "labs" project every app
# lives in regardless of which node it's placed on, its one "production"
# environment. Per-node Coolify server_uuid lives in registry.yaml's
# `nodes` section (single source of truth) -- see node_config() below.
COOLIFY_PROJECT_UUID = "p81uewe25gri9vdgtbt4kx7c"
COOLIFY_ENVIRONMENT_NAME = "production"
COOLIFY_ENVIRONMENT_UUID = "bn4ub336dd38hhe59qq04xtg"
PLATFORM_ROOT_DOMAIN = "zaindroid.me"

CLOUDFLARE_TOKEN_PATH = SECRETS / "cloudflare.json"
CLOUDFLARE_ACCOUNT_ID = "26aa85afc7a8d27691557c29f5bedbe1"
CLOUDFLARE_ZONE_ID = "f2d0bfd7ab2567c81993e5afe6ef6624"
CLOUDFLARE_TUNNEL_ID = "a8ceda0a-a10a-4924-8095-fb443319382d"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

# AGENTS.md §5 "Typical memory footprints" table, encoded.
FRAMEWORK_MEMORY_MB = {
    "static": 0,
    "node": 384,
    "python": 384,
    "go": 256,
    "dockerfile": 384,
}

BACKEND_MANIFESTS = {"package.json", "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile"}
FRONTEND_ONLY_DEPS = ("vite", "next", "react-scripts", "@angular/core", "svelte", "astro")


def _coolify_headers() -> dict:
    token = json.loads(COOLIFY_TOKEN_PATH.read_text())["token"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _cloudflare_token() -> str:
    return json.loads(CLOUDFLARE_TOKEN_PATH.read_text())["token"]


def _cloudflare_headers() -> dict:
    return {"Authorization": f"Bearer {_cloudflare_token()}", "Content-Type": "application/json"}


def create_dns_record(subdomain: str, target: str | None = None, record_type: str = "CNAME") -> None:
    """<subdomain>.zaindroid.me -> target. Defaults to a CNAME at the
    existing Cloudflare Tunnel, the pattern every servingz-routed
    subdomain uses (servingz has no public IP, so this is the only way
    in). A node with a real public IP (has_public_ip: true in registry.yaml)
    needs record_type="A" with target=that node's IP instead -- a CNAME
    cannot point at a bare IP address, and there's no tunnel involved.
    Pages custom domains need their own CNAME pointed at *.pages.dev --
    Cloudflare does NOT auto-create this even though the zone and the
    Pages project are on the same account (confirmed against the live
    API: attaching a custom domain leaves it status=pending with "CNAME
    record not set" until this exists)."""
    hostname = f"{subdomain}.{PLATFORM_ROOT_DOMAIN}"
    target = target or f"{CLOUDFLARE_TUNNEL_ID}.cfargotunnel.com"
    with httpx.Client(timeout=15) as client:
        existing = client.get(
            f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
            headers=_cloudflare_headers(), params={"name": hostname},
        )
        existing.raise_for_status()
        if existing.json()["result"]:
            return  # already exists, idempotent
        r = client.post(
            f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
            headers=_cloudflare_headers(),
            json={
                "type": record_type,
                "name": hostname,
                "content": target,
                "proxied": True,
            },
        )
        r.raise_for_status()


def deploy_to_pages(*, project_name: str, repo_dir: Path, build_command: str | None) -> str:
    """Creates the Pages project if needed, builds (if there's a build
    command), and uploads via wrangler (handles Cloudflare's content-hash
    upload protocol correctly -- not worth hand-rolling)."""
    with httpx.Client(timeout=20) as client:
        r = client.get(
            f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}",
            headers=_cloudflare_headers(),
        )
        if r.status_code == 404:
            create = client.post(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects",
                headers=_cloudflare_headers(),
                json={"name": project_name, "production_branch": "main"},
            )
            create.raise_for_status()

    publish_dir = repo_dir
    if build_command:
        subprocess.run(build_command, shell=True, cwd=repo_dir, check=True, capture_output=True, timeout=180)
        for candidate in ("dist", "build", "public", "out"):
            if (repo_dir / candidate).is_dir():
                publish_dir = repo_dir / candidate
                break

    # HOME must be writable -- the systemd service runs with
    # ProtectHome=read-only (this process has no other writes at runtime),
    # so wrangler can't create its config/cache under the real $HOME.
    # Give it a scratch one instead, in the same private-tmp namespace the
    # clone already lives in.
    wrangler_home = Path(tempfile.mkdtemp(prefix="wrangler-home-"))
    env = {"CLOUDFLARE_API_TOKEN": _cloudflare_token(), "CLOUDFLARE_ACCOUNT_ID": CLOUDFLARE_ACCOUNT_ID,
           "PATH": "/usr/bin:/usr/local/bin", "HOME": str(wrangler_home)}
    result = subprocess.run(
        ["wrangler", "pages", "deploy", str(publish_dir), "--project-name", project_name,
         "--branch", "main", "--commit-dirty=true"],
        env=env, cwd=str(wrangler_home), check=True, capture_output=True, text=True, timeout=180,
    )
    match = re.search(r"https://[a-z0-9.-]+\.pages\.dev", result.stdout)
    return match.group(0) if match else f"https://{project_name}.pages.dev"


def add_pages_custom_domain(project_name: str, domain: str) -> None:
    with httpx.Client(timeout=15) as client:
        r = client.post(
            f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}/domains",
            headers=_cloudflare_headers(), json={"name": domain},
        )
        if r.status_code == 200:
            return
        # Cloudflare returns 400 (not 409) for "already attached" -- code
        # 8000018 specifically, confirmed against the live API.
        already_attached = any(e.get("code") == 8000018 for e in r.json().get("errors", []))
        if r.status_code == 400 and already_attached:
            return
        r.raise_for_status()


def load_registry() -> dict:
    return yaml.safe_load(REGISTRY_PATH.read_text())


def node_config(node_name: str) -> dict:
    """Raises KeyError with the valid options listed if node_name isn't a
    real node -- callers should let that surface rather than silently
    falling back to servingz, since a typo'd target is exactly the kind of
    mistake check_budget.py exists to catch before it reaches deploy()."""
    reg = load_registry()
    nodes = reg["nodes"]
    if node_name not in nodes:
        raise KeyError(f"{node_name!r} is not a known node (valid: {sorted(nodes)})")
    return nodes[node_name]


# This file always runs on servingz. Reaching any other node for a live
# resource check needs its own SSH access -- generated specifically for
# this (deploy/secrets/hostinger_vps_deploy_key), never a personal key.
LOCAL_NODE = "servingz"
REMOTE_DEPLOY_KEY = SECRETS / "hostinger_vps_deploy_key"


def _remote_host_memory_mb(tailscale_ip: str) -> tuple[float, float]:
    """Same as _host_memory_mb() (defined further down) but for a node
    this process isn't running on, over SSH via the dedicated deploy key."""
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(REMOTE_DEPLOY_KEY), f"root@{tailscale_ip}", "cat", "/proc/meminfo"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    info = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            info[parts[0]] = int(parts[1]) / 1024  # kB -> MB
    return info.get("MemTotal:", 0.0), info.get("MemAvailable:", 0.0)


def live_headroom_mb(node_name: str) -> float:
    """Real, right-now available memory on the target node -- independent
    of registry.yaml's static budget, so it catches drift from something
    already eating memory *right now* that the static number alone can't
    see. A second, live check used immediately before create_coolify_app,
    alongside (never instead of) the static budget_headroom_mb() check."""
    node = node_config(node_name)
    if node_name == LOCAL_NODE:
        _, available_mb = _host_memory_mb()
    else:
        tailscale_ip = node.get("tailscale_ip")
        if not tailscale_ip:
            raise RuntimeError(f"{node_name!r} has no tailscale_ip in registry.yaml -- cannot live-check it")
        _, available_mb = _remote_host_memory_mb(tailscale_ip)
    return available_mb


def _probe_hardware_over_ssh(tailscale_ip: str, ssh_key: Path, user: str = "root") -> dict:
    """Runs the same arch/cpu/ram/power/accelerator detection
    monitoring/checks.py's gather_node_info() uses locally, but over SSH
    against a remote host, using an explicit key -- the shared primitive
    both remote_node_probe() (an already-registered node, always
    REMOTE_DEPLOY_KEY as root) and propose_node() (a not-yet-registered
    candidate, whatever key and user nodes/candidates.yaml says for it --
    a shared/partial machine may only grant a non-root user) build on.
    None of this probe's commands (uname, /proc, /sys reads, nvidia-smi)
    need root, so a limited user works fine here.

    Ships the ACTUAL function source from checks.py (via inspect.getsource)
    rather than a hand-copied duplicate, so a future fix to arch/GPU/power
    detection there is automatically what gets probed remotely too -- one
    place to fix, not two. Needs nothing installed on the target beyond a
    python3 interpreter, which every node already has."""
    import inspect
    import sys as _sys
    monitoring_dir = str(ZORC_DIR / "monitoring")
    if monitoring_dir not in _sys.path:
        _sys.path.insert(0, monitoring_dir)
    import checks  # deploy/ and monitoring/ import each other; deferred so this only pays the circular-import cost when actually called

    funcs_src = "\n\n".join(
        inspect.getsource(getattr(checks, fn_name))
        for fn_name in ("_run", "_detect_accelerator", "_detect_power_source", "_detect_cpu")
    )
    probe_script = f'''
import json, os, subprocess, platform
from pathlib import Path

{funcs_src}

def _meminfo_total_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    return None

print(json.dumps({{
    "arch": platform.machine(),
    "accelerator": _detect_accelerator(),
    "cpu": _detect_cpu(),
    "ram_mb": _meminfo_total_mb(),
    "power": _detect_power_source(),
}}))
'''
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(ssh_key), f"{user}@{tailscale_ip}", "python3", "-"],
        input=probe_script, capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"remote probe on {tailscale_ip!r} failed: {(proc.stderr or proc.stdout)[-500:]}")
    return json.loads(proc.stdout)


def _ssh_run(tailscale_ip: str, ssh_key: Path, remote_cmd: list[str], user: str = "root") -> tuple[int, str, str]:
    """One-off remote command, same connection conventions as
    _probe_hardware_over_ssh -- used for the small existing-software checks
    (docker, coolify) propose_node() runs alongside the hardware probe."""
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(ssh_key), f"{user}@{tailscale_ip}", *remote_cmd],
        capture_output=True, text=True, timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def remote_node_probe(node_name: str) -> dict:
    """_probe_hardware_over_ssh() for an already-registered node -- looks
    up its tailscale_ip from registry.yaml and always uses
    REMOTE_DEPLOY_KEY, the one key every registered node accepts. Shared by
    two callers: watchdog.py's periodic refresh of non-local nodes'
    nodes/<name>.yaml, and (indirectly, via _probe_hardware_over_ssh)
    propose_node()'s one-off pre-registration capability report."""
    node = node_config(node_name)
    tailscale_ip = node.get("tailscale_ip")
    if not tailscale_ip:
        raise RuntimeError(f"{node_name!r} has no tailscale_ip in registry.yaml -- cannot probe it remotely")
    return _probe_hardware_over_ssh(tailscale_ip, REMOTE_DEPLOY_KEY)


CANDIDATES_PATH = ZORC_DIR / "nodes" / "candidates.yaml"


def load_candidates() -> list[dict]:
    if not CANDIDATES_PATH.exists():
        return []
    return (yaml.safe_load(CANDIDATES_PATH.read_text()) or {}).get("candidates", [])


def propose_node(hostname: str) -> dict:
    """Read-only capability report for a node NOT yet in registry.yaml --
    the guarded, human-gated alternative to a one-shot autonomous
    "register_node()" (deliberately never built -- node registration is a
    new trust boundary and stays human-confirmed, same principle already
    applied to DNS record creation elsewhere in this MCP server).

    Refuses anything not already listed in nodes/candidates.yaml, a small
    human-maintained allowlist -- being listed there only authorizes
    read-only inspection, it is NOT registration, and there is no way to
    reach an arbitrary caller-supplied host through this function. Never
    stages anything, never installs anything, never writes to
    registry.yaml. Actual onboarding stays a human-reviewed registry.yaml
    edit, guided by this report."""
    match = next((c for c in load_candidates() if c["hostname"] == hostname), None)
    if not match:
        known = [c["hostname"] for c in load_candidates()]
        return {
            "status": "refused",
            "reason": (
                f"{hostname!r} is not in nodes/candidates.yaml -- propose_node() only inspects "
                f"pre-approved candidates, never an arbitrary host a caller names. "
                f"Known candidates: {known or '(none yet)'}. A human needs to add it there first."
            ),
        }

    tailscale_ip = match["tailscale_ip"]
    ssh_key = ZORC_DIR / match["ssh_key"]
    user = match.get("user", "root")

    try:
        hardware = _probe_hardware_over_ssh(tailscale_ip, ssh_key, user)
    except Exception as e:
        return {"status": "unreachable", "hostname": hostname, "reason": str(e)}

    docker_rc, _, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "--version"], user)
    coolify_rc, _, _ = _ssh_run(tailscale_ip, ssh_key, ["test", "-d", "/data/coolify"], user)
    has_docker = docker_rc == 0
    has_coolify = coolify_rc == 0
    coolify_check_note = (
        None if user == "root" else
        "checked as a non-root user -- a false negative is possible if /data/coolify exists "
        "but isn't readable by this account"
    )

    if has_docker and has_coolify:
        suggested_backend = "coolify"
    elif has_docker:
        suggested_backend = "zorc-agent"  # not yet implemented -- see registry.yaml's node schema comment
    else:
        suggested_backend = None  # neither present; needs a human decision, not a guess

    return {
        "status": "ready_for_review",
        "hostname": hostname,
        "hardware": hardware,
        "has_docker": has_docker,
        "has_coolify": has_coolify,
        "coolify_check_note": coolify_check_note,
        "suggested_backend": suggested_backend,
        "suggested_is_control_plane": False,  # never auto-suggest true -- control-plane trust is always a deliberate human call
        "note": (
            "Read-only report. Nothing was staged or installed on this host. To actually onboard: run "
            "the appropriate process yourself (bootstrap/*.sh for a new control-plane-capable node, or "
            "Coolify's own server-add flow for a lighter worker node -- see hostinger-vps's onboarding "
            "as precedent), then add this node to registry.yaml's `nodes:` section yourself (or ask for "
            "that specific, reviewed edit)."
        ),
    }


def budget_headroom_mb(node_name: str = "servingz") -> float:
    reg = load_registry()
    node = node_config(node_name)
    ceiling = node["usable_mb"] * node["max_utilisation"]
    allocated = sum(
        a.get("memory_mb", 0) for a in reg.get("apps", []) if a.get("target") == node_name
    )
    return ceiling - allocated


def name_taken(name: str) -> bool:
    reg = load_registry()
    return any(a["name"] == name for a in reg.get("apps", []))


def clone_repo(owner_repo: str) -> Path:
    """owner_repo like 'zaindroid/hello-app'. Uses gh CLI (already
    authenticated on this host) so it works for private repos too, not
    just public ones."""
    workdir = Path(tempfile.mkdtemp(prefix="deploy-"))
    subprocess.run(
        ["gh", "repo", "clone", owner_repo, str(workdir), "--", "--depth", "1"],
        check=True, capture_output=True, timeout=60,
    )
    return workdir


def classify(repo_dir: Path) -> dict:
    """No LLM: deterministic detection, same order Nixpacks uses internally.
    Returns {kind: static|app|unknown, language, memory_mb, reason, ...}."""
    files = {p.name for p in repo_dir.iterdir() if p.is_file()}
    has_backend_manifest = bool(BACKEND_MANIFESTS & files)
    has_dockerfile = "Dockerfile" in files
    has_index_html = "index.html" in files

    if has_index_html and not has_backend_manifest and not has_dockerfile:
        return {"kind": "static", "language": None, "memory_mb": 0,
                "reason": "index.html with no backend manifest — static site"}

    if "package.json" in files:
        try:
            pkg = json.loads((repo_dir / "package.json").read_text())
        except (json.JSONDecodeError, OSError):
            pkg = {}
        scripts = pkg.get("scripts", {})
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        looks_frontend_only = any(fw in deps for fw in FRONTEND_ONLY_DEPS) and "start" not in scripts
        if looks_frontend_only and "build" in scripts:
            return {"kind": "static", "language": "node", "memory_mb": 0,
                    "build_command": scripts["build"],
                    "reason": "frontend build tool present, no start script — static site"}

        result = {"kind": "app", "language": "node", "memory_mb": FRAMEWORK_MEMORY_MB["node"],
                  "reason": "package.json with a server script"}

        # Real bug found live (blylinks-crm): Nixpacks' own build-pack
        # auto-detection can independently decide a repo is a static site
        # even when classify() correctly says "app" -- specifically when a
        # frontend build tool (vite.config.js etc) sits alongside a real
        # server script. Nixpacks doesn't know what classify() knows here;
        # left to guess, it built and served the static SPA shell via Caddy
        # instead of running the actual server (confirmed live: /health
        # returned the SPA's index.html, no X-Powered-By: Express header).
        # Passing explicit commands removes the ambiguity Nixpacks was
        # guessing on, instead of leaving deploy() to discover a broken
        # deploy after the fact.
        has_frontend_tooling = any(fw in deps for fw in FRONTEND_ONLY_DEPS)
        if has_frontend_tooling and "start" in scripts:
            result["start_command"] = "npm start"
            if "build" in scripts:
                result["build_command"] = "npm run build"
            result["reason"] += (" (frontend build tool + server script both present -- explicit "
                                  "build/start commands set to avoid Nixpacks static-site misdetection)")
        return result

    if "requirements.txt" in files or "pyproject.toml" in files:
        return {"kind": "app", "language": "python", "memory_mb": FRAMEWORK_MEMORY_MB["python"],
                "reason": "python manifest present"}

    if "go.mod" in files:
        return {"kind": "app", "language": "go", "memory_mb": FRAMEWORK_MEMORY_MB["go"],
                "reason": "go.mod present"}

    if has_dockerfile:
        return {"kind": "app", "language": "dockerfile", "memory_mb": FRAMEWORK_MEMORY_MB["dockerfile"],
                "reason": "Dockerfile present, no other manifest recognized"}

    return {"kind": "unknown", "language": None, "memory_mb": None,
            "reason": "no recognizable manifest — needs a human decision"}


# Only generation strategy supported for now -- a 256-bit random hex
# string, the right shape for JWT/session/signing secrets. Not meant for
# values tied to an external account (API keys, third-party credentials)
# -- those can't be conjured locally and must come through as
# "required" instead, supplied by whoever calls deploy().
_ENV_GENERATE_STRATEGIES = {"hex": lambda: secrets.token_hex(32)}


def parse_app_yaml(repo_dir: Path) -> dict:
    """Reads app.yaml's optional `env:` section and `database:` flag, if
    present. Format:

        database: true          # provisions a dedicated Postgres instance
                                 # for this app -- zorc creates it, creates
                                 # a scoped role+database on it, and sets
                                 # DATABASE_URL itself (see
                                 # provision_dedicated_postgres()). The
                                 # instance's own superuser credentials are
                                 # never exposed to the app or the caller.
        env:
          JWT_SECRET:
            generate: hex        # zorc generates a random value itself,
                                  # sets it via Coolify before first boot --
                                  # the calling agent/human never sees it.
          STRIPE_SECRET_KEY:
            required: true       # tied to an external account -- must be
                                  # supplied via deploy()'s env_overrides,
                                  # deploy() fails loudly (before creating
                                  # anything) if it's missing.

    Missing app.yaml, or a missing/empty env: section, or no database:
    flag, is not an error -- most apps only need the platform-provided
    APP_ENV/LOG_LEVEL and declare nothing here. This function was written
    after blylinks-crm's DATABASE_URL was discovered to be entirely
    unimplemented despite get_platform_contract() promising it -- database:
    true is what actually makes that promise real."""
    app_yaml_path = repo_dir / "app.yaml"
    if not app_yaml_path.exists():
        return {"env": {}, "database": False}
    try:
        parsed = yaml.safe_load(app_yaml_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"app.yaml is not valid YAML: {e}")
    env = parsed.get("env") or {}
    for key, spec in env.items():
        if not isinstance(spec, dict) or ("generate" not in spec and "required" not in spec):
            raise ValueError(
                f"app.yaml env.{key} must be either {{generate: hex}} or {{required: true}}, got {spec!r}"
            )
        if "generate" in spec and spec["generate"] not in _ENV_GENERATE_STRATEGIES:
            raise ValueError(
                f"app.yaml env.{key}.generate={spec['generate']!r} is not supported "
                f"(supported: {sorted(_ENV_GENERATE_STRATEGIES)})"
            )
    database = parsed.get("database", False)
    if not isinstance(database, bool):
        raise ValueError(f"app.yaml database must be true or false, got {database!r}")
    return {"env": env, "database": database}


def resolve_env_vars(repo_dir: Path, env_overrides: dict[str, str] | None) -> tuple[dict[str, str], list[str]]:
    """Cross-references app.yaml's env: section against env_overrides
    (values the caller already has, e.g. a real third-party API key).
    Returns (vars_to_set, missing_required) -- vars_to_set is the full set
    of {key: value} to push to Coolify (generated values included, caller
    never has to see or handle those), missing_required lists any
    required-but-unsupplied keys so deploy() can fail before creating
    anything rather than deploying a container guaranteed to crash-loop."""
    env_overrides = env_overrides or {}
    declared = parse_app_yaml(repo_dir)["env"]
    vars_to_set: dict[str, str] = {}
    missing_required: list[str] = []
    for key, spec in declared.items():
        if "generate" in spec:
            vars_to_set[key] = _ENV_GENERATE_STRATEGIES[spec["generate"]]()
        elif key in env_overrides:
            vars_to_set[key] = env_overrides[key]
        else:
            missing_required.append(key)
    return vars_to_set, missing_required


# Extra memory to add to an app's own registry.yaml memory_mb when it gets
# a dedicated Postgres via provision_dedicated_postgres(), so the existing
# per-node budget math (budget_headroom_mb, which only sums apps: memory_mb)
# accounts for it without needing a separate tracked-infrastructure entry.
# Based on a real dedicated instance's observed baseline (~43MB on
# blylinks-crm-postgres) padded for connection/buffer growth under load.
DEDICATED_POSTGRES_MEMORY_MB = 150


def provision_dedicated_postgres(app_name: str, target_node: str) -> tuple[str, str]:
    """Creates a new, single-app-dedicated Postgres instance on target_node
    via Coolify, waits (bounded, 90s) for it to report healthy, creates a
    role+database scoped to app_name, and returns (coolify_postgres_uuid,
    database_url). The instance's own superuser credentials never leave
    this function -- not returned, not logged, not passed to the caller --
    only the freshly-generated app-scoped role's connection string is.

    Built after discovering DATABASE_URL provisioning was entirely
    unimplemented despite being a documented part of the platform contract
    -- blylinks-crm needed this done by hand once; this is that process
    turned into reusable, repeatable code."""
    node = node_config(target_node)
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/databases/postgresql", headers=_coolify_headers(), json={
            "project_uuid": COOLIFY_PROJECT_UUID,
            "server_uuid": node["server_uuid"],
            "environment_name": COOLIFY_ENVIRONMENT_NAME,
            "name": f"{app_name}-postgres",
            "image": "postgres:18-alpine",
        })
        r.raise_for_status()
        db_uuid = r.json()["uuid"]

        r = client.get(f"{COOLIFY_URL}/databases/{db_uuid}/start", headers=_coolify_headers())
        r.raise_for_status()

    deadline = time.time() + 90
    healthy = False
    while time.time() < deadline:
        with httpx.Client(timeout=15) as client:
            r = client.get(f"{COOLIFY_URL}/databases/{db_uuid}", headers=_coolify_headers())
            r.raise_for_status()
            if r.json().get("status") == "running:healthy":
                healthy = True
                break
        time.sleep(5)
    if not healthy:
        raise RuntimeError(f"dedicated postgres {db_uuid} for {app_name!r} did not become healthy within 90s")

    # Coolify's internal Docker-network hostname for a database resource is
    # its own UUID -- confirmed live (matches every other database on this
    # platform, e.g. the shared servingz instance).
    container_name = db_uuid
    db_role = re.sub(r"[^a-z0-9_]", "_", app_name.lower())
    db_password = secrets.token_hex(24)
    sql = f"CREATE ROLE {db_role} WITH LOGIN PASSWORD '{db_password}'; CREATE DATABASE {db_role} OWNER {db_role};"

    if target_node == LOCAL_NODE:
        cmd = ["docker", "exec", "-i", container_name, "psql", "-U", "postgres"]
    else:
        tailscale_ip = node.get("tailscale_ip")
        if not tailscale_ip:
            raise RuntimeError(f"{target_node!r} has no tailscale_ip in registry.yaml -- cannot reach it")
        cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
               "-i", str(REMOTE_DEPLOY_KEY), f"root@{tailscale_ip}",
               "docker", "exec", "-i", container_name, "psql", "-U", "postgres"]

    proc = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create role/database for {app_name!r}: {proc.stderr[-500:]}")

    database_url = f"postgres://{db_role}:{db_password}@{container_name}:5432/{db_role}"
    return db_uuid, database_url


def check_deploy_budget(name: str, memory_mb: int, node_name: str = "servingz") -> tuple[bool, str]:
    if name_taken(name):
        return False, f"'{name}' is already registered in registry.yaml"
    headroom = budget_headroom_mb(node_name)
    if memory_mb > headroom:
        return False, (f"needs {memory_mb} MB but only {headroom:.0f} MB of headroom left on "
                        f"{node_name} — does not fit without retiring something or using the other node")
    return True, f"fits on {node_name} — {headroom:.0f} MB headroom, {memory_mb} MB requested"


def build_pack_for(language: str) -> str:
    return "dockerfile" if language == "dockerfile" else "nixpacks"


def create_coolify_app(*, name: str, git_repository: str, git_branch: str,
                        build_pack: str, memory_mb: int, domain: str, server_uuid: str,
                        instant_deploy: bool = True, install_command: str | None = None,
                        build_command: str | None = None, start_command: str | None = None) -> dict:
    """instant_deploy=False when the app declares env vars that must be set
    (see resolve_env_vars) -- Coolify's default behaviour builds and starts
    the container immediately on creation, before there's any chance to
    push those vars in, which is exactly how blylinks-crm crash-looped on
    JWT_SECRET. False here means "create the resource, don't start it yet";
    the caller is responsible for setting env vars and then calling
    trigger_coolify_deploy() itself once they're in place.

    install/build/start_command, when given (see classify()'s frontend-
    tooling-plus-server-script detection), override Nixpacks' own
    auto-detection -- left blank, Nixpacks can independently misdetect an
    app classify() already correctly identified as a real server (see
    create_coolify_app's docstring history / blylinks-crm)."""
    payload = {
        "project_uuid": COOLIFY_PROJECT_UUID,
        "server_uuid": server_uuid,
        "environment_name": COOLIFY_ENVIRONMENT_NAME,
        "git_repository": git_repository,
        "git_branch": git_branch,
        "build_pack": build_pack,
        "name": name,
        "domains": f"https://{domain}",
        "ports_exposes": "8080",  # AGENTS.md's app contract convention
        "limits_memory": f"{memory_mb}m",
        "is_auto_deploy_enabled": True,
        "is_force_https_enabled": True,
        "health_check_enabled": True,
        "health_check_path": "/health",
        "instant_deploy": instant_deploy,
    }
    if install_command:
        payload["install_command"] = install_command
    if build_command:
        payload["build_command"] = build_command
    if start_command:
        payload["start_command"] = start_command
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/applications/public", headers=_coolify_headers(), json=payload)
        r.raise_for_status()
        return r.json()


def set_coolify_env_vars(coolify_uuid: str, vars_to_set: dict[str, str]) -> None:
    """Pushes each {key: value} to Coolify as a runtime env var on the
    given application. Called after create_coolify_app (instant_deploy=
    False) and before trigger_coolify_deploy, so the container's first
    real start already has everything it needs."""
    with httpx.Client(timeout=30) as client:
        for key, value in vars_to_set.items():
            r = client.post(
                f"{COOLIFY_URL}/applications/{coolify_uuid}/envs",
                headers=_coolify_headers(),
                json={"key": key, "value": value, "is_preview": False},
            )
            r.raise_for_status()


def trigger_coolify_deploy(coolify_uuid: str) -> None:
    """Explicitly starts the first real build+deploy -- the counterpart to
    create_coolify_app(instant_deploy=False). Coolify's own webhook-style
    deploy-trigger endpoint, keyed by application uuid."""
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{COOLIFY_URL}/deploy", headers=_coolify_headers(), params={"uuid": coolify_uuid})
        r.raise_for_status()


def register_app(*, name: str, memory_mb: int, subdomain: str, repo: str, target: str = "servingz",
                  database: bool = False, redis: bool = False, critical: bool = False) -> None:
    """Appends a new entry to registry.yaml and commits it — mirrors what a
    human did by hand for hello-app. target must be "pages" for static
    sites (memory_mb: 0) or a real node name from registry.yaml's `nodes`
    section for real apps -- check_budget.py's own sanity rule rejects
    node+0 or pages+nonzero combinations, and an unknown target name."""
    if name_taken(name):
        return  # idempotent -- a repeated deploy of the same app shouldn't double-register it
    text = REGISTRY_PATH.read_text()
    entry = f"""
  - name: {name}
    target: {target}
    memory_mb: {memory_mb}
    subdomain: {subdomain}
    database: {"true" if database else "null"}
    redis_db: null
    storage_prefix: null
    repo: "{repo}"
    critical: {"true" if critical else "false"}
    depends_on: []
"""
    marker = "# Applications. Add new entries at the end. Keep alphabetical within groups.\n# ---------------------------------------------------------------------------\napps:"
    if marker not in text:
        raise RuntimeError("registry.yaml marker not found — format changed, update register_app()")
    text = text.replace(marker, marker + entry, 1)
    REGISTRY_PATH.write_text(text)


def add_tunnel_route(hostname: str) -> None:
    """Every Coolify-managed app routes through the same Traefik hop --
    Traefik dispatches to the right container by Host() header, using the
    domains we set on the Coolify app resource. Same pattern as every
    existing app entry (hello.zaindroid.me, hello-staging.zaindroid.me).

    Real bug found live: the idempotency check used to read the REPO copy
    of cloudflared's config, not the live one at /etc/cloudflared. A first
    attempt that wrote the repo file successfully but then failed at the
    `sudo cp` (or the reload) left the repo "ahead" of what's actually
    served. A retry saw the hostname already in the repo file, treated
    that as "already routed," and returned without ever copying to
    /etc/cloudflared or reloading cloudflared -- reported ok:true while
    the tunnel kept serving its old rules with no route for the new
    hostname at all (blylinks-crm: container healthy, but every request
    404'd at Cloudflare's edge before reaching it). Fix: check the LIVE
    config for the idempotency test (that's the thing that actually
    determines whether traffic gets routed), and always sync+reload if a
    prior partial failure left the repo file ahead of it -- so a retry
    after a failed cp/reload actually finishes the job instead of
    silently no-op'ing."""
    config_path = Path("/etc/cloudflared/config.yml")
    repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"

    live_cfg = yaml.safe_load(config_path.read_text())
    if any(r.get("hostname") == hostname for r in live_cfg["ingress"]):
        return  # genuinely already serving this route -- nothing to do

    cfg = yaml.safe_load(repo_config_path.read_text())
    if not any(r.get("hostname") == hostname for r in cfg["ingress"]):
        new_rule = {
            "hostname": hostname,
            "service": "https://localhost:443",
            "originRequest": {"noTLSVerify": True},
        }
        cfg["ingress"].insert(-1, new_rule)  # keep the catch-all 404 rule last
        repo_config_path.write_text(yaml.dump(cfg, sort_keys=False))
    # Always push to live + reload from here, even if the repo file already
    # had the rule (a prior run got this far before failing) -- that's
    # exactly the case that silently broke before.
    subprocess.run(["sudo", "cp", str(repo_config_path), str(config_path)], check=True)
    subprocess.run(["sudo", "systemctl", "reload-or-restart", "cloudflared"], check=True)


def git_commit_and_push(message: str) -> None:
    subprocess.run(["git", "-C", str(ZORC_DIR), "add", "registry.yaml", "cloudflared/config.yml"], check=True)
    subprocess.run(["git", "-C", str(ZORC_DIR), "commit", "-m", message], check=True)
    subprocess.run(["git", "-C", str(ZORC_DIR), "push", "origin", "main"], check=True)


class DeployError(Exception):
    def __init__(self, step: str, reason: str):
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


def deploy(*, owner_repo: str, name: str, git_branch: str = "main", target_node: str = "servingz",
           memory_mb_override: int | None = None, env_overrides: dict[str, str] | None = None) -> dict:
    """Full pipeline: clone -> classify -> budget check -> live resource
    check -> either Cloudflare Pages (static) or Coolify (real app), DNS +
    registration either way. Raises DeployError with the exact step and
    reason on any failure. For the Coolify path, a failure after
    create_coolify_app succeeds triggers a best-effort rollback of that
    Coolify app (see the try/except below) -- DNS records and tunnel
    routes created before the failure are NOT rolled back (a stale DNS
    record pointing at nothing is a cosmetic issue, not a resource one;
    the Coolify app itself is what actually consumes node memory, so
    that's what gets cleaned up). The static-site path has no rollback --
    it fails much earlier in practice and Pages projects cost no node
    memory either way.

    target_node picks which node from registry.yaml's `nodes` section a
    real app (not a static site -- those always go to Cloudflare Pages
    regardless of target_node) gets deployed to. Ignored for static sites.
    Raises KeyError immediately (via node_config) if target_node isn't a
    real node -- fail before cloning anything, not after.

    memory_mb_override, if given, replaces classify()'s flat per-language
    default (384MB for node/python/dockerfile, 256 for go) for the budget
    check and the actual Coolify memory limit. classify() alone can't
    know an app's real requirements -- how much traffic it expects,
    whether it runs background jobs, how many/how heavy its dependencies
    are -- it only knows which manifest file exists. This is the hook
    callers with better information (e.g. mcp_server.py's requirements
    analysis) use to supply an informed number instead. Ignored for
    static sites, which always cost zero node memory regardless.

    env_overrides supplies values for any app.yaml env: entries marked
    required: true (e.g. a real third-party API key) -- see
    resolve_env_vars(). Entries marked generate: hex are handled
    internally and never need (or use) anything from env_overrides. If
    app.yaml declares a required var with no matching entry here, deploy()
    fails at the resolve_env_vars step, before create_coolify_app runs --
    no half-built container left crash-looping for a human to clean up.
    Ignored for static sites (no runtime env vars to set)."""
    log = []
    node = node_config(target_node)  # raises KeyError immediately on a bad name

    def step(name_, fn, *a, **kw):
        try:
            result = fn(*a, **kw)
            log.append({"step": name_, "ok": True})
            return result
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or str(e))
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            detail = detail.strip()[-1500:]  # tail -- most useful part of a CLI error is usually the end
            log.append({"step": name_, "ok": False, "error": detail})
            raise DeployError(name_, detail) from e
        except Exception as e:
            log.append({"step": name_, "ok": False, "error": str(e)})
            raise DeployError(name_, str(e)) from e

    repo_dir = step("clone", clone_repo, owner_repo)
    classification = step("classify", classify, repo_dir)

    if classification["kind"] == "unknown":
        raise DeployError("classify", classification["reason"] + " — cannot proceed automatically")

    # Determined here (not deferred into the app.yaml env: parse further
    # down) specifically so the extra memory a dedicated Postgres needs is
    # already folded into memory_mb before budget_check runs -- otherwise
    # the static budget check would pass on a number the deploy would then
    # exceed once provision_dedicated_postgres() actually runs.
    needs_database = classification["kind"] != "static" and parse_app_yaml(repo_dir).get("database", False)

    memory_mb = memory_mb_override if memory_mb_override is not None else classification["memory_mb"]
    if needs_database:
        memory_mb += DEDICATED_POSTGRES_MEMORY_MB

    ok, reason = step("budget_check", check_deploy_budget, name, memory_mb, target_node)
    if not ok:
        raise DeployError("budget_check", reason)

    domain = f"{name}.{PLATFORM_ROOT_DOMAIN}"

    if classification["kind"] == "static":
        pages_url = step("deploy_to_pages", deploy_to_pages, project_name=name,
                          repo_dir=repo_dir, build_command=classification.get("build_command"))
        step("add_pages_custom_domain", add_pages_custom_domain, name, domain)
        step("create_dns_record", create_dns_record, name, f"{name}.pages.dev")
        step("register_app", register_app, name=name, memory_mb=0, subdomain=name,
             repo=f"github.com/{owner_repo}", target="pages")
        step("record_resource", record_resource, name, kind="pages")
        step("commit_and_push", git_commit_and_push, f"registry: add {name} (static, via deploy agent)")
        return {
            "log": log, "classification": classification, "status": "deployed",
            "domain": domain, "pages_url": pages_url,
            "message": f"{name} deployed to Cloudflare Pages (zero node memory used), "
                       f"custom domain https://{domain} attached, registered in registry.yaml.",
        }

    build_pack = build_pack_for(classification["language"])

    env_vars_to_set, missing_required_env = step("resolve_env_vars", resolve_env_vars, repo_dir, env_overrides)
    if missing_required_env:
        raise DeployError(
            "resolve_env_vars",
            f"app.yaml requires {missing_required_env} but no value was supplied for "
            f"{'it' if len(missing_required_env) == 1 else 'them'} -- pass via deploy()'s env_overrides "
            "(these are tied to an external account/service, zorc cannot generate them itself)",
        )

    postgres_uuid = None
    if needs_database:
        postgres_uuid, database_url = step("provision_database", provision_dedicated_postgres, name, target_node)
        env_vars_to_set["DATABASE_URL"] = database_url

    # Live re-check immediately before creating anything -- catches drift
    # from something already eating memory on the target node right now,
    # which the static budget_headroom_mb() check above can't see (it only
    # knows what registry.yaml *declares* apps use, not what they actually
    # use this second). Belt-and-suspenders, not a replacement for it.
    def _check_live_headroom():
        live = live_headroom_mb(target_node)
        if memory_mb > live:
            raise RuntimeError(
                f"needs {memory_mb} MB but only {live:.0f} MB is actually free on "
                f"{target_node} right now (static budget said this would fit -- "
                f"something is using more memory than registry.yaml accounts for)"
            )
    step("live_resource_check", _check_live_headroom)

    coolify_result = step(
        "create_coolify_app", create_coolify_app,
        name=name, git_repository=f"https://github.com/{owner_repo}",
        git_branch=git_branch, build_pack=build_pack, memory_mb=memory_mb, domain=domain,
        server_uuid=node["server_uuid"], instant_deploy=not env_vars_to_set,
        build_command=classification.get("build_command"),
        start_command=classification.get("start_command"),
    )

    try:
        if env_vars_to_set:
            # instant_deploy was False above specifically so this can run
            # first -- the container's actual first start happens at
            # trigger_coolify_deploy, by which point every declared env var
            # (generated or caller-supplied) is already set.
            step("set_env_vars", set_coolify_env_vars, coolify_result["uuid"], env_vars_to_set)
            step("trigger_coolify_deploy", trigger_coolify_deploy, coolify_result["uuid"])

        # A node with a real public IP is reached directly (A record at its IP,
        # Coolify's own Traefik terminates TLS there) -- no Cloudflare Tunnel
        # involved, unlike servingz which has no public IP of its own.
        if node.get("has_public_ip"):
            step("create_dns_record", create_dns_record, name, target=node["ip"], record_type="A")
        else:
            step("create_dns_record", create_dns_record, name)
            step("add_tunnel_route", add_tunnel_route, domain)

        step("register_app", register_app, name=name, memory_mb=memory_mb, subdomain=name,
             repo=f"github.com/{owner_repo}", target=target_node, database=needs_database)
        step("record_resource", record_resource, name, kind="coolify", coolify_uuid=coolify_result.get("uuid"),
             coolify_postgres_uuid=postgres_uuid)
        step("commit_and_push", git_commit_and_push, f"registry: add {name} (deployed via deploy agent)")
    except DeployError:
        # create_coolify_app already succeeded above -- a real app now
        # exists on the target node consuming its declared memory. Leaving
        # it in place on a later-step failure would mean a half-registered
        # app silently sitting there forever. This only matters once
        # external callers (the MCP server) can trigger deploy()
        # unattended; a human running this by hand used to just clean up
        # manually, which is what the old docstring here described.
        coolify_uuid = coolify_result.get("uuid")
        try:
            if coolify_uuid:
                with httpx.Client(timeout=30) as client:
                    r = client.delete(f"{COOLIFY_URL}/applications/{coolify_uuid}", headers=_coolify_headers())
                    if r.status_code not in (200, 404):
                        log.append({"step": "rollback_coolify_app", "ok": False,
                                    "error": f"cleanup returned HTTP {r.status_code}"})
                    else:
                        log.append({"step": "rollback_coolify_app", "ok": True})
        except Exception as cleanup_err:
            log.append({"step": "rollback_coolify_app", "ok": False, "error": str(cleanup_err)})
        if postgres_uuid:
            try:
                with httpx.Client(timeout=30) as client:
                    r = client.delete(f"{COOLIFY_URL}/databases/{postgres_uuid}", headers=_coolify_headers())
                    if r.status_code not in (200, 404):
                        log.append({"step": "rollback_postgres", "ok": False,
                                    "error": f"cleanup returned HTTP {r.status_code}"})
                    else:
                        log.append({"step": "rollback_postgres", "ok": True})
            except Exception as cleanup_err:
                log.append({"step": "rollback_postgres", "ok": False, "error": str(cleanup_err)})
        raise

    return {
        "log": log,
        "classification": classification,
        "status": "deployed",
        "domain": domain,
        "target_node": target_node,
        "coolify_uuid": coolify_result.get("uuid"),
        "coolify_postgres_uuid": postgres_uuid,
        "message": f"{name} created in Coolify on {target_node}, routed at https://{domain}, "
                    f"registered in registry.yaml. First build is running in Coolify now.",
    }


# ============================================================ management
# Everything below is for the /apps dashboard: list what's deployed, show
# live status/logs, start/stop/restart, and tear down cleanly.

def _find_coolify_uuid(name: str) -> str | None:
    """Prefer the recorded mapping; fall back to a name-prefix search
    against Coolify's own application list for apps deployed before this
    mapping existed (e.g. hello-app, deployed by hand)."""
    mapped = _load_resource_map().get(name)
    if mapped and mapped.get("coolify_uuid"):
        return mapped["coolify_uuid"]
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{COOLIFY_URL}/applications", headers=_coolify_headers())
        r.raise_for_status()
        candidates = [a for a in r.json() if a.get("name", "").startswith(name)]
    # Prefer a running one over a stopped/leftover one when there are several.
    candidates.sort(key=lambda a: 0 if str(a.get("status", "")).startswith("running") else 1)
    return candidates[0]["uuid"] if candidates else None


_MEM_UNIT_TO_MB = {"b": 1 / (1024 * 1024), "kib": 1 / 1024, "mib": 1, "gib": 1024, "tib": 1024 * 1024}


def _parse_mem_to_mb(s: str) -> float:
    """docker stats formats sizes like '86.2MiB', '1.2GiB', '512kB'."""
    m = re.match(r"([\d.]+)\s*([a-zA-Z]+)", s.strip())
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2).lower().rstrip("b") + "b"
    return value * _MEM_UNIT_TO_MB.get(unit, 1.0)


def _docker_stats() -> list[dict]:
    """One live snapshot of every running container's CPU/memory."""
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    out = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        d = json.loads(line)
        used_str, limit_str = (d.get("MemUsage") or "0 / 0").split(" / ")
        out.append({
            "name": d.get("Name", ""),
            "cpu_percent": float((d.get("CPUPerc") or "0%").rstrip("%") or 0),
            "mem_used_mb": _parse_mem_to_mb(used_str),
            "mem_limit_mb": _parse_mem_to_mb(limit_str),
        })
    return out


def _host_memory_mb() -> tuple[float, float]:
    """(total, available) in MB. MemAvailable (not MemFree) is the real
    "could still be used" number -- it already accounts for reclaimable
    page cache, which MemFree does not."""
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            info[parts[0]] = int(parts[1]) / 1024  # kB -> MB
    return info.get("MemTotal:", 0.0), info.get("MemAvailable:", 0.0)


def app_resources(name: str) -> dict:
    """Live CPU/memory usage for one app, summed across all of its
    containers (a coolify-service has several; a coolify app has one)."""
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind")
    if kind == "pages":
        return {"kind": "pages", "cpu_percent": 0, "mem_used_mb": 0, "containers": 0}
    coolify_uuid = mapped.get("coolify_uuid") or _find_coolify_uuid(name)
    if not coolify_uuid:
        return {"kind": kind, "cpu_percent": 0, "mem_used_mb": 0, "containers": 0}
    stats = [s for s in _docker_stats() if coolify_uuid in s["name"]]
    return {
        "kind": kind,
        "cpu_percent": round(sum(s["cpu_percent"] for s in stats), 1),
        "mem_used_mb": round(sum(s["mem_used_mb"] for s in stats), 1),
        "containers": len(stats),
    }


# Coolify's own platform containers (not apps, not shared infra used by
# apps) -- grouped as a single "platform" slice rather than one each.
_PLATFORM_CONTAINER_PREFIXES = (
    "coolify", "coolify-db", "coolify-redis", "coolify-proxy",
    "coolify-realtime", "coolify-sentinel",
)


def resource_overview() -> dict:
    """Host-wide memory picture for the /apps pie chart: total RAM, how
    much every registered app is actually using right now (not its
    declared budget -- the real number), a platform slice for Coolify's
    own containers, and the remainder attributed to the OS/everything else
    not individually tracked."""
    stats = _docker_stats()
    resource_map = _load_resource_map()
    reg_apps = {a["name"] for a in load_registry().get("apps", [])}

    slices = []
    attributed_mb = 0.0
    matched_container_names = set()

    for name in reg_apps:
        mapped = resource_map.get(name, {})
        coolify_uuid = mapped.get("coolify_uuid") or _find_coolify_uuid(name)
        if not coolify_uuid:
            slices.append({"name": name, "mem_used_mb": 0.0})
            continue
        app_stats = [s for s in stats if coolify_uuid in s["name"]]
        used = sum(s["mem_used_mb"] for s in app_stats)
        matched_container_names.update(s["name"] for s in app_stats)
        slices.append({"name": name, "mem_used_mb": round(used, 1)})
        attributed_mb += used

    platform_stats = [s for s in stats if s["name"] not in matched_container_names
                       and any(s["name"].startswith(p) for p in _PLATFORM_CONTAINER_PREFIXES)]
    platform_mb = sum(s["mem_used_mb"] for s in platform_stats)
    matched_container_names.update(s["name"] for s in platform_stats)
    slices.append({"name": "platform (Coolify)", "mem_used_mb": round(platform_mb, 1)})
    attributed_mb += platform_mb

    # Anything running that isn't a registered app or a known platform
    # container (shared Postgres/Redis, leftover/manual containers, etc.)
    other_stats = [s for s in stats if s["name"] not in matched_container_names]
    other_mb = sum(s["mem_used_mb"] for s in other_stats)
    slices.append({"name": "other containers", "mem_used_mb": round(other_mb, 1)})
    attributed_mb += other_mb

    total_mb, available_mb = _host_memory_mb()
    real_used_mb = max(0.0, total_mb - available_mb)  # everything actually in use, containers + OS
    os_mb = max(0.0, real_used_mb - attributed_mb)
    slices.append({"name": "OS / system", "mem_used_mb": round(os_mb, 1)})
    slices.append({"name": "free", "mem_used_mb": round(available_mb, 1)})

    return {
        "total_mb": round(total_mb, 1),
        "used_mb": round(real_used_mb, 1),
        "available_mb": round(available_mb, 1),
        "slices": [s for s in slices if s["mem_used_mb"] > 0],
    }


def list_apps() -> list[dict]:
    reg = load_registry()
    resource_map = _load_resource_map()
    out = []
    for a in reg.get("apps", []):
        entry = {
            "name": a["name"], "target": a.get("target", "node"),
            "memory_mb": a.get("memory_mb", 0), "subdomain": a.get("subdomain"),
            "repo": a.get("repo"), "kind": resource_map.get(a["name"], {}).get("kind"),
        }
        if not entry["kind"]:
            entry["kind"] = "pages" if entry["target"] == "pages" else "coolify"
        out.append(entry)
    return out


def app_status(name: str) -> dict:
    reg_entry = next((a for a in load_registry().get("apps", []) if a["name"] == name), None)
    if not reg_entry:
        raise ValueError(f"{name} is not registered")
    kind = _load_resource_map().get(name, {}).get("kind") or \
        ("pages" if reg_entry.get("target") == "pages" else "coolify")

    if kind == "pages":
        with httpx.Client(timeout=15) as client:
            r = client.get(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{name}",
                headers=_cloudflare_headers(),
            )
            r.raise_for_status()
            proj = r.json()["result"]
        deployment = proj.get("latest_deployment") or {}
        return {
            "kind": "pages", "name": name,
            "status": (deployment.get("latest_stage") or {}).get("status", "unknown"),
            "url": deployment.get("url"),
            "domains": proj.get("domains", []),
            "memory_mb": 0,
        }

    if kind == "coolify-service":
        # A docker-compose stack -- Coolify decomposes it into "service
        # applications" and "service databases", each with their own
        # status. No single running/stopped flag for the whole thing;
        # report the worst-case status across containers plus the list.
        service_uuid = _load_resource_map().get(name, {}).get("coolify_uuid")
        if not service_uuid:
            return {"kind": "coolify-service", "name": name, "status": "not_found",
                    "memory_mb": reg_entry.get("memory_mb", 0)}
        with httpx.Client(timeout=15) as client:
            r = client.get(f"{COOLIFY_URL}/services/{service_uuid}", headers=_coolify_headers())
            r.raise_for_status()
            svc = r.json()
        containers = [{"name": a.get("name"), "status": a.get("status", "unknown")}
                      for a in svc.get("applications", []) + svc.get("databases", [])]
        overall = "running:healthy" if containers and all(
            str(c["status"]).startswith("running") for c in containers
        ) else "degraded" if any(str(c["status"]).startswith("running") for c in containers) else "exited"
        usage = app_resources(name)
        return {
            "kind": "coolify-service", "name": name, "coolify_uuid": service_uuid,
            "status": overall, "containers": containers,
            "memory_mb": reg_entry.get("memory_mb", 0),
            "cpu_percent": usage["cpu_percent"], "mem_used_mb": usage["mem_used_mb"],
        }

    coolify_uuid = _find_coolify_uuid(name)
    if not coolify_uuid:
        return {"kind": "coolify", "name": name, "status": "not_found", "memory_mb": reg_entry.get("memory_mb", 0)}
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{COOLIFY_URL}/applications/{coolify_uuid}", headers=_coolify_headers())
        r.raise_for_status()
        app = r.json()
    usage = app_resources(name)
    return {
        "kind": "coolify", "name": name, "coolify_uuid": coolify_uuid,
        "status": app.get("status", "unknown"),
        "memory_mb": reg_entry.get("memory_mb", 0),
        "fqdn": app.get("fqdn"),
        "cpu_percent": usage["cpu_percent"], "mem_used_mb": usage["mem_used_mb"],
    }


def app_logs(name: str, lines: int = 200) -> str:
    kind = _load_resource_map().get(name, {}).get("kind")
    if kind == "coolify-service":
        return ("(this is a multi-container service -- open it in Coolify directly "
                "to see per-container logs, e.g. wordpress/db/typesense/n8n each "
                "have their own log stream)")
    coolify_uuid = _find_coolify_uuid(name)
    if not coolify_uuid:
        return "(no Coolify resource found -- static/Pages apps don't have server logs here; check the Cloudflare Pages dashboard)"
    with httpx.Client(timeout=20) as client:
        r = client.get(
            f"{COOLIFY_URL}/applications/{coolify_uuid}/logs",
            headers=_coolify_headers(), params={"lines": lines},
        )
        r.raise_for_status()
        return r.json().get("logs", "")


def app_action(name: str, action: str) -> dict:
    """action: start | stop | restart. Coolify apps/services only -- Pages
    has no such concept (it's not a running process)."""
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"unknown action {action!r}")
    kind = _load_resource_map().get(name, {}).get("kind")
    if kind == "coolify-service":
        service_uuid = _load_resource_map().get(name, {}).get("coolify_uuid")
        if not service_uuid:
            raise ValueError(f"no Coolify service found for {name}")
        with httpx.Client(timeout=30) as client:
            r = client.post(f"{COOLIFY_URL}/services/{service_uuid}/{action}", headers=_coolify_headers())
            r.raise_for_status()
            return r.json()
    coolify_uuid = _find_coolify_uuid(name)
    if not coolify_uuid:
        raise ValueError(f"no Coolify resource found for {name} (static sites can't be start/stopped)")
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/applications/{coolify_uuid}/{action}", headers=_coolify_headers())
        r.raise_for_status()
        return r.json()


def remove_registry_entry(name: str) -> None:
    text = REGISTRY_PATH.read_text()
    lines = text.split("\n")
    out, i, removed = [], 0, False
    while i < len(lines):
        if lines[i].strip() == f"- name: {name}":
            removed = True
            i += 1
            while i < len(lines) and (lines[i].startswith("    ") or lines[i].strip() == ""):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if not removed:
        raise ValueError(f"{name} not found in registry.yaml")
    REGISTRY_PATH.write_text("\n".join(out))


def delete_app(name: str) -> dict:
    """Tears down everything the deploy agent created for this app:
    Coolify resource or Pages project, DNS record, tunnel route (Coolify
    only), the resource_map entry, and the registry.yaml entry -- then
    commits. Irreversible; the dashboard must confirm before calling this."""
    reg_entry = next((a for a in load_registry().get("apps", []) if a["name"] == name), None)
    if not reg_entry:
        raise ValueError(f"{name} is not registered")
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind") or ("pages" if reg_entry.get("target") == "pages" else "coolify")
    hostname = f"{name}.{PLATFORM_ROOT_DOMAIN}"

    if kind == "coolify-service":
        service_uuid = mapped.get("coolify_uuid")
        if service_uuid:
            with httpx.Client(timeout=30) as client:
                r = client.delete(f"{COOLIFY_URL}/services/{service_uuid}", headers=_coolify_headers())
                if r.status_code not in (200, 404):
                    r.raise_for_status()
        domains = mapped.get("domains") or []
        repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"
        cfg = yaml.safe_load(repo_config_path.read_text())
        service_hostnames = {f"{d}.{PLATFORM_ROOT_DOMAIN}" for d in domains}
        cfg["ingress"] = [r_ for r_ in cfg["ingress"] if r_.get("hostname") not in service_hostnames]
        repo_config_path.write_text(yaml.dump(cfg, sort_keys=False))
        subprocess.run(["sudo", "cp", str(repo_config_path), "/etc/cloudflared/config.yml"], check=True)
        subprocess.run(["sudo", "systemctl", "reload-or-restart", "cloudflared"], check=True)
        with httpx.Client(timeout=15) as client:
            for d in domains:
                h = f"{d}.{PLATFORM_ROOT_DOMAIN}"
                existing = client.get(
                    f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
                    headers=_cloudflare_headers(), params={"name": h},
                )
                existing.raise_for_status()
                for rec in existing.json()["result"]:
                    client.delete(f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records/{rec['id']}",
                                   headers=_cloudflare_headers())
        m = _load_resource_map()
        m.pop(name, None)
        RESOURCE_MAP_PATH.write_text(json.dumps(m, indent=2))
        remove_registry_entry(name)
        git_commit_and_push(f"registry: remove {name} (deleted via deploy agent)")
        return {"deleted": name, "kind": kind}

    if kind == "pages":
        with httpx.Client(timeout=20) as client:
            # Cloudflare refuses to delete a Pages project while it still
            # has a custom domain attached (confirmed live: 400, code
            # 8000028) -- detach first.
            dr = client.delete(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{name}/domains/{hostname}",
                headers=_cloudflare_headers(),
            )
            if dr.status_code not in (200, 404):
                dr.raise_for_status()
            r = client.delete(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{name}",
                headers=_cloudflare_headers(),
            )
            if r.status_code not in (200, 404):
                r.raise_for_status()
    else:
        coolify_uuid = _find_coolify_uuid(name)
        if coolify_uuid:
            with httpx.Client(timeout=30) as client:
                r = client.delete(f"{COOLIFY_URL}/applications/{coolify_uuid}", headers=_coolify_headers())
                if r.status_code not in (200, 404):
                    r.raise_for_status()
        # remove the tunnel ingress rule
        repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"
        cfg = yaml.safe_load(repo_config_path.read_text())
        cfg["ingress"] = [r_ for r_ in cfg["ingress"] if r_.get("hostname") != hostname]
        repo_config_path.write_text(yaml.dump(cfg, sort_keys=False))
        subprocess.run(["sudo", "cp", str(repo_config_path), "/etc/cloudflared/config.yml"], check=True)
        subprocess.run(["sudo", "systemctl", "reload-or-restart", "cloudflared"], check=True)

    with httpx.Client(timeout=15) as client:
        existing = client.get(
            f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
            headers=_cloudflare_headers(), params={"name": hostname},
        )
        existing.raise_for_status()
        for rec in existing.json()["result"]:
            client.delete(f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records/{rec['id']}",
                           headers=_cloudflare_headers())

    m = _load_resource_map()
    m.pop(name, None)
    RESOURCE_MAP_PATH.write_text(json.dumps(m, indent=2))

    remove_registry_entry(name)
    git_commit_and_push(f"registry: remove {name} (deleted via deploy agent)")
    return {"deleted": name, "kind": kind}
