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
import subprocess
import tempfile
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
                     domains: list[str] | None = None) -> None:
    """kind: 'coolify' | 'coolify-service' | 'pages'. domains is only
    needed for 'coolify-service' -- a docker-compose stack can expose
    several subdomains that don't derive from `name` the way a normal
    app's single hostname does, so delete_app needs them listed explicitly
    to clean DNS up properly."""
    m = _load_resource_map()
    m[name] = {"kind": kind, "coolify_uuid": coolify_uuid, "domains": domains or []}
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
        return {"kind": "app", "language": "node", "memory_mb": FRAMEWORK_MEMORY_MB["node"],
                "reason": "package.json with a server script"}

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
                        build_pack: str, memory_mb: int, domain: str, server_uuid: str) -> dict:
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
    }
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/applications/public", headers=_coolify_headers(), json=payload)
        r.raise_for_status()
        return r.json()


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
    existing app entry (hello.zaindroid.me, hello-staging.zaindroid.me)."""
    config_path = Path("/etc/cloudflared/config.yml")
    repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"
    cfg = yaml.safe_load(repo_config_path.read_text())
    new_rule = {
        "hostname": hostname,
        "service": "https://localhost:443",
        "originRequest": {"noTLSVerify": True},
    }
    if any(r.get("hostname") == hostname for r in cfg["ingress"]):
        return  # already routed, idempotent
    cfg["ingress"].insert(-1, new_rule)  # keep the catch-all 404 rule last
    new_yaml = yaml.dump(cfg, sort_keys=False)
    repo_config_path.write_text(new_yaml)
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


def deploy(*, owner_repo: str, name: str, git_branch: str = "main", target_node: str = "servingz") -> dict:
    """Full pipeline: clone -> classify -> budget check -> either Cloudflare
    Pages (static) or Coolify (real app), DNS + registration either way.
    Raises DeployError with the exact step and reason on any failure;
    nothing partially-applied is rolled back automatically -- if a later
    step fails, earlier ones (e.g. a created Coolify app) stay in place and
    need manual cleanup. Fine for a single-operator platform; would need
    real rollback before handing this to more than one person.

    target_node picks which node from registry.yaml's `nodes` section a
    real app (not a static site -- those always go to Cloudflare Pages
    regardless of target_node) gets deployed to. Ignored for static sites.
    Raises KeyError immediately (via node_config) if target_node isn't a
    real node -- fail before cloning anything, not after."""
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

    ok, reason = step("budget_check", check_deploy_budget, name, classification["memory_mb"], target_node)
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

    memory_mb = classification["memory_mb"]
    build_pack = build_pack_for(classification["language"])

    coolify_result = step(
        "create_coolify_app", create_coolify_app,
        name=name, git_repository=f"https://github.com/{owner_repo}",
        git_branch=git_branch, build_pack=build_pack, memory_mb=memory_mb, domain=domain,
        server_uuid=node["server_uuid"],
    )

    # A node with a real public IP is reached directly (A record at its IP,
    # Coolify's own Traefik terminates TLS there) -- no Cloudflare Tunnel
    # involved, unlike servingz which has no public IP of its own.
    if node.get("has_public_ip"):
        step("create_dns_record", create_dns_record, name, target=node["ip"], record_type="A")
    else:
        step("create_dns_record", create_dns_record, name)
        step("add_tunnel_route", add_tunnel_route, domain)

    step("register_app", register_app, name=name, memory_mb=memory_mb, subdomain=name,
         repo=f"github.com/{owner_repo}", target=target_node)
    step("record_resource", record_resource, name, kind="coolify", coolify_uuid=coolify_result.get("uuid"))
    step("commit_and_push", git_commit_and_push, f"registry: add {name} (deployed via deploy agent)")

    return {
        "log": log,
        "classification": classification,
        "status": "deployed",
        "domain": domain,
        "target_node": target_node,
        "coolify_uuid": coolify_result.get("uuid"),
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
