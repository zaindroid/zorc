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
                     domains: list[str] | None = None, coolify_postgres_uuid: str | None = None,
                     container_name: str | None = None, postgres_container_name: str | None = None,
                     node: str | None = None) -> None:
    """kind: 'coolify' | 'coolify-service' | 'pages' | 'zorc-agent'. domains is only
    needed for 'coolify-service' -- a docker-compose stack can expose
    several subdomains that don't derive from `name` the way a normal
    app's single hostname does, so delete_app needs them listed explicitly
    to clean DNS up properly. coolify_postgres_uuid is set when
    provision_dedicated_postgres() created a database for this app --
    kept alongside so a future teardown/backup step can find it without
    re-deriving the name. container_name/postgres_container_name/node are
    the zorc-agent equivalents -- no UUIDs exist on that backend, teardown
    needs the container name plus which node it's actually on."""
    m = _load_resource_map()
    entry = {"kind": kind, "coolify_uuid": coolify_uuid, "domains": domains or []}
    if coolify_postgres_uuid:
        entry["coolify_postgres_uuid"] = coolify_postgres_uuid
    if container_name:
        entry["container_name"] = container_name
    if postgres_container_name:
        entry["postgres_container_name"] = postgres_container_name
    if node:
        entry["node"] = node
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


def _remote_host_memory_mb(tailscale_ip: str, ssh_key: Path = REMOTE_DEPLOY_KEY, user: str = "root") -> tuple[float, float]:
    """Same as _host_memory_mb() (defined further down) but for a node
    this process isn't running on, over SSH. Defaults match every
    backend: coolify node (root, the shared deploy key) -- a backend:
    zorc-agent node on a shared/partial machine (e.g. rtx5090) passes its
    own non-root ssh_key/ssh_user instead, since that's all it has."""
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(ssh_key), f"{user}@{tailscale_ip}", "cat", "/proc/meminfo"],
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
    see. A second, live check used immediately before create_coolify_app
    (or the zorc-agent equivalent), alongside (never instead of) the
    static budget_headroom_mb() check.

    This is the ONLY thing that actually protects a shared node like
    rtx5090 in real time -- registry.yaml's usable_mb/max_utilisation is
    pure accounting for zorc's own declared apps and has no idea how much
    RAM the machine's other, non-zorc containers are using at any given
    moment. This live check does, which is exactly why it's unconditional
    here regardless of backend."""
    node = node_config(node_name)
    if node_name == LOCAL_NODE:
        _, available_mb = _host_memory_mb()
    else:
        tailscale_ip = node.get("tailscale_ip")
        if not tailscale_ip:
            raise RuntimeError(f"{node_name!r} has no tailscale_ip in registry.yaml -- cannot live-check it")
        if node.get("backend") == "zorc-agent":
            ssh_key = ZORC_DIR / node["ssh_key"]
            user = node.get("ssh_user", "root")
            _, available_mb = _remote_host_memory_mb(tailscale_ip, ssh_key, user)
        else:
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


def _ssh_run(tailscale_ip: str, ssh_key: Path, remote_cmd: list[str], user: str = "root",
              timeout: int = 15) -> tuple[int, str, str]:
    """One-off remote command, same connection conventions as
    _probe_hardware_over_ssh -- used for the small existing-software checks
    (docker, coolify) propose_node() runs alongside the hardware probe, and
    (with a longer timeout) the zorc-agent backend's docker build/run/
    inspect commands."""
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(ssh_key), f"{user}@{tailscale_ip}", *remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def remote_node_probe(node_name: str) -> dict:
    """_probe_hardware_over_ssh() for an already-registered node -- looks
    up its tailscale_ip from registry.yaml, using REMOTE_DEPLOY_KEY as root
    for every backend: coolify node (the one key those all accept) or the
    node's own ssh_key/ssh_user for a backend: zorc-agent node, which may
    have no root access at all. Shared by two callers: watchdog.py's
    periodic refresh of non-local nodes' nodes/<name>.yaml, and
    (indirectly, via _probe_hardware_over_ssh) propose_node()'s one-off
    pre-registration capability report.

    Real bug found live: this always used REMOTE_DEPLOY_KEY as root,
    exactly like live_headroom_mb() before its own fix -- broke the
    watchdog's periodic refresh for both zorc-agent nodes the moment they
    were registered (rtx5090's nodes/rtx5090.yaml silently flipped to
    status: unreachable, jetson-thor's was reduced to a bare stub), since
    root SSH is blocked on both by Tailscale ACL policy. Same fix as
    live_headroom_mb(): use the node's own credentials when it has them."""
    node = node_config(node_name)
    tailscale_ip = node.get("tailscale_ip")
    if not tailscale_ip:
        raise RuntimeError(f"{node_name!r} has no tailscale_ip in registry.yaml -- cannot probe it remotely")
    if node.get("backend") == "zorc-agent":
        ssh_key = ZORC_DIR / node["ssh_key"]
        user = node.get("ssh_user", "root")
        return _probe_hardware_over_ssh(tailscale_ip, ssh_key, user)
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


def clone_repo(owner_repo: str, git_branch: str = "main") -> Path:
    """owner_repo like 'zaindroid/hello-app'. Uses gh CLI (already
    authenticated on this host) so it works for private repos too, not
    just public ones.

    Real bug found live: this used to ignore git_branch entirely, always
    cloning the default branch -- harmless-looking for the Coolify path
    (Coolify does its own separate remote clone of the real git_branch for
    the actual build, so only classify()/parse_app_yaml()/resolve_env_vars()
    were silently reading the wrong branch's files), but a real correctness
    bug for zorc-agent, which has no second remote clone step at all --
    _deploy_zorc_agent uploads and builds this exact repo_dir, so deploying
    a non-default branch silently built and shipped the default branch's
    code instead, with no error. Caught because a deploy from a test branch
    that added `database: true` came back with no database provisioned."""
    workdir = Path(tempfile.mkdtemp(prefix="deploy-"))
    subprocess.run(
        ["gh", "repo", "clone", owner_repo, str(workdir), "--", "--depth", "1", "--branch", git_branch],
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
    persistent_storage = parsed.get("persistent_storage")
    if persistent_storage is not None:
        if not isinstance(persistent_storage, dict) or "mount_path" not in persistent_storage:
            raise ValueError(
                f"app.yaml persistent_storage must be {{mount_path: /some/path}}, got {persistent_storage!r}"
            )
    return {"env": env, "database": database, "persistent_storage": persistent_storage}


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


def _provision_postgres_coolify(app_name: str, node: dict, target_node: str,
                                 image: str = "postgres:18-alpine") -> tuple[str, list[str]]:
    """Backend-specific head #1: creates a Coolify-managed dedicated
    Postgres, waits (bounded, 90s) for it to report healthy. Returns
    (container_name, exec_cmd_prefix) -- exec_cmd_prefix is how the shared
    tail below reaches it: plain `docker exec` if this process already
    runs on the target (servingz), SSH with the standard REMOTE_DEPLOY_KEY
    otherwise.

    image -- override for an app needing a non-vanilla Postgres, e.g.
    "pgvector/pgvector:pg18" for an app that needs the vector extension
    (vanilla postgres:18-alpine doesn't carry it). Same role+database
    creation tail either way; see provision_dedicated_postgres's
    post_create_sql for running extension/schema setup on the new
    database once it exists."""
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/databases/postgresql", headers=_coolify_headers(), json={
            "project_uuid": COOLIFY_PROJECT_UUID,
            "server_uuid": node["server_uuid"],
            "environment_name": COOLIFY_ENVIRONMENT_NAME,
            "name": f"{app_name}-postgres",
            "image": image,
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
    if target_node == LOCAL_NODE:
        exec_prefix = ["docker", "exec", "-i", container_name, "psql", "-U", "postgres"]
    else:
        tailscale_ip = node.get("tailscale_ip")
        if not tailscale_ip:
            raise RuntimeError("node has no tailscale_ip in registry.yaml -- cannot reach it")
        exec_prefix = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                        "-i", str(REMOTE_DEPLOY_KEY), f"root@{tailscale_ip}",
                        "docker", "exec", "-i", container_name, "psql", "-U", "postgres"]
    return container_name, exec_prefix


def _provision_postgres_zorc_agent(app_name: str, node: dict,
                                    image: str = "postgres:18-alpine") -> tuple[str, list[str]]:
    """Backend-specific head #2: a direct `docker run` on the zorc-agent
    network -- no Coolify API involved at all, matches everything else
    this backend does. Superuser password generated here, used only for
    the one connection the shared tail makes immediately after, never
    returned or logged. Returns (container_name, exec_cmd_prefix), same
    shape as the Coolify head. image: see _provision_postgres_coolify."""
    tailscale_ip = node.get("tailscale_ip")
    if not tailscale_ip:
        raise RuntimeError("node has no tailscale_ip in registry.yaml -- cannot reach it")
    ssh_key = ZORC_DIR / node["ssh_key"]
    user = node.get("ssh_user", "root")

    _zorc_agent_ensure_network(tailscale_ip, ssh_key, user)
    container_name = f"{app_name}-postgres"
    superuser_password = secrets.token_hex(24)
    rc, out, err = _ssh_run(
        tailscale_ip, ssh_key,
        ["docker", "run", "-d", "--name", container_name, "--network", ZORC_AGENT_NETWORK,
         "--restart", "unless-stopped", "--label", "managed-by=zorc", "--label", f"zorc-app={app_name}",
         "-e", f"POSTGRES_PASSWORD={superuser_password}", image],
        user, timeout=30,
    )
    if rc != 0:
        raise RuntimeError(f"failed to start dedicated postgres for {app_name!r}: {err[-500:]}")
    container_id = out.strip()
    _zorc_agent_wait_healthy(tailscale_ip, ssh_key, user, container_id, timeout_sec=60)

    # Container-running is not the same as Postgres-accepting-connections
    # (it takes a few seconds to init even after the process starts) --
    # wait for pg_isready specifically before handing off to the shared
    # SQL tail, which would otherwise hit a connection-refused race.
    ready_deadline = time.time() + 30
    ready = False
    while time.time() < ready_deadline:
        rc, _, _ = _ssh_run(tailscale_ip, ssh_key,
                             ["docker", "exec", container_name, "pg_isready", "-U", "postgres"], user)
        if rc == 0:
            ready = True
            break
        time.sleep(2)
    if not ready:
        raise RuntimeError(f"dedicated postgres for {app_name!r} did not become ready within 30s")

    exec_prefix = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                    "-i", str(ssh_key), f"{user}@{tailscale_ip}",
                    "docker", "exec", "-i", container_name, "psql", "-U", "postgres"]
    return container_name, exec_prefix


