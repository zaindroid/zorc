"""Create (idempotently) the lab-mosca-pg Coolify Docker Compose service on servingz: Mosca's own Postgres on
groundswell-net, plus lab-mosca-crtsh, the dblink gateway to crt.sh for Zetaris source crtsh_pg (compose in
stacks/lab-mosca/pg-compose.yml). Passwords come from ~/apps/openhack-mosca/infra/.env (MOSCA_PG_PASSWORD,
MOSCA_CRTSH_PASSWORD; never printed).
Run from ~/zorc/deploy:  ~/zorc/monitoring/.venv/bin/python create_lab_mosca_pg.py"""
import base64
import sys
from pathlib import Path

sys.path.insert(0, ".")
import agent  # noqa: E402
import httpx  # noqa: E402

NAME = "lab-mosca-pg"
COMPOSE = Path(__file__).resolve().parents[1] / "stacks" / "lab-mosca" / "pg-compose.yml"
ENV = Path.home() / "apps" / "openhack-mosca" / "infra" / ".env"


def main() -> int:
    env = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l)
    pw, crtsh_pw = env["MOSCA_PG_PASSWORD"].strip(), env["MOSCA_CRTSH_PASSWORD"].strip()
    h = agent._coolify_headers()
    node = agent.node_config("servingz")
    compose = base64.b64encode(COMPOSE.read_bytes()).decode()
    with httpx.Client(timeout=60) as c:
        existing = [s for s in c.get(f"{agent.COOLIFY_URL}/services", headers=h).json() if s.get("name") == NAME]
        if existing:
            uuid = existing[0]["uuid"]
            r = c.patch(f"{agent.COOLIFY_URL}/services/{uuid}", headers=h, json={"docker_compose_raw": compose})
            print("patch compose", r.status_code, r.text[:300])
        else:
            r = c.post(f"{agent.COOLIFY_URL}/services", headers=h, json={
                "project_uuid": agent.COOLIFY_PROJECT_UUID, "server_uuid": node["server_uuid"],
                "environment_name": agent.COOLIFY_ENVIRONMENT_NAME, "name": NAME,
                "description": "Mosca (openhack idea/mosca) own Postgres, database mosca; Zetaris source mosca_pg via groundswell-net",
                "docker_compose_raw": compose, "instant_deploy": False})
            print("create", r.status_code, r.text[:300])
            r.raise_for_status()
            uuid = r.json()["uuid"]
        r = c.patch(f"{agent.COOLIFY_URL}/services/{uuid}/envs/bulk", headers=h, json={"data": [{"key": "MOSCA_PG_PASSWORD", "value": pw}, {"key": "MOSCA_CRTSH_PASSWORD", "value": crtsh_pw}]})
        print("envs", r.status_code)
        action = "restart" if existing else "start"
        r = c.post(f"{agent.COOLIFY_URL}/services/{uuid}/{action}", headers=h)
        print(action, r.status_code, r.text[:200])
        agent.record_resource(NAME, kind="coolify-service", coolify_uuid=uuid, domains=[])
        print("uuid", uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
