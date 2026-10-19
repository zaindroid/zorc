"""Idempotently prepare undercurrent.zaindroid.me: Cloudflare Access app FIRST, then DNS + tunnel route.

Access policy is copied from the existing `status` Access app (same allow-list / session pattern as
coolify + status). The route points at the Coolify Traefik hop (https://localhost:443) like every
Coolify-managed app; Traefik serves the Coolify service undercurrent (uc-app:8080).
Run from ~/zorc/deploy:  ~/zorc/monitoring/.venv/bin/python expose_undercurrent.py
"""
import sys

sys.path.insert(0, ".")
import agent  # noqa: E402
import httpx  # noqa: E402

HOST = "undercurrent.zaindroid.me"
TEMPLATE_APP = "status.zaindroid.me"
ACC = f"{agent.CLOUDFLARE_API}/accounts/{agent.CLOUDFLARE_ACCOUNT_ID}/access/apps"


def main() -> int:
    h = agent._cloudflare_headers()
    with httpx.Client(timeout=30, headers=h) as c:
        apps = c.get(ACC).json()["result"]
        app = next((a for a in apps if a.get("domain") == HOST), None)
        if app:
            print(f"access app exists: {app['id']}")
        else:
            tpl = next(a for a in apps if a.get("domain") == TEMPLATE_APP)
            pols = c.get(f"{ACC}/{tpl['id']}/policies").json()["result"]
            body = {
                "name": "undercurrent",
                "domain": HOST,
                "type": "self_hosted",
                "session_duration": tpl.get("session_duration", "24h"),
                "app_launcher_visible": False,
                "policies": [{"name": p["name"], "decision": p["decision"], "include": p["include"],
                              "exclude": p.get("exclude", []), "require": p.get("require", []),
                              "precedence": i + 1} for i, p in enumerate(pols)],
            }
            if tpl.get("allowed_idps"):
                body["allowed_idps"] = tpl["allowed_idps"]
            r = c.post(ACC, json=body)
            j = r.json()
            print("create access app", r.status_code, j.get("errors") or j["result"]["id"])
            if not j.get("success"):
                return 1
            app = j["result"]
        # verify a policy is actually attached before opening the route
        pols = c.get(f"{ACC}/{app['id']}/policies").json()["result"]
        if not pols:
            print("refusing to route: access app has no policies")
            return 1
        print(f"access policies: {[p['name'] + ':' + p['decision'] for p in pols]}")
    try:
        agent.create_dns_record("undercurrent")
        print("dns: CNAME undercurrent -> tunnel")
    except httpx.HTTPStatusError as e:
        if "already exists" in e.response.text or "81053" in e.response.text:
            print("dns: already exists")
        else:
            raise
    agent.add_tunnel_route(HOST)
    print("tunnel: route present -> https://localhost:443 (Traefik)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