def provision_dedicated_postgres(app_name: str, target_node: str,
                                  image: str = "postgres:18-alpine",
                                  post_create_sql: str | None = None) -> tuple[str, str]:
    """Creates a new, single-app-dedicated Postgres instance on target_node
    -- via Coolify's API for backend: coolify nodes, or a direct `docker
    run` for backend: zorc-agent nodes (see the two heads above) -- then
    runs the same role+database creation SQL either way (this tail), and
    returns (container_identifier, database_url). The instance's own
    superuser credentials never leave this function -- not returned, not
    logged, not passed to the caller -- only the freshly-generated
    app-scoped role's connection string is.

    Built after discovering DATABASE_URL provisioning was entirely
    unimplemented despite being a documented part of the platform contract
    -- blylinks-crm needed this done by hand once; this is that process
    turned into reusable, repeatable code.

    image -- non-default Postgres image, e.g. "pgvector/pgvector:pg18" for
    an app needing the vector extension (see the two provision heads).

    post_create_sql -- run against the NEW app database (not the
    maintenance "postgres" database the initial connection lands on) right
    after it's created, via a `\\c {db_role}` reconnect -- e.g.
    "CREATE EXTENSION IF NOT EXISTS vector;" for an app using image=
    pgvector/pgvector. Runs as the new role's own owner privileges are
    already in place by this point, so no separate grant is needed."""
    node = node_config(target_node)
    if node.get("backend") == "zorc-agent":
        container_name, exec_prefix = _provision_postgres_zorc_agent(app_name, node, image=image)
    else:
        container_name, exec_prefix = _provision_postgres_coolify(app_name, node, target_node, image=image)

    db_role = re.sub(r"[^a-z0-9_]", "_", app_name.lower())
    db_password = secrets.token_hex(24)
    sql = f"CREATE ROLE {db_role} WITH LOGIN PASSWORD '{db_password}'; CREATE DATABASE {db_role} OWNER {db_role};"
    if post_create_sql:
        sql += f"\n\\c {db_role}\n{post_create_sql}"

    proc = subprocess.run(exec_prefix, input=sql, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create role/database for {app_name!r}: {proc.stderr[-500:]}")

    database_url = f"postgres://{db_role}:{db_password}@{container_name}:5432/{db_role}"
    return container_name, database_url


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


def add_coolify_persistent_storage(coolify_uuid: str, name: str, mount_path: str) -> None:
    """Attaches a Coolify-managed named volume to the application, mounted
    at mount_path -- no host_path given, so Coolify creates a Docker-
    managed volume (not a host bind mount) and namespaces it under the
    app's own uuid to avoid collisions with any other app's volume.

    Called after create_coolify_app (instant_deploy=False) and before
    trigger_coolify_deploy, same ordering as set_coolify_env_vars, so the
    container's first real start already has the mount -- Coolify (like
    Docker) only applies volume changes on container recreation, not to
    an already-running container.

    Payload shape confirmed live against a disposable throwaway app
    (create_coolify_app + this call + GET to verify + DELETE to clean up,
    all outside any real deploy): type is a required field with no
    useful error on what's valid until you guess right -- "persistent"
    is a Docker-managed volume (this function's only use case); the
    other valid value, "file", is for single-file bind-mounts with a
    different field set entirely, not used here."""
    with httpx.Client(timeout=30) as client:
        r = client.post(
            f"{COOLIFY_URL}/applications/{coolify_uuid}/storages",
            headers=_coolify_headers(),
            json={"name": name, "mount_path": mount_path, "type": "persistent"},
        )
        r.raise_for_status()


def trigger_coolify_deploy(coolify_uuid: str) -> None:
    """Explicitly starts the first real build+deploy -- the counterpart to
    create_coolify_app(instant_deploy=False). Coolify's own webhook-style
    deploy-trigger endpoint, keyed by application uuid."""
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{COOLIFY_URL}/deploy", headers=_coolify_headers(), params={"uuid": coolify_uuid})
        r.raise_for_status()


# ---------------------------------------------------------- zorc-agent ----
# The lightweight, non-root deploy backend for nodes that can't or
# shouldn't run full Coolify -- built for one real case: a personal GPU
# workstation already running 40+ of its own containers under a non-root
# user, on a Tailscale network whose ACL policy blocks root SSH outright.
# No Nixpacks (not installed there, and installing new tooling on a
# machine like this is exactly the footprint this backend exists to
# avoid) -- Dockerfile-only, built and run directly via whatever docker
# access the deploy key's user already has (confirmed: docker-group
# membership, no sudo needed, for the one real node this targets today).

ZORC_AGENT_NETWORK = "zorc-agent"
ZORC_AGENT_CONTAINER_PORT = 8080  # same "apps listen on 8080" convention as the Coolify path
ZORC_AGENT_PORT_RANGE = (20000, 20100)


def _zorc_agent_ensure_network(tailscale_ip: str, ssh_key: Path, user: str) -> None:
    """Idempotent. Never removed by rollback -- other containers may
    already be attached (this is a shared network across every zorc-agent
    app on the node), and Docker refuses to remove a network still in use
    regardless."""
    rc, _, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "network", "inspect", ZORC_AGENT_NETWORK], user)
    if rc == 0:
        return
    create_rc, _, err = _ssh_run(tailscale_ip, ssh_key, ["docker", "network", "create", ZORC_AGENT_NETWORK], user)
    if create_rc != 0:
        raise RuntimeError(f"failed to create {ZORC_AGENT_NETWORK!r} network: {err[-500:]}")


