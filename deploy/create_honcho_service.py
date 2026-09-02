import base64
import sys
sys.path.insert(0, '.')
import agent
import httpx

compose = base64.b64encode(open('/tmp/honcho-compose.yml', 'rb').read()).decode()
node = agent.node_config('servingz')

with httpx.Client(timeout=30) as client:
    r = client.post(f"{agent.COOLIFY_URL}/services", headers=agent._coolify_headers(), json={
        "project_uuid": agent.COOLIFY_PROJECT_UUID,
        "server_uuid": node["server_uuid"],
        "environment_name": agent.COOLIFY_ENVIRONMENT_NAME,
        "name": "honcho",
        "docker_compose_raw": compose,
        "instant_deploy": False,
    })
    print(r.status_code, r.text[:1500])
