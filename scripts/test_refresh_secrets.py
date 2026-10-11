#!/usr/bin/env python3
"""test_refresh_secrets.py -- recording from_infisical references and
refresh_app_secrets(). Run in CI alongside the other scripts/test_*.py.

Fully mocked: never contacts Infisical, Coolify, or the real
resource_map.json.

  1. set_app_env_vars() records references (path only) and forgets a key
     later set to a literal.
  2. refresh_app_secrets(): re-resolves only recorded references, sets them,
     redeploys; refuses missing app / wrong kind / no uuid / no references.
  3. The MCP tool: owner-or-admin gate, authorization re-checked at refresh
     time, rate limit, and the audit log holds reference paths, never values.

Usage:
    python3 scripts/test_refresh_secrets.py
"""
import hashlib
import json
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


class FakeCtx:
    def __init__(self, bearer_token: str):
        self.headers = {"authorization": f"Bearer {bearer_token}"}


def _setup_agent(agent, resource_map: dict):
    """Point resource_map + Coolify/Infisical calls at in-memory fakes.
    Returns (calls, restore)."""
    path = Path(tempfile.mktemp(suffix=".json"))
    path.write_text(json.dumps(resource_map))
    saved = dict(path=agent.RESOURCE_MAP_PATH, taken=agent.name_taken, setvars=agent.set_coolify_env_vars,
                 trigger=agent.trigger_coolify_deploy, resolve=agent.resolve_infisical_refs)
    calls = {"set": [], "trigger": [], "resolve": []}
    agent.RESOURCE_MAP_PATH = path
    agent.name_taken = lambda n: n == "myapp"
    agent.set_coolify_env_vars = lambda uuid, vars_: calls["set"].append((uuid, dict(vars_)))
    agent.trigger_coolify_deploy = lambda uuid: calls["trigger"].append(uuid)

    def fake_resolve(values):
        calls["resolve"].append(dict(values))
        return {k: ("REAL-" + k if agent.is_infisical_ref(v) else v) for k, v in values.items()}

    agent.resolve_infisical_refs = fake_resolve

    def restore():
        agent.RESOURCE_MAP_PATH, agent.name_taken = saved["path"], saved["taken"]
        agent.set_coolify_env_vars, agent.trigger_coolify_deploy = saved["setvars"], saved["trigger"]
        agent.resolve_infisical_refs = saved["resolve"]
        path.unlink(missing_ok=True)

    return calls, restore, path


def test_agent(agent) -> None:
    ref_a = {"from_infisical": "p1/KEY_A", "environment": "prod"}
    ref_b = {"from_infisical": "p1/KEY_B"}
    calls, restore, path = _setup_agent(agent, {"myapp": {"kind": "coolify", "coolify_uuid": "u-1"}})
    try:
        agent.set_app_env_vars("myapp", {"A": ref_a, "LITERAL": "plain"})
        recorded = agent.get_recorded_infisical_refs("myapp")
        check("set_app_env_vars records the reference", recorded == {"A": ref_a}, str(recorded))
        check("literal values are never recorded", "LITERAL" not in recorded)
        check("recorded file holds no resolved value", "REAL-" not in path.read_text())

        agent.set_app_env_vars("myapp", {"B": ref_b})
        check("a second call adds, not replaces", set(agent.get_recorded_infisical_refs("myapp")) == {"A", "B"})

        agent.set_app_env_vars("myapp", {"A": "now-a-literal"})
        check("a key later set to a literal is forgotten",
              set(agent.get_recorded_infisical_refs("myapp")) == {"B"},
              str(agent.get_recorded_infisical_refs("myapp")))

        calls["set"].clear()
        calls["trigger"].clear()
        calls["resolve"].clear()
        r = agent.refresh_app_secrets("myapp")
        check("refresh re-resolves only the recorded reference",
              calls["resolve"] == [{"B": ref_b}], str(calls["resolve"]))
        check("refresh pushes the fresh value and redeploys",
              calls["set"] == [("u-1", {"B": "REAL-B"})] and calls["trigger"] == ["u-1"], str(calls))
        check("refresh reports key names only", r.get("keys") == ["B"] and "REAL-" not in json.dumps(r), str(r))
    finally:
        restore()

    for label, rmap, expect in (
        ("unregistered app", {}, "not a registered app"),
        ("wrong kind", {"myapp": {"kind": "zorc-agent", "coolify_uuid": "u"}}, "only supports"),
        ("no uuid", {"myapp": {"kind": "coolify"}}, "no recorded coolify_uuid"),
        ("no references", {"myapp": {"kind": "coolify", "coolify_uuid": "u"}}, "no recorded from_infisical"),
    ):
        calls, restore, _ = _setup_agent(agent, rmap)
        try:
            if label == "unregistered app":
                agent.name_taken = lambda n: False
            try:
                agent.refresh_app_secrets("myapp")
                check(f"refresh refuses: {label}", False, "no exception raised")
            except ValueError as e:
                check(f"refresh refuses: {label}", expect in str(e), str(e))
            check(f"refresh touches nothing: {label}", calls["set"] == [] and calls["trigger"] == [])
        finally:
            restore()