def _zorc_agent_allocate_port(tailscale_ip: str, ssh_key: Path, user: str) -> int:
    """Live-probes for the first free port in ZORC_AGENT_PORT_RANGE rather
    than trusting a static claimed-list -- this node's other 40+ services
    are unknown to zorc, so a stale table would drift immediately. Parses
    the full listener list in Python rather than passing a filter
    expression through `ss` -- that string has to survive local shell,
    ssh's own argument reconstruction, and a remote shell, and a filter
    expression with spaces/colons in it does not survive that trip
    intact (confirmed live: silently matched nothing, real bug caught
    during this build, not a hypothetical). A clean `ss -Htln`/`netstat
    -tln` with no embedded filter avoids the whole problem. Inherently
    TOCTOU-racy regardless (nothing stops something else binding the port
    between this probe and the eventual `docker run -p`) -- the caller
    must treat "address already in use" from that step as a
    retry-next-port condition, not fatal."""
    rc, out, err = _ssh_run(tailscale_ip, ssh_key, ["ss", "-Htln"], user)
    if rc != 0:
        rc, out, err = _ssh_run(tailscale_ip, ssh_key, ["netstat", "-tln"], user)
        if rc != 0:
            raise RuntimeError(f"could not list listening ports on {tailscale_ip!r} "
                                f"(neither ss nor netstat available): {err[-300:]}")

    used_ports = set()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local_addr = cols[3]  # State Recv-Q Send-Q <Local Address:Port> Peer...
        if ":" in local_addr:
            port_str = local_addr.rsplit(":", 1)[-1]
            if port_str.isdigit():
                used_ports.add(int(port_str))

    for port in range(*ZORC_AGENT_PORT_RANGE):
        if port not in used_ports:
            return port
    raise RuntimeError(f"no free port in {ZORC_AGENT_PORT_RANGE[0]}-{ZORC_AGENT_PORT_RANGE[1]} on {tailscale_ip!r}")


