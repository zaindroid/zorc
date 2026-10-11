#!/usr/bin/env python3
"""test_from_infisical.py -- {"from_infisical": ...} env resolution test.
Run in CI alongside the other scripts/test_*.py.

Fully mocked -- never contacts Infisical or touches real secrets:
  1. parse_infisical_ref(): path splitting, environment default, rejects
     malformed references and unsupported keys.
  2. resolve_infisical_refs(): login + one v4 fetch per reference with the
     right parameters, plain values untouched, no network when there are no
     references, failures name the reference but never leak a value or the
     client secret.
  3. resolve_env_vars(): only declared-required keys are fetched.
  4. mcp_server's authorization gate: admin may reference any project, a
     client only projects listed for them, unlisted = denied, malformed =
     denied, and the deploy/set_app_env_vars tools reject before doing
     anything and audit reference paths, never values.

Usage:
    python3 scripts/test_from_infisical.py
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


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def _fake_client(calls, login=(200, {"accessToken": "tok"}), secrets=None, fail=()):
    secrets = secrets or {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, json=None, headers=None):
            calls.append(("POST", url, json, None))
            return _Resp(*login)

        def get(self, url, headers=None, params=None):
            calls.append(("GET", url, params, headers))
            name = url.rsplit("/", 1)[1]
            if name in fail:
                return _Resp(404, {"message": "Project with ID 'p1' not found during bot lookup "
                                              "(echoing csecret and tok to test scrubbing)"})
            return _Resp(200, {"secret": {"secretValue": secrets.get(name, "v-" + name)}})

    return FakeClient


def test_parse(agent) -> None:
    p = agent.parse_infisical_ref({"from_infisical": "proj1/apps/api/STRIPE_KEY"})
    check("project/folders/NAME split", (p["project_id"], p["secret_path"], p["secret_name"]) ==
          ("proj1", "/apps/api", "STRIPE_KEY"), str(p))
    p = agent.parse_infisical_ref({"from_infisical": "proj1/TOKEN"})
    check("no folders -> path '/'", p["secret_path"] == "/" and p["secret_name"] == "TOKEN", str(p))
    check("environment defaults to prod", p["environment"] == "prod")
    p = agent.parse_infisical_ref({"from_infisical": "proj1/TOKEN", "environment": "staging"})
    check("environment override kept", p["environment"] == "staging")
    for bad in ({"from_infisical": "justaname"}, {"from_infisical": "a/b c/D"}, {"from_infisical": "a/../b"},
                {"from_infisical": 5}, {"from_infisical": "a/B", "extra": 1},
                {"from_infisical": "a/B", "environment": "pr od"}):
        try:
            agent.parse_infisical_ref(bad)
            check(f"rejects {bad!r}", False, "no exception raised")
        except agent.InfisicalRefError:
            check(f"rejects {bad!r}", True)


def test_resolve(agent) -> None:
    cfg = Path(tempfile.mktemp(suffix=".json"))
    cfg.write_text(json.dumps({"url": "https://inf.test", "client_id": "cid", "client_secret": "csecret",
                               "project_access": {"p1": ["alice"], "p2": ["alice", "bob"]}}))
    saved = (agent.INFISICAL_CONFIG_PATH, agent.httpx.Client)
    agent.INFISICAL_CONFIG_PATH = cfg
    try:
        calls = []
        agent.httpx.Client = _fake_client(calls, secrets={"STRIPE_KEY": "sk-real"})
        out = agent.resolve_infisical_refs({
            "PLAIN": "literal",
            "STRIPE": {"from_infisical": "p1/billing/STRIPE_KEY", "environment": "prod"},
        })
        check("plain value passes through", out["PLAIN"] == "literal")
        check("reference replaced by the real value", out["STRIPE"] == "sk-real", str(out))
        login = [c for c in calls if c[0] == "POST"]
        check("logs in once via universal-auth", len(login) == 1 and
              login[0][1] == "https://inf.test/api/v1/auth/universal-auth/login", str(calls))
        check("login sends client id/secret",
              login[0][2] == {"clientId": "cid", "clientSecret": "csecret"})
        get = [c for c in calls if c[0] == "GET"][0]
        check("fetches the v4 secret endpoint", get[1] == "https://inf.test/api/v4/secrets/STRIPE_KEY", get[1])
        check("passes projectId/environment/secretPath",
              get[2] == {"projectId": "p1", "environment": "prod", "secretPath": "/billing"}, str(get[2]))
        check("uses the bearer access token", get[3] == {"Authorization": "Bearer tok"}, str(get[3]))

        calls.clear()
        out = agent.resolve_infisical_refs({"A": "x"})
        check("no references -> no network, unchanged", out == {"A": "x"} and calls == [])

        calls.clear()
        agent.httpx.Client = _fake_client(calls, fail=("MISSING",))
        try:
            agent.resolve_infisical_refs({"K": {"from_infisical": "p1/MISSING"}})
            check("missing secret raises", False, "no exception raised")
        except agent.InfisicalRefError as e:
            msg = str(e)
            check("missing secret raises, naming the reference and status",
                  "p1/MISSING" in msg and "404" in msg, msg)
            check("error includes Infisical's own message", "not found during bot lookup" in msg, msg)
            check("error never leaks the client secret or access token",
                  "csecret" not in msg and "tok" not in msg, msg)

        agent.httpx.Client = _fake_client(calls, login=(401, {}))
        try:
            agent.resolve_infisical_refs({"K": {"from_infisical": "p1/X"}})
            check("failed login raises", False, "no exception raised")
        except agent.InfisicalRefError as e:
            check("failed login raises without the client secret", "login failed" in str(e)
                  and "csecret" not in str(e), str(e))

        check("project_access lists owner's projects",
              agent.infisical_project_access("alice") == {"p1", "p2"} and
              agent.infisical_project_access("bob") == {"p2"} and
              agent.infisical_project_access("mallory") == set())

        agent.INFISICAL_CONFIG_PATH = Path("/nonexistent/infisical.json")
        try:
            agent.resolve_infisical_refs({"K": {"from_infisical": "p1/X"}})
            check("unconfigured server raises", False, "no exception raised")
        except agent.InfisicalRefError as e:
            check("unconfigured server raises clearly", "not configured" in str(e), str(e))
        check("unconfigured server grants no project access", agent.infisical_project_access("alice") == set())
    finally:
        agent.INFISICAL_CONFIG_PATH, agent.httpx.Client = saved
        cfg.unlink(missing_ok=True)


def test_resolve_env_vars(agent) -> None:
    cfg = Path(tempfile.mktemp(suffix=".json"))
    cfg.write_text(json.dumps({"client_id": "c", "client_secret": "s"}))
    repo = Path(tempfile.mkdtemp())
    (repo / "app.yaml").write_text("env:\n  NEEDED:\n    required: true\n  GEN:\n    generate: hex\n")
    saved = (agent.INFISICAL_CONFIG_PATH, agent.httpx.Client)
    agent.INFISICAL_CONFIG_PATH = cfg
    try:
        calls = []
        agent.httpx.Client = _fake_client(calls, secrets={"A": "resolved-a"})
        vars_, missing = agent.resolve_env_vars(repo, {
            "NEEDED": {"from_infisical": "p/A"},
            "UNUSED": {"from_infisical": "p/B"},
        })
        check("declared required key is resolved", vars_.get("NEEDED") == "resolved-a", str(vars_))
        gets = [c[1] for c in calls if c[0] == "GET"]
        check("undeclared keys are never fetched", gets == [agent.INFISICAL_DEFAULT_URL + "/api/v4/secrets/A"], str(gets))
        check("nothing missing, generated key present", missing == [] and len(vars_["GEN"]) == 64)
    finally:
        agent.INFISICAL_CONFIG_PATH, agent.httpx.Client = saved
        cfg.unlink(missing_ok=True)


def test_gate(agent, m) -> None:
    admin_tok, alice_tok = "admintokI", "alicetokI"
    tokens = Path(tempfile.mktemp(suffix=".json"))
    tokens.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "root-admin", "role": "admin"},
        hashlib.sha256(alice_tok.encode()).hexdigest(): {"name": "alice", "role": "client"},
    }))
    cfg = Path(tempfile.mktemp(suffix=".json"))
    cfg.write_text(json.dumps({"client_id": "c", "client_secret": "s", "project_access": {"p1": ["alice"]}}))
    audit = Path(tempfile.mktemp(suffix=".log"))

    saved = dict(tokens_path=m.MCP_TOKEN_PATH, audit=m.AUDIT_LOG_PATH, cfg=agent.INFISICAL_CONFIG_PATH,
                 reg=agent.load_registry, setenv=agent.set_app_env_vars)
    m.MCP_TOKEN_PATH = tokens
    m._TOKEN_CACHE = {"mtime": None, "map": {}}
    m.AUDIT_LOG_PATH = audit
    agent.INFISICAL_CONFIG_PATH = cfg
    agent.load_registry = lambda: {"apps": [{"name": "app-x", "owner": "alice", "target": "servingz"}]}
    set_calls = []
    agent.set_app_env_vars = lambda name, env_vars: (set_calls.append((name, env_vars)),
                                                      {"updated": name, "keys": sorted(env_vars)})[1]
    m._set_env_timestamps.clear()
    try:
        alice = {"name": "alice", "role": "client"}
        admin = {"name": "root-admin", "role": "admin"}
        ok_ref = {"K": {"from_infisical": "p1/SECRET"}}
        bad_ref = {"K": {"from_infisical": "p9/SECRET"}}
        check("client may use a project listed for them", m._infisical_refs_denied(alice, ok_ref) is None)
        check("client denied an unlisted project", m._infisical_refs_denied(alice, bad_ref) is not None)
        check("admin may use any project", m._infisical_refs_denied(admin, bad_ref) is None)
        check("plain values never need access", m._infisical_refs_denied(alice, {"K": "v"}) is None)
        check("malformed reference is denied",
              m._infisical_refs_denied(admin, {"K": {"from_infisical": "nope"}}) is not None)

        r = m.set_app_env_vars(FakeCtx(alice_tok), "app-x", bad_ref)
        check("set_app_env_vars rejects an unpermitted reference",
              r.get("status") == "rejected" and "p9" in r.get("reason", ""), str(r))
        check("...and never reached agent.set_app_env_vars", set_calls == [], str(set_calls))

        r = m.set_app_env_vars(FakeCtx(alice_tok), "app-x", ok_ref)
        check("set_app_env_vars passes a permitted reference to the agent",
              r.get("status") == "updated" and set_calls == [("app-x", ok_ref)], f"{r} {set_calls}")

        log = audit.read_text()
        check("audit log records the reference path", "p1/SECRET" in log)
        check("audit log never contains a resolved value or client secret",
              "sk-" not in log and '"s"' not in log)
    finally:
        m.MCP_TOKEN_PATH, m.AUDIT_LOG_PATH = saved["tokens_path"], saved["audit"]
        agent.INFISICAL_CONFIG_PATH = saved["cfg"]
        agent.load_registry, agent.set_app_env_vars = saved["reg"], saved["setenv"]
        for p in (tokens, cfg, audit):
            p.unlink(missing_ok=True)


def main() -> int:
    import agent
    import mcp_server as m

    test_parse(agent)
    test_resolve(agent)
    test_resolve_env_vars(agent)
    test_gate(agent, m)

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
