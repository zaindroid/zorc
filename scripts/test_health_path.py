#!/usr/bin/env python3
"""test_health_path.py -- app.yaml `health_path` pipeline test. Run in CI
alongside the other scripts/test_*.py.

Mocked throughout, never touches Coolify:
  1. parse_app_yaml()'s health_path: default /health, custom value kept,
     malformed values rejected.
  2. create_coolify_app() sends the configured path to Coolify, and still
     sends /health when none is given.

Usage:
    python3 scripts/test_health_path.py
"""
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


def test_parse(agent) -> None:
    def parse(text):
        d = Path(tempfile.mkdtemp())
        if text is not None:
            (d / "app.yaml").write_text(text)
        return agent.parse_app_yaml(d)

    check("no app.yaml -> /health", parse(None)["health_path"] == "/health")
    check("app.yaml without health_path -> /health", parse("name: x\n")["health_path"] == "/health")
    check("custom health_path is kept", parse("health_path: /api/status\n")["health_path"] == "/api/status")
    for bad in ("api/status", "http://evil.example/x", "/has space", "/semi;colon", "/q?x=1", "123"):
        try:
            parse(f"health_path: {bad!r}\n")
            check(f"health_path {bad!r} is rejected", False, "no exception raised")
        except ValueError:
            check(f"health_path {bad!r} is rejected", True)


def test_create_app_payload(agent) -> None:
    payloads = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"uuid": "u"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None):
            payloads.append(json)
            return _Resp()

    orig_client, orig_headers = agent.httpx.Client, agent._coolify_headers
    agent.httpx.Client = FakeClient
    agent._coolify_headers = lambda: {}
    base = dict(name="a", git_repository="https://github.com/o/r", git_branch="main", build_pack="dockerfile",
                memory_mb=100, domain="a.example.com", server_uuid="s")
    try:
        agent.create_coolify_app(**base)
        check("default payload health_check_path is /health", payloads[-1]["health_check_path"] == "/health")
        agent.create_coolify_app(**base, health_check_path="/api/status")
        check("custom health_check_path reaches Coolify", payloads[-1]["health_check_path"] == "/api/status")
    finally:
        agent.httpx.Client, agent._coolify_headers = orig_client, orig_headers


def test_generate_strategies(agent) -> None:
    gens = agent._ENV_GENERATE_STRATEGIES
    check("generate: hex is 64 hex chars", len(gens["hex"]()) == 64)
    check("generate: hex16 is 32 hex chars", len(gens["hex16"]()) == 32 and int(gens["hex16"](), 16) >= 0)
    d = Path(tempfile.mkdtemp())
    (d / "app.yaml").write_text("env:\n  K:\n    generate: hex16\n")
    check("app.yaml accepts generate: hex16", agent.parse_app_yaml(d)["env"]["K"] == {"generate": "hex16"})


def main() -> int:
    import agent

    test_parse(agent)
    test_create_app_payload(agent)
    test_generate_strategies(agent)

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