def _zorc_agent_upload_repo(tailscale_ip: str, ssh_key: Path, user: str, repo_dir: Path, remote_dir: str) -> None:
    """Copies repo_dir's contents to remote_dir on the target via a tar
    pipe over SSH -- no rsync/scp availability assumptions on a machine
    zorc doesn't control the software inventory of, just tar (universal)
    and the SSH connection every other zorc-agent operation already uses."""
    mkdir_rc, _, mkdir_err = _ssh_run(tailscale_ip, ssh_key, ["mkdir", "-p", remote_dir], user)
    if mkdir_rc != 0:
        raise RuntimeError(f"failed to create {remote_dir!r} on target: {mkdir_err[-300:]}")

    tar_proc = subprocess.Popen(["tar", "czf", "-", "-C", str(repo_dir), "."], stdout=subprocess.PIPE)
    try:
        ssh_proc = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
             "-i", str(ssh_key), f"{user}@{tailscale_ip}", "tar", "xzf", "-", "-C", remote_dir],
            stdin=tar_proc.stdout, capture_output=True, text=True, timeout=120,
        )
    finally:
        tar_proc.stdout.close()
        tar_proc.wait()
    if ssh_proc.returncode != 0:
        raise RuntimeError(f"failed to upload repo to {remote_dir!r}: {(ssh_proc.stderr or ssh_proc.stdout)[-500:]}")


def _zorc_agent_build_image(tailscale_ip: str, ssh_key: Path, user: str, remote_dir: str, image_tag: str) -> None:
    rc, _, err = _ssh_run(tailscale_ip, ssh_key, ["docker", "build", "-t", image_tag, remote_dir],
                           user, timeout=600)
    if rc != 0:
        raise RuntimeError(f"docker build failed: {err[-1500:]}")


