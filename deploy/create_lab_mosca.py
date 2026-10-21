"""Create (idempotently) the lab-mosca Coolify application on servingz: zaindroid/openhack branch idea/mosca, root
Dockerfile, port 8080, domain mosca.zaindroid.me (Traefik; the tunnel routes the hostname to https://localhost:443 and
Cloudflare Access gates it), persistent /srv/data/memory, health /healthz.

Network: deployed to the Coolify destination `groundswell-net` (created 2026-10-20 for this app), so the container sits
on the shared hackathon network only, where it reaches Zetaris as zetaris-api-1:8888 and is reachable by Traefik
(coolify-proxy is already a member). Network alias lab-mosca; no host port.
Env comes from ~/apps/openhack-mosca/infra/.env (values never printed), with the in-network Zetaris address.
Run from ~/zorc/deploy:  ~/zorc/monitoring/.venv/bin/python create_lab_mosca.py"""
import sys
from pathlib import Path

sys.path.insert(0, ".")
import agent  # noqa: E402
import httpx  # noqa: E402

NAME = "lab-mosca"
HOST = "mosca.zaindroid.me"
DESTINATION = "anxumm5olvxni6em8r3wj08p"     # Coolify StandaloneDocker on network groundswell-net
ENV_FILE = Path.home() / "apps" / "openhack-mosca" / "infra" / ".env"
KEYS = {"ZETARIS_USER", "ZETARIS_PASSWORD", "ZETARIS_ORG_ID", "MODEL_ROUTE", "NIM_BASE_URL", "NIM_API_KEY", "NIM_REASON", "NIM_FAST",
        "METERLESS_BASE_URL", "METERLESS_API_KEY", "METERLESS_REASON", "METERLESS_FAST", "MODEL_FALLBACK_ROUTE",
        "MODEL_REASON_FALLBACK", "MODEL_THINKING", "MODEL_CONCURRENCY",
        "DEMO_APPLY_URL", "DEMO_APPLY_TOKEN", "APPLY_ALLOW", "APPLY_ENABLED"}   # demo fixture + apply guardrail (openhack docs/demo.md)
FIXED = {"APP_NAME": "lab-mosca", "METERLESS_APP": "lab-mosca", "DATA_BACKEND": "zetaris",
         "ZETARIS_TABLES": "assets=mosca_s3.assets,probes=mosca_s3.probes,data_classes=mosca_pg.data_classes,migration_paths=mosca_pg.migration_paths,scenarios=mosca_pg.scenarios",
         "ZETARIS_REST": "http://zetaris-api-1:8888/api/v1.0", "ZETARIS_PREFIX": "mosca_pg.", "MEMORY_DIR": "/srv/data/memory"}


def read_env() -> dict:
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() in KEYS and v.strip():
                out[k.strip()] = v.strip()
    return {**out, **FIXED}


def main() -> int:
    h = agent._coolify_headers()
    node = agent.node_config("servingz")
    with httpx.Client(timeout=60) as c:
        apps = c.get(f"{agent.COOLIFY_URL}/applications", headers=h).json()
        existing = [a for a in apps if a.get("name") == NAME]
        if existing:
            uuid = existing[0]["uuid"]
            print("exists", uuid)
        else:
            r = c.post(f"{agent.COOLIFY_URL}/applications/public", headers=h, json={
                "project_uuid": agent.COOLIFY_PROJECT_UUID, "server_uuid": node["server_uuid"],
                "environment_name": agent.COOLIFY_ENVIRONMENT_NAME, "destination_uuid": DESTINATION,
                "git_repository": "https://github.com/zaindroid/openhack", "git_branch": "idea/mosca",
                "build_pack": "dockerfile", "dockerfile_location": "/Dockerfile", "name": NAME,
                "description": "Mosca + Notary (openhack idea/mosca): post-quantum crypto-agility agent",
                "domains": f"https://{HOST}", "ports_exposes": "8080", "limits_memory": "768m",
                "is_auto_deploy_enabled": True, "is_force_https_enabled": True,
                "health_check_enabled": True, "health_check_path": "/healthz",
                "custom_network_aliases": NAME, "instant_deploy": False})
            print("create", r.status_code, r.text[:300])
            r.raise_for_status()
            uuid = r.json()["uuid"]
            agent.add_coolify_persistent_storage(uuid, name=f"{NAME}-memory", mount_path="/srv/data/memory")
            print("storage /srv/data/memory")
        env = read_env()
        agent.set_coolify_env_vars(uuid, env)
        print("env", len(env), "keys:", ",".join(sorted(env)))
        r = c.post(f"{agent.COOLIFY_URL}/deploy", headers=h, params={"uuid": uuid, "force": "false"})
        print("deploy", r.status_code, r.text[:300])
        agent.record_resource(NAME, kind="coolify-app", coolify_uuid=uuid, domains=[HOST])
        print("uuid", uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
