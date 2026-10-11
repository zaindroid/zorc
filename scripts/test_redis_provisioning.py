#!/usr/bin/env python3
"""test_redis_provisioning.py -- app.yaml `redis: true` pipeline test. Run
in CI alongside the other scripts/test_*.py.

Entirely mocked/monkeypatched -- never touches Coolify, SSH, Docker, or
the real registry.yaml:

  1. parse_app_yaml()'s `redis:` flag (default false, rejects non-bool).
  2. provision_dedicated_redis() on both backend heads: right Coolify
     payload / docker run shape, REDIS_URL format, and that the generated
     password never lands in command arguments (only env vars) or in an
     error message.
  3. register_app() writes `redis:` and no longer writes the dead
     `redis_db:` field.

Usage:
    python3 scripts/test_redis_provisioning.py
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def test_parse_app_yaml(agent) -> None:
    def parse(text):
        d = Path(tempfile.mkdtemp())
        if text is not None:
            (d / "app.yaml").write_text(text)
        return agent.parse_app_yaml(d)

    check("no app.yaml -> redis False", parse(None)["redis"] is False)
    check("app.yaml without redis: -> False", parse("name: x\n")["redis"] is False)
    check("redis: true -> True", parse("redis: true\n")["redis"] is True)
    check("redis: true and database: true are independent",
          (lambda p: p["redis"] and p["database"])(parse("redis: true\ndatabase: true\n")))
    try:
        parse("redis: yes-please\n")
        check("non-bool redis: is rejected", False, "no exception raised")
    except ValueError as e:
        check("non-bool redis: is rejected", "redis must be true or false" in str(e), str(e))


class _FakeResponse:
    def __init__(self, body=None):
        self._body = body or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _patch(agent, **overrides):
    saved = {k: getattr(agent, k) for k in overrides}
    for k, v in overrides.items():
        setattr(agent, k, v)
    return saved


def _restore(agent, saved):
    for k, v in saved.items():
        setattr(agent, k, v)


def test_coolify_head(agent) -> None:
    posts, subprocess_calls = [], []

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None):
            posts.append((url, json))
            return _FakeResponse({"uuid": "redis-uuid-1"})

        def get(self, url, headers=None):
            return _FakeResponse({"status": "running:healthy"})

    def fake_run(cmd, **kw):
        subprocess_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="PONG\n", stderr="")

    node = {"server_uuid": "srv-1", "backend": "coolify"}
    saved = _patch(agent, node_config=lambda n: node, _coolify_headers=lambda: {})
    orig_client, orig_run = agent.httpx.Client, agent.subprocess.run
    agent.httpx.Client = FakeClient
    agent.subprocess.run = fake_run
    try:
        container, url = agent.provision_dedicated_redis("my-app", agent.LOCAL_NODE)
        check("coolify head: container name is the database resource uuid", container == "redis-uuid-1", container)

        create = [p for p in posts if p[0].endswith("/databases/redis")]
        check("coolify head: POSTs to /databases/redis", len(create) == 1, str(posts))
        payload = create[0][1]
        check("coolify head: payload names the app's redis and the server",
              payload["name"] == "my-app-redis" and payload["server_uuid"] == "srv-1", str(payload))
        password = payload["redis_password"]
        check("coolify head: generated password is long random hex",
              re.fullmatch(r"[0-9a-f]{48}", password) is not None, password)
        check("coolify head: started after creation",
              any(p[0].endswith("/databases/redis-uuid-1/start") for p in posts), str(posts))
        check("REDIS_URL has the expected shape",
              url == f"redis://default:{password}@redis-uuid-1:6379", url)

        ping = subprocess_calls[-1]
        check("ping verifies auth with redis-cli ping", ping[-2:] == ["redis-cli", "ping"], str(ping))
        check("ping passes the password via REDISCLI_AUTH env, not as an argument",
              f"REDISCLI_AUTH={password}" in ping and not any(a == password for a in ping), str(ping))
    finally:
        agent.httpx.Client, agent.subprocess.run = orig_client, orig_run
        _restore(agent, saved)


def test_zorc_agent_head(agent) -> None:
    ssh_calls = []

    def fake_ssh_run(ip, key, cmd, user="root", timeout=20):
        ssh_calls.append(cmd)
        return 0, "container-id-123\n", ""

    node = {"backend": "zorc-agent", "tailscale_ip": "100.1.2.3", "ssh_key": "k", "ssh_user": "zman"}
    saved = _patch(
        agent, node_config=lambda n: node, _ssh_run=fake_ssh_run,
        _zorc_agent_ensure_network=lambda *a, **k: None,
        _zorc_agent_wait_healthy=lambda *a, **k: None,
    )
    orig_run = agent.subprocess.run
    agent.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="PONG\n", stderr="")
    try:
        container, url = agent.provision_dedicated_redis("edge-app", "some-agent-node")
        check("zorc-agent head: container is {app}-redis", container == "edge-app-redis", container)
        run_cmd = next(c for c in ssh_calls if c[:2] == ["docker", "run"])
        env_arg = next(a for a in run_cmd if a.startswith("REDIS_PASSWORD="))
        password = env_arg.split("=", 1)[1]
        check("zorc-agent head: password passed via -e env var", run_cmd[run_cmd.index(env_arg) - 1] == "-e")
        check("zorc-agent head: password absent from the redis-server command line",
              password not in " ".join(run_cmd[run_cmd.index("sh"):]), str(run_cmd))
        check("zorc-agent head: requires a password", "--requirepass" in " ".join(run_cmd), str(run_cmd))
        check("zorc-agent head: labelled managed-by=zorc so teardown can find it",
              "managed-by=zorc" in run_cmd, str(run_cmd))
        check("zorc-agent head: URL is on the container name",
              url == f"redis://default:{password}@edge-app-redis:6379", url)
    finally:
        agent.subprocess.run = orig_run
        _restore(agent, saved)


def test_ping_failure_redacts_password(agent) -> None:
    node = {"server_uuid": "srv-1", "backend": "coolify"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None):
            if json and "redis_password" in json:
                FakeClient.password = json["redis_password"]
            return _FakeResponse({"uuid": "u"})

        def get(self, url, headers=None):
            return _FakeResponse({"status": "running:healthy"})

    def failing_run(cmd, **kw):
        # A redis-cli that echoes the password back in its error -- worst case.
        pw = next(a.split("=", 1)[1] for a in cmd if a.startswith("REDISCLI_AUTH="))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"WRONGPASS using {pw}")

    saved = _patch(agent, node_config=lambda n: node, _coolify_headers=lambda: {})
    orig_client, orig_run, orig_sleep = agent.httpx.Client, agent.subprocess.run, agent.time.sleep
    orig_time = agent.time.time
    ticks = iter(range(0, 10_000, 20))
    agent.time.time = lambda: next(ticks)
    agent.httpx.Client = FakeClient
    agent.subprocess.run = failing_run
    agent.time.sleep = lambda s: None
    try:
        try:
            agent.provision_dedicated_redis("my-app", agent.LOCAL_NODE)
            check("failed auth ping raises", False, "no exception raised")
        except RuntimeError as e:
            check("failed auth ping raises", "authenticated PING" in str(e), str(e))
            check("error message never contains the password",
                  FakeClient.password not in str(e), str(e))
    finally:
        agent.httpx.Client, agent.subprocess.run = orig_client, orig_run
        agent.time.sleep, agent.time.time = orig_sleep, orig_time
        _restore(agent, saved)


def test_register_app(agent) -> None:
    tmp = Path(tempfile.mktemp(suffix=".yaml"))
    marker = ("# Applications. Add new entries at the end. Keep alphabetical within groups.\n"
              "# ---------------------------------------------------------------------------\napps:")
    tmp.write_text(marker + "\n")
    saved = _patch(agent, REGISTRY_PATH=tmp, name_taken=lambda n: False)
    try:
        agent.register_app(name="with-redis", memory_mb=100, subdomain="with-redis", repo="x/y",
                           owner="o", redis=True)
        agent.register_app(name="no-redis", memory_mb=100, subdomain="no-redis", repo="x/z", owner="o")
        text = tmp.read_text()
        check("register_app writes redis: true when requested", "    redis: true\n" in text)
        check("register_app writes redis: null otherwise", "    redis: null\n" in text)
        check("register_app no longer writes the dead redis_db field", "redis_db" not in text)
    finally:
        tmp.unlink(missing_ok=True)
        _restore(agent, saved)


def test_registry_has_no_dead_field() -> None:
    text = (ZORC_DIR / "registry.yaml").read_text()
    check("registry.yaml has no leftover redis_db entries", "redis_db" not in text)


def main() -> int:
    import agent

    test_parse_app_yaml(agent)
    test_coolify_head(agent)
    test_zorc_agent_head(agent)
    test_ping_failure_redacts_password(agent)
    test_register_app(agent)
    test_registry_has_no_dead_field()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