def _zorc_agent_preflight_gpu(tailscale_ip: str, ssh_key: Path, user: str) -> None:
    """Checked before attempting --gpus all, so a missing nvidia runtime
    surfaces as a clear rejection here rather than an opaque docker error
    after the image is already built.

    Real bug found live, same class as the other two this session: a Go
    template with an embedded space ("--format", "{{json .Runtimes}}")
    does not survive the local-shell -> ssh-argument-reconstruction ->
    remote-shell round trip -- the space inside that single argv element
    splits into two words once ssh joins argv with spaces and the remote
    shell re-tokenizes, so `docker info` received `--format {{json` (one
    stray extra word `.Runtimes}}`) and errored out; a real nvidia runtime
    on the target was invisible to this check and would have wrongly
    blocked every needs_gpu=True deploy. Fixed by dropping --format
    entirely and reading the plain-text "Runtimes:" line instead -- no
    embedded spaces, nothing to reconstruct incorrectly."""
    rc, out, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "info"], user)
    runtimes_line = next((line for line in out.splitlines() if line.strip().startswith("Runtimes:")), "")
    if rc != 0 or "nvidia" not in runtimes_line:
        raise RuntimeError(
            f"needs_gpu=True but {tailscale_ip!r} has no 'nvidia' Docker runtime configured "
            f"(docker info Runtimes line: {runtimes_line.strip() or '(unavailable)'})"
        )


def _zorc_agent_run_container(tailscale_ip: str, ssh_key: Path, user: str, *, name: str, image_tag: str,
                               port: int, memory_mb: int, env_vars: dict[str, str], needs_gpu: bool,
                               gpu_legacy_runtime: bool = False, gpu_cdi: bool = False,
                               volumes: list[str] | None = None) -> str:
    """Starts the container, labeled for safe rollback identification.
    Returns the container ID. Does not itself verify health -- see
    _zorc_agent_wait_healthy().

    gpu_legacy_runtime -- real bug found live on jetson-thor: its Docker
    (Jetson/L4T-style nvidia-container-runtime) rejects the `--gpus all`
    flag outright ("invoking the NVIDIA Container Runtime Hook directly
    ... is not supported"), even though rtx5090's Docker (newer NVIDIA
    Container Toolkit) accepts it fine -- two GPU nodes, two incompatible
    conventions. `docker info` confirms both machines register an
    `nvidia` runtime, so `--runtime nvidia` + the driver-capabilities env
    vars it expects (not needed with `--gpus`) is the portable fallback.
    Driven by nodes/<name>'s own `gpu_runtime: legacy` in registry.yaml
    (see _deploy_zorc_agent).

    gpu_cdi -- real bug found live on rtx5090: `--gpus all` (the legacy
    nvidia-container-runtime hook path) started failing there with
    `Failed to initialize NVML: Unknown Error` inside the container even
    though host-level `nvidia-smi` was completely healthy -- confirmed via
    a direct A/B test (`docker run --gpus all ... nvidia-smi -L` fails,
    `docker run --device nvidia.com/gpu=all ... nvidia-smi -L` succeeds on
    the same host, same moment). Root cause: the legacy hook path is
    broken on this host's toolkit version/config; the newer CDI
    (Container Device Interface) device-injection path is not. `--device
    nvidia.com/gpu=all` is the CDI equivalent of `--gpus all` and needs no
    extra env vars. Driven by nodes/<name>'s own `gpu_runtime: cdi` in
    registry.yaml (see _deploy_zorc_agent)."""
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "--network", ZORC_AGENT_NETWORK,
        "--memory", f"{memory_mb}m",
        "--restart", "unless-stopped",
        "--label", "managed-by=zorc",
        "--label", f"zorc-app={name}",
        "-p", f"{port}:{ZORC_AGENT_CONTAINER_PORT}",
    ]
    # Not wired through the public MCP deploy() surface -- a host bind
    # mount is real host filesystem access, a meaningfully bigger
    # privilege than anything else that surface grants. Internal-only,
    # for direct agent.deploy() calls (this session's established
    # pattern), same trust boundary as target_node forcing.
    for v in (volumes or []):
        cmd += ["-v", v]
    if needs_gpu:
        if gpu_legacy_runtime:
            cmd += ["--runtime", "nvidia",
                    "-e", "NVIDIA_VISIBLE_DEVICES=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all"]
        elif gpu_cdi:
            cmd += ["--device", "nvidia.com/gpu=all"]
        else:
            cmd += ["--gpus", "all"]
        # Multi-process GPU frameworks (sglang/vLLM tensor-parallel workers
        # coordinating over NCCL, torch shared-memory tensors) routinely
        # need far more than Docker's 64MB default /dev/shm -- real gap
        # found live deploying a multi-GPU model, not a hypothetical.
        # Fixed at a generous size rather than plumbed through app.yaml:
        # every needs_gpu app on this backend is an ML workload with the
        # same class of requirement, and this costs nothing when unused
        # (tmpfs, not a reservation against the node's real RAM).
        cmd += ["--shm-size", "16g"]
    for key, value in env_vars.items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append(image_tag)

    rc, out, err = _ssh_run(tailscale_ip, ssh_key, cmd, user, timeout=30)
    if rc != 0:
        raise RuntimeError(f"docker run failed: {err[-500:]}")
    return out.strip()


