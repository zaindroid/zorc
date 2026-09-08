#!/usr/bin/env python3
"""test_private_repo_deploy.py — private-repo Coolify deploy path. Run in
CI alongside the other scripts/test_*.py.

Covers _is_private_repo() (parses `gh api`'s output) and
create_coolify_app()'s private/public branching (right endpoint, right
payload shape) -- both via mocked subprocess/httpx, no real GitHub or
Coolify calls.

Usage:
    python3 scripts/test_private_repo_deploy.py
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def test_is_private_repo(agent) -> None:
    original_run = subprocess.run
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        stdout = "true" if "known-private/repo" in cmd[2] else "false"
        return SimpleNamespace(stdout=stdout, returncode=0)

    subprocess.run = fake_run
    try:
        check("a repo gh reports private returns True",
              agent._is_private_repo("known-private/repo") is True)
        check("a repo gh reports public returns False",
              agent._is_private_repo("some-org/public-repo") is False)
        check("uses gh api, not gh repo view, against the exact owner/repo",
              calls[-1][:3] == ["gh", "api", "repos/some-org/public-repo"], str(calls[-1]))
    finally:
        subprocess.run = original_run


def test_create_coolify_app_branching(agent) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"uuid": "fake-uuid"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    original_client = agent.httpx.Client
    agent.httpx.Client = FakeClient
    try:
        agent.create_coolify_app(
            name="pub-app", git_repository="https://github.com/x/pub-app", git_branch="main",
            build_pack="nixpacks", memory_mb=256, domain="pub-app.example.com",
            server_uuid="srv-1", private=False,
        )
        check("a public repo posts to /applications/public",
              captured["url"].endswith("/applications/public"), captured["url"])
        check("...and never includes github_app_uuid in the payload",
              "github_app_uuid" not in captured["payload"], str(captured["payload"]))

        agent.create_coolify_app(
            name="priv-app", git_repository="https://github.com/x/priv-app", git_branch="main",
            build_pack="nixpacks", memory_mb=256, domain="priv-app.example.com",
            server_uuid="srv-1", private=True,
        )
        check("a private repo posts to /applications/private-github-app",
              captured["url"].endswith("/applications/private-github-app"), captured["url"])
        check("...with the real github_app_uuid set",
              captured["payload"].get("github_app_uuid") == agent.COOLIFY_GITHUB_APP_UUID,
              str(captured["payload"]))
        check("...and environment_uuid included alongside environment_name",
              captured["payload"].get("environment_uuid") == agent.COOLIFY_ENVIRONMENT_UUID,
              str(captured["payload"]))
    finally:
        agent.httpx.Client = original_client


def main() -> int:
    import agent

    test_is_private_repo(agent)
    test_create_coolify_app_branching(agent)

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
