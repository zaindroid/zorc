"""Create (idempotently) the groundswell-infra Coolify Docker Compose service on servingz.

Mirrors deploy/create_honcho_service.py: raw compose via POST /services, env vars pushed from
~/apps/groundswell/infra/.env (values never printed), then start. Run from ~/zorc/deploy.
"""
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
import agent  # noqa: E402
import httpx  # noqa: E402

NAME = "groundswell-infra"
REPO = Path.home() / "apps" / "groundswell"
COMPOSE = REPO / "infra" / "docker-compose.yml"
ENV_KEYS = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET",
            "POSTGIS_HOST_PORT", "MINIO_HOST_PORT", "MINIO_CONSOLE_HOST_PORT"}


def read_env() -> dict[str, str]:
    out = {}
    for line in (REPO / "infra" / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip()
        if k in ENV_KEYS:
            out[k] = v
    return out


def main() -> int:
    h = agent._coolify_headers()
    node = agent.node_config("servingz")
    with httpx.Client(timeout=60) as c:
        existing = [s for s in c.get(f"{agent.COOLIFY_URL}/services", headers=h).json() if s.get("name") == NAME]
        if existing:
            uuid = existing[0]["uuid"]
            print(f"exists: {uuid}")
            r = c.patch(f"{agent.COOLIFY_URL}/services/{uuid}", headers=h,
                        json={"docker_compose_raw": base64.b64encode(COMPOSE.read_bytes()).decode()})
            print("patch compose", r.status_code, r.text[:300])
        else:
            r = c.post(f"{agent.COOLIFY_URL}/services", headers=h, json={
                "project_uuid": agent.COOLIFY_PROJECT_UUID,
                "server_uuid": node["server_uuid"],
                "environment_name": agent.COOLIFY_ENVIRONMENT_NAME,
                "name": NAME,
                "description": "Groundswell hackathon data infra: PostGIS + MinIO (internal only)",
                "docker_compose_raw": base64.b64encode(COMPOSE.read_bytes()).decode(),
                "instant_deploy": False,
            })
            print("create", r.status_code, r.text[:500])
            r.raise_for_status()
            uuid = r.json()["uuid"]
        env = read_env()
        r = c.patch(f"{agent.COOLIFY_URL}/services/{uuid}/envs/bulk", headers=h,
                    json={"data": [{"key": k, "value": v} for k, v in env.items()]})
        print("envs bulk", r.status_code, f"{len(env)} keys" if r.is_success else r.text[:300])
        # A running service only picks up a changed compose on restart; a stopped one needs start.
        action = "restart" if existing else "start"
        r = c.post(f"{agent.COOLIFY_URL}/services/{uuid}/{action}", headers=h)
        if r.status_code == 400 and "not running" in r.text.lower():
            action, r = "start", c.post(f"{agent.COOLIFY_URL}/services/{uuid}/start", headers=h)
        print(action, r.status_code, r.text[:300])
        agent.record_resource(NAME, kind="coolify-service", coolify_uuid=uuid, domains=[])
        print("uuid", uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