def test_tool(agent, m) -> None:
    tokens = {"admintokR": ("root-admin", "admin"), "ownertokR": ("alice", "client"), "othertokR": ("bob", "client")}
    tok_path = Path(tempfile.mktemp(suffix=".json"))
    tok_path.write_text(json.dumps({
        hashlib.sha256(t.encode()).hexdigest(): {"name": n, "role": r} for t, (n, r) in tokens.items()
    }))
    cfg = Path(tempfile.mktemp(suffix=".json"))
    cfg.write_text(json.dumps({"client_id": "c", "client_secret": "s", "project_access": {"p1": ["alice"]}}))
    audit = Path(tempfile.mktemp(suffix=".log"))

    calls, restore, _ = _setup_agent(agent, {
        "myapp": {"kind": "coolify", "coolify_uuid": "u-1",
                  "infisical_refs": {"K": {"from_infisical": "p1/SECRET", "environment": "prod"}}},
    })
    saved = dict(tp=m.MCP_TOKEN_PATH, audit=m.AUDIT_LOG_PATH, cfg=agent.INFISICAL_CONFIG_PATH,
                 reg=agent.load_registry)
    m.MCP_TOKEN_PATH = tok_path
    m._TOKEN_CACHE = {"mtime": None, "map": {}}
    m.AUDIT_LOG_PATH = audit
    agent.INFISICAL_CONFIG_PATH = cfg
    agent.load_registry = lambda: {"apps": [{"name": "myapp", "owner": "alice", "target": "servingz"}]}
    m._set_env_timestamps.clear()
    try:
        r = m.refresh_app_secrets(FakeCtx("othertokR"), "myapp")
        check("non-owner is refused", r.get("status") == "rejected" or r.get("ok") is False, str(r))
        check("...and nothing was resolved", calls["resolve"] == [])

        r = m.refresh_app_secrets(FakeCtx("ownertokR"), "myapp")
        check("owner may refresh", r.get("status") == "refreshed" and r.get("keys") == ["K"], str(r))
        check("refresh went through the resolver", len(calls["resolve"]) == 1)

        r = m.refresh_app_secrets(FakeCtx("admintokR"), "myapp")
        check("admin may refresh any app", r.get("status") == "refreshed", str(r))

        # owner loses access to the project since deploy time
        cfg.write_text(json.dumps({"client_id": "c", "client_secret": "s", "project_access": {"p1": ["someone-else"]}}))
        calls["resolve"].clear()
        r = m.refresh_app_secrets(FakeCtx("ownertokR"), "myapp")
        check("authorization is re-checked at refresh time",
              r.get("status") == "rejected" and "not permitted" in r.get("reason", ""), str(r))
        check("...and nothing was resolved", calls["resolve"] == [])

        m._set_env_timestamps.clear()
        cfg.write_text(json.dumps({"client_id": "c", "client_secret": "s", "project_access": {"p1": ["alice"]}}))
        results = [m.refresh_app_secrets(FakeCtx("ownertokR"), "myapp") for _ in range(m.SET_ENV_RATE_LIMIT + 2)]
        ok = sum(1 for x in results if x.get("status") == "refreshed")
        check(f"rate limit trips at exactly {m.SET_ENV_RATE_LIMIT}", ok == m.SET_ENV_RATE_LIMIT, f"ok={ok}")

        log = audit.read_text()
        check("audit log records the reference path", "p1/SECRET" in log)
        check("audit log never contains a resolved value", "REAL-" not in log)
    finally:
        restore()
        m.MCP_TOKEN_PATH, m.AUDIT_LOG_PATH = saved["tp"], saved["audit"]
        agent.INFISICAL_CONFIG_PATH, agent.load_registry = saved["cfg"], saved["reg"]
        for p in (tok_path, cfg, audit):
            p.unlink(missing_ok=True)


def main() -> int:
    import agent
    import mcp_server as m

    test_agent(agent)
    test_tool(agent, m)

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
