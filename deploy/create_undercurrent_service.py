"""Create (idempotently) the undercurrent Coolify Docker Compose service on servingz.

Mirrors deploy/create_groundswell_service.py: builds the app image on the host (a raw-compose service has no build
context), pushes the compose via POST/PATCH /services, env vars from ~/apps/undercurrent/infra/.env (values never
printed; *_INTERNAL keys override their host-side twins because the app talks Docker DNS), sets the app domain,
then starts or restarts. Run from ~/zorc/deploy:  ~/zorc/monitoring/.venv/bin/python create_undercurrent_service.py
"""
import base64
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")
import agent  # noqa: E402
import httpx  # noqa: E402

NAME = "undercurrent"
HOST = "undercurrent.zaindroid.me"
REPO = Path.home() / "apps" / "undercurrent"
COMPOSE = REPO / "infra" / "docker-compose.yml"
ENV_KEYS = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "PG_HOST_PORT", "DATABASE_URL", "S3_ENDPOINT",
            "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "ZETARIS_REST", "ZETARIS_USER", "ZETARIS_PASSWORD",
            "ZETARIS_ORG_ID", "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_REASON", "MODEL_FAST", "METERLESS_APP",
            "LAG_MONTHS", "XCORR_THRESHOLD", "APP_TAG"}


def read_env() -> dict[str, str]:
    raw = {}
    for line in (REPO / "infra" / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        raw[k] = v.split(" #", 1)[0].strip()
    out = {k: v for k, v in raw.items() if k in ENV_KEYS}
    for k, v in raw.items():
        if k.endswith("_INTERNAL") and k[: -len("_INTERNAL")] in ENV_KEYS:
            base = k[: -len("_INTERNAL")]
            # ZETARIS_REST stays empty (dev mode) until it is set host-side; then the internal address is used
            if base != "ZETARIS_REST" or raw.get("ZETARIS_REST"):
                out[base] = v
    return out


def build_image() -> str:
    sha = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], text=True).strip()
    subprocess.run(["docker", "build", "-q", "-f", "agent/Dockerfile", "-t", f"undercurrent-app:{sha}",
                    "-t", "undercurrent-app:local", "."], cwd=REPO, check=True)
    print(f"image undercurrent-app:{sha}")
    return sha


def main() -> int:
    tag = build_image()
    h = agent._coolify_headers()
    node = agent.node_config("servingz")
    compose = base64.b64encode(COMPOSE.read_bytes()).decode()
    with httpx.Client(timeout=60) as c:
        existing = [s for s in c.get(f"{agent.COOLIFY_URL}/services", headers=h).json() if s.get("name") == NAME]
        if existing:
            uuid = existing[0]["uuid"]
            print(f"exists: {uuid}")
            r = c.patch(f"{agent.COOLIFY_URL}/services/{uuid}", headers=h, json={"docker_compose_raw": compose})
            print("patch compose", r.status_code, r.text[:300])
        else:
            r = c.post(f"{agent.COOLIFY_URL}/services", headers=h, json={
                "project_uuid": agent.COOLIFY_PROJECT_UUID,
                "server_uuid": node["server_uuid"],
                "environment_name": agent.COOLIFY_ENVIRONMENT_NAME,
                "name": NAME,
                "description": "Undercurrent hackathon agent: app + own Postgres; shared Zetaris/MinIO via groundswell-net",
                "docker_compose_raw": compose,
                "instant_deploy": False,
            })
            print("create", r.status_code, r.text[:500])
            r.raise_for_status()
            uuid = r.json()["uuid"]
        r = c.patch(f"{agent.COOLIFY_URL}/services/{uuid}", headers=h,
                    json={"urls": [{"name": "uc-app", "url": f"https://{HOST}:8080"}]})
        print("app url", r.status_code, r.text[:300])
        env = {**read_env(), "APP_TAG": tag}
        r = c.patch(f"{agent.COOLIFY_URL}/services/{uuid}/envs/bulk", headers=h,
                    json={"data": [{"key": k, "value": v} for k, v in env.items()]})
        print("envs bulk", r.status_code, f"{len(env)} keys" if r.is_success else r.text[:300])
        action = "restart" if existing else "start"
        r = c.post(f"{agent.COOLIFY_URL}/services/{uuid}/{action}", headers=h)
        if r.status_code == 400 and "not running" in r.text.lower():
            action, r = "start", c.post(f"{agent.COOLIFY_URL}/services/{uuid}/start", headers=h)
        print(action, r.status_code, r.text[:300])
        agent.record_resource(NAME, kind="coolify-service", coolify_uuid=uuid, domains=[HOST])
        print("uuid", uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