def _zorc_agent_inspect(tailscale_ip: str, ssh_key: Path, user: str, container: str) -> dict | None:
    """`docker inspect` with no -f, JSON parsed locally, rather than a Go
    template string -- real bug found live during this build: a template
    string with embedded double quotes (needed for `{{index .Config.Labels
    "managed-by"}}`) does not reliably survive the round trip through
    local shell -> ssh's own argument reconstruction -> remote shell
    (confirmed: silently returned nothing for a label that was genuinely
    there). Same class of bug as the port-allocator's filter-expression
    issue -- avoid passing any string with embedded quoting through this
    path at all, parse structured output in Python instead. Returns None
    if the container doesn't exist."""
    rc, out, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "inspect", container], user)
    if rc != 0:
        return None
    try:
        parsed = json.loads(out)
        return parsed[0] if parsed else None
    except (json.JSONDecodeError, IndexError):
        return None


def _zorc_agent_wait_healthy(tailscale_ip: str, ssh_key: Path, user: str, container_id: str,
                              timeout_sec: int = 30) -> None:
    """Bounded wait confirming the container is still running after
    startup, not immediately crash-looped -- without this, "deploy
    succeeded" could mean nothing more than "the container was created."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        info = _zorc_agent_inspect(tailscale_ip, ssh_key, user, container_id)
        status = ((info or {}).get("State") or {}).get("Status")
        if status == "running":
            return
        if status in ("exited", "dead"):
            _, logs, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "logs", "--tail", "50", container_id], user)
            raise RuntimeError(f"container exited immediately after start (status={status}): {logs[-1000:]}")
        time.sleep(2)
    raise RuntimeError(f"container did not reach 'running' state within {timeout_sec}s")


def _zorc_agent_rollback(tailscale_ip: str, ssh_key: Path, user: str, container_names: list[str]) -> list[dict]:
    """Best-effort teardown of zorc-created containers on a later-step
    failure. Verifies the managed-by=zorc label before touching anything --
    never a name-pattern glob, on a machine with 40+ containers zorc did
    not create. Never removes the shared network (see
    _zorc_agent_ensure_network)."""
    results = []
    for cname in container_names:
        info = _zorc_agent_inspect(tailscale_ip, ssh_key, user, cname)
        labels = ((info or {}).get("Config") or {}).get("Labels") or {}
        if labels.get("managed-by") != "zorc":
            results.append({"container": cname, "ok": False,
                             "error": "not found, or missing managed-by=zorc label -- refused to touch"})
            continue
        _ssh_run(tailscale_ip, ssh_key, ["docker", "stop", cname], user, timeout=20)
        rm_rc, _, rm_err = _ssh_run(tailscale_ip, ssh_key, ["docker", "rm", "-f", cname], user, timeout=20)
        results.append({"container": cname, "ok": rm_rc == 0, "error": None if rm_rc == 0 else rm_err[-300:]})
    return results


def register_app(*, name: str, memory_mb: int, subdomain: str, repo: str, owner: str, target: str = "servingz",
                  database: bool = False, redis: bool = False, critical: bool = False) -> None:
    """Appends a new entry to registry.yaml and commits it — mirrors what a
    human did by hand for hello-app. target must be "pages" for static
    sites (memory_mb: 0) or a real node name from registry.yaml's `nodes`
    section for real apps -- check_budget.py's own sanity rule rejects
    node+0 or pages+nonzero combinations, and an unknown target name.

    owner is required, not optional -- the resolved MCP client name (see
    mcp_server.py's _caller_identity) that requested this deploy, or an
    explicit human-chosen value for anything registered outside the MCP
    path. No default and no empty-string fallback: an app with no owner
    is exactly the gap Phase 1 (ownership) closes, so a caller forgetting
    to pass one should fail loudly here rather than silently produce
    another unowned entry -- see scripts/backfill_owner.py for the one-off
    migration that gave every PRE-Phase-1 entry an owner."""
    if not owner:
        raise ValueError("register_app() requires a non-empty owner -- refusing to create another unowned entry")
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
    owner: "{owner}"
    critical: {"true" if critical else "false"}
    depends_on: []
"""
    marker = "# Applications. Add new entries at the end. Keep alphabetical within groups.\n# ---------------------------------------------------------------------------\napps:"
    if marker not in text:
        raise RuntimeError("registry.yaml marker not found — format changed, update register_app()")
    text = text.replace(marker, marker + entry, 1)
    REGISTRY_PATH.write_text(text)


def add_tunnel_route(hostname: str, service: str = "https://localhost:443") -> None:
    """Every Coolify-managed app routes through the same Traefik hop --
    Traefik dispatches to the right container by Host() header, using the
    domains we set on the Coolify app resource. Same pattern as every
    existing app entry (hello.zaindroid.me, hello-staging.zaindroid.me).
    Default `service` is unchanged for these callers.

    zorc-agent apps have no Traefik hop and no TLS of their own -- cloudflared
    on servingz reaches the target node directly over Tailscale, so callers
    pass service="http://<tailscale_ip>:<port>" instead. Plain HTTP, no
    originRequest override needed (that only exists to skip verifying
    Traefik's self-signed origin cert, which doesn't apply here).

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
        new_rule = {"hostname": hostname, "service": service}
        if service.startswith("https://"):
            new_rule["originRequest"] = {"noTLSVerify": True}
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


def _deploy_zorc_agent(*, step, log: list, node: dict, target_node: str, name: str, owner_repo: str, owner: str,
                        repo_dir: Path, classification: dict, memory_mb: int, env_vars_to_set: dict[str, str],
                        needs_gpu: bool, needs_database: bool, postgres_container_name: str | None,
                        domain: str, volumes: list[str] | None = None) -> dict:
    """The zorc-agent equivalent of deploy()'s Coolify branch below --
    everything from here down runs entirely over SSH against a non-root
    Docker daemon, no Coolify API involved. Called from deploy() once the
    shared clone/classify/budget/env/database/live-headroom steps have all
    passed -- those steps are backend-agnostic and stay in deploy() itself."""
    tailscale_ip = node["tailscale_ip"]
    ssh_key = ZORC_DIR / node["ssh_key"]
    user = node.get("ssh_user", "root")

    step("ensure_network", _zorc_agent_ensure_network, tailscale_ip, ssh_key, user)
    port = step("allocate_port", _zorc_agent_allocate_port, tailscale_ip, ssh_key, user)

    remote_dir = f"/home/{user}/zorc-agent-apps/{name}"
    step("upload_repo", _zorc_agent_upload_repo, tailscale_ip, ssh_key, user, repo_dir, remote_dir)

    image_tag = f"zorc-{name}:latest"
    step("build_image", _zorc_agent_build_image, tailscale_ip, ssh_key, user, remote_dir, image_tag)

    if needs_gpu:
        step("preflight_gpu", _zorc_agent_preflight_gpu, tailscale_ip, ssh_key, user)

    container_id = step(
        "run_container", _zorc_agent_run_container, tailscale_ip, ssh_key, user,
        name=name, image_tag=image_tag, port=port, memory_mb=memory_mb,
        env_vars=env_vars_to_set, needs_gpu=needs_gpu,
        gpu_legacy_runtime=node.get("gpu_runtime") == "legacy",
        gpu_cdi=node.get("gpu_runtime") == "cdi", volumes=volumes,
    )

    try:
        step("wait_healthy", _zorc_agent_wait_healthy, tailscale_ip, ssh_key, user, container_id)

        # No Traefik/public IP on this backend -- cloudflared on servingz
        # reaches the target node directly over Tailscale instead.
        service_url = f"http://{tailscale_ip}:{port}"
        step("create_dns_record", create_dns_record, name)
        step("add_tunnel_route", add_tunnel_route, domain, service=service_url)

        step("register_app", register_app, name=name, memory_mb=memory_mb, subdomain=name,
             repo=f"github.com/{owner_repo}", owner=owner, target=target_node, database=needs_database)
        step("record_resource", record_resource, name, kind="zorc-agent", container_name=name,
             postgres_container_name=postgres_container_name, node=target_node)
        step("commit_and_push", git_commit_and_push, f"registry: add {name} (deployed via zorc-agent)")
    except DeployError:
        # container/database creation already succeeded above -- clean up
        # anything zorc actually created (label-verified, never touches the
        # machine's other 40+ pre-existing containers) rather than leaving
        # a half-registered app running unattended.
        rollback_targets = [name]
        if postgres_container_name:
            rollback_targets.append(postgres_container_name)
        rollback_results = _zorc_agent_rollback(tailscale_ip, ssh_key, user, rollback_targets)
        log.append({"step": "rollback_zorc_agent", "ok": all(r["ok"] for r in rollback_results),
                    "detail": rollback_results})
        raise

    return {
        "log": log,
        "classification": classification,
        "status": "deployed",
        "domain": domain,
        "target_node": target_node,
        "container_name": name,
        "postgres_container_name": postgres_container_name,
        "message": f"{name} created on {target_node} (zorc-agent) at port {port}, routed at "
                    f"https://{domain} via Cloudflare Tunnel, registered in registry.yaml.",
    }


class DeployError(Exception):
    def __init__(self, step: str, reason: str):
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


def deploy(*, owner_repo: str, name: str, owner: str, git_branch: str = "main", target_node: str = "servingz",
           memory_mb_override: int | None = None, env_overrides: dict[str, str] | None = None,
           needs_gpu: bool = False, volumes: list[str] | None = None) -> dict:
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

    owner is required (Phase 1 -- see register_app()) and is stamped onto
    the new registry.yaml entry verbatim. mcp_server.py's deploy() tool
    always passes the caller's own resolved identity here (never anything
    the caller typed) -- so an app deployed through the MCP surface is
    owned by whoever's token actually created it, full stop.

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
    Ignored for static sites (no runtime env vars to set).

    needs_gpu requires target_node's backend to be "zorc-agent" -- GPU
    passthrough isn't implemented for the Coolify path at all (checked
    here, fails before cloning anything), regardless of whether the target
    node happens to report some accelerator in its live telemetry."""
    log = []
    node = node_config(target_node)  # raises KeyError immediately on a bad name

    if needs_gpu and node.get("backend") != "zorc-agent":
        raise DeployError(
            "gpu_backend_check",
            f"{target_node!r} has backend={node.get('backend')!r} -- GPU passthrough is only implemented "
            "for backend: zorc-agent nodes, regardless of what hardware the target reports",
        )

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

    repo_dir = step("clone", clone_repo, owner_repo, git_branch)
    classification = step("classify", classify, repo_dir)

    if classification["kind"] == "unknown":
        raise DeployError("classify", classification["reason"] + " — cannot proceed automatically")

    # zorc-agent nodes have no Nixpacks/buildpack tooling at all (deliberately
    # not installed -- new dependencies on these machines are exactly the
    # footprint zorc-agent exists to avoid). A genuine new constraint versus
    # the Coolify path, not an existing rule -- say so plainly rather than
    # implying a missing feature. Checks the file's actual presence, not
    # classification["language"] -- classify() reports "dockerfile" only
    # when NO other manifest is recognized, so a repo with both a proper
    # Dockerfile AND e.g. pyproject.toml (a perfectly normal, good setup --
    # hello-app has exactly this shape) would otherwise be wrongly rejected
    # here despite having exactly what this backend needs.
    if node.get("backend") == "zorc-agent" and not (repo_dir / "Dockerfile").exists():
        raise DeployError(
            "backend_build_check",
            f"{target_node!r} (backend: zorc-agent) only supports repos with a Dockerfile at root -- "
            "none found. No buildpack auto-detection on this backend; add a Dockerfile or target a "
            "backend: coolify node instead.",
        )

    # Determined here (not deferred into the app.yaml env: parse further
    # down) specifically so the extra memory a dedicated Postgres needs is
    # already folded into memory_mb before budget_check runs -- otherwise
    # the static budget check would pass on a number the deploy would then
    # exceed once provision_dedicated_postgres() actually runs.
    needs_database = classification["kind"] != "static" and parse_app_yaml(repo_dir).get("database", False)
    # Coolify-only today (zorc-agent has no app that needs this yet) -- see
    # add_coolify_persistent_storage(). {mount_path: "/opt/data"} in app.yaml.
    persistent_storage = parse_app_yaml(repo_dir).get("persistent_storage") if classification["kind"] != "static" else None

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
             repo=f"github.com/{owner_repo}", owner=owner, target="pages")
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

    if node.get("backend") == "zorc-agent":
        return _deploy_zorc_agent(
            step=step, log=log, node=node, target_node=target_node, name=name, owner_repo=owner_repo,
            owner=owner, repo_dir=repo_dir, classification=classification, memory_mb=memory_mb,
            env_vars_to_set=env_vars_to_set, needs_gpu=needs_gpu, needs_database=needs_database,
            postgres_container_name=postgres_uuid, domain=domain, volumes=volumes,
        )

    coolify_result = step(
        "create_coolify_app", create_coolify_app,
        name=name, git_repository=f"https://github.com/{owner_repo}",
        git_branch=git_branch, build_pack=build_pack, memory_mb=memory_mb, domain=domain,
        server_uuid=node["server_uuid"], instant_deploy=not env_vars_to_set and not persistent_storage,
        build_command=classification.get("build_command"),
        start_command=classification.get("start_command"),
    )

    try:
        if env_vars_to_set or persistent_storage:
            # instant_deploy was False above specifically so this can run
            # first -- the container's actual first start happens at
            # trigger_coolify_deploy, by which point every declared env var
            # (generated or caller-supplied) is already set and the volume
            # (if any) is already attached -- Coolify, like Docker, only
            # applies a volume mount on container (re)creation, not to an
            # already-running one.
            if env_vars_to_set:
                step("set_env_vars", set_coolify_env_vars, coolify_result["uuid"], env_vars_to_set)
            if persistent_storage:
                step("add_persistent_storage", add_coolify_persistent_storage, coolify_result["uuid"],
                     name=f"{name}-data", mount_path=persistent_storage["mount_path"])
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
             repo=f"github.com/{owner_repo}", owner=owner, target=target_node, database=needs_database)
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

    if kind == "zorc-agent":
        # No Coolify resource, no Traefik -- self-contained like the
        # coolify-service branch below: real docker containers over SSH,
        # then the same DNS + tunnel-ingress + resource_map + registry
        # cleanup every other kind does.
        target_node = mapped.get("node") or reg_entry.get("target")
        node = node_config(target_node)
        tailscale_ip = node["tailscale_ip"]
        ssh_key = ZORC_DIR / node["ssh_key"]
        user = node.get("ssh_user", "root")

        rollback_targets = [mapped.get("container_name") or name]
        if mapped.get("postgres_container_name"):
            rollback_targets.append(mapped["postgres_container_name"])
        rollback_results = _zorc_agent_rollback(tailscale_ip, ssh_key, user, rollback_targets)

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
        return {"deleted": name, "kind": kind, "rollback": rollback_results}

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
