#!/usr/bin/env python3
"""test_owner_budgets.py — Phase 5 pipeline test (soft per-owner memory
budgets). Run in CI alongside the other scripts/test_*.py.

Never clones a real repo or calls a real Coolify/GitHub API -- agent.
clone_repo/classify/parse_app_yaml and mcp_server's own
_estimate_memory_from_repo/_recommend_placement are all monkeypatched to
deterministic fixtures, so this drives analyze_deployment_requirements()'s
real code path end-to-end without any external dependency.

Usage:
    python3 scripts/test_owner_budgets.py
"""
import sys
import tempfile
from pathlib import Path

import yaml

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


class FakeCtx:
    def __init__(self, name: str, role: str):
        # analyze_deployment_requirements resolves identity via
        # _caller_identity(ctx), same as every other tool -- but that
        # function hashes a bearer token against the real token map. For
        # this test we bypass that entirely by monkeypatching
        # _caller_identity itself (see main()) rather than standing up a
        # real token file, since this test is only exercising the budget
        # arithmetic, not auth (already covered by test_mcp_auth.py).
        self.name, self.role = name, role


def test_owner_memory_total_and_budget(agent) -> None:
    original_load_registry = agent.load_registry
    try:
        agent.load_registry = lambda: {
            "owner_budgets": {"default_mb": 8192, "overrides": {"big-client": 20000}},
            "apps": [
                {"name": "a1", "owner": "multi-app-client", "memory_mb": 512},
                {"name": "a2", "owner": "multi-app-client", "memory_mb": 1024},
                {"name": "a3", "owner": "multi-app-client", "memory_mb": 256},
                {"name": "a4", "owner": "someone-else", "memory_mb": 99999},
            ],
        }
        check("owner_memory_total_mb sums correctly across multiple owned apps",
              agent.owner_memory_total_mb("multi-app-client") == 512 + 1024 + 256,
              str(agent.owner_memory_total_mb("multi-app-client")))
        check("owner_memory_total_mb ignores other owners' apps entirely",
              agent.owner_memory_total_mb("nobody-owns-this") == 0,
              str(agent.owner_memory_total_mb("nobody-owns-this")))
        check("owner_budget_mb returns default_mb when no override exists",
              agent.owner_budget_mb("multi-app-client") == 8192, str(agent.owner_budget_mb("multi-app-client")))
        check("owner_budget_mb returns the override when one exists",
              agent.owner_budget_mb("big-client") == 20000, str(agent.owner_budget_mb("big-client")))
    finally:
        agent.load_registry = original_load_registry


def test_analyze_deployment_requirements_budget_gate(agent, m) -> None:
    original = {
        "load_registry": agent.load_registry,
        "clone_repo": agent.clone_repo,
        "classify": agent.classify,
        "parse_app_yaml": agent.parse_app_yaml,
        "estimate": m._estimate_memory_from_repo,
        "placement": m._recommend_placement,
        "caller_identity": m._caller_identity,
        "rmtree": __import__("shutil").rmtree,
    }

    fixture_registry = {"owner_budgets": {"default_mb": 8192, "overrides": {"roomy-client": 20000}}, "apps": []}

    def set_owner_apps(owner: str, total_mb: int):
        fixture_registry["apps"] = [{"name": f"{owner}-existing", "owner": owner, "memory_mb": total_mb}] if total_mb else []

    agent.load_registry = lambda: fixture_registry
    agent.clone_repo = lambda owner_repo, git_branch="main": Path("/tmp/fake-repo-does-not-need-to-exist")
    agent.classify = lambda repo_dir: {"kind": "python", "language": "python", "reason": "requirements.txt found"}
    agent.parse_app_yaml = lambda repo_dir: {"env": {}, "database": False, "ai": False, "persistent_storage": None}
    m._estimate_memory_from_repo = lambda repo_dir, classification: (512, [])
    m._recommend_placement = lambda memory_mb, needs_public_ip, needs_gpu=False: {
        "recommended_node": "servingz", "fits": True, "reason": "plenty of headroom",
        "candidates_considered": {},
    }
    __import__("shutil").rmtree = lambda *a, **kw: None

    def call(name: str, role: str) -> dict:
        m._caller_identity = lambda ctx: {"name": name, "role": role}
        return m.analyze_deployment_requirements(
            ctx=object(),  # unused -- _caller_identity is monkeypatched above
            owner_repo="someorg/somerepo", architecture="single_service", app_kind="api",
            frontend_rendering="none", framework="fastapi", expected_concurrency="low",
            has_database=False, needs_ai=False, has_background_jobs=False, needs_websockets=False,
            needs_persistent_storage=False, needs_public_ip=False, needs_gpu=False,
            estimated_memory_mb=512, reasoning="a plain FastAPI service, 512MB is the standard baseline for this",
        )

    try:
        # --- client well under their cap: approved ---
        set_owner_apps("fresh-client", 0)
        r = call("fresh-client", "client")
        check("client with no existing apps and a small estimate is approved",
              r.get("status") == "approved", f"got {r}")

        # --- client whose existing total + new estimate exceeds the default cap: blocked ---
        set_owner_apps("near-cap-client", 7800)  # + 512 new = 8312 > 8192 default
        r = call("near-cap-client", "client")
        check("client pushed over their cap by this deploy is blocked",
              r.get("status") == "blocked", f"got {r}")
        check("...with a readable reason naming the actual numbers",
              all(str(n) in r.get("reason", "") for n in (7800, 512, 8312, 8192)), f"got {r}")
        check("...and reports the correct current/cap numbers as structured fields",
              r.get("owner_current_total_mb") == 7800 and r.get("owner_budget_mb") == 8192, f"got {r}")

        # --- same numbers, but this owner has a raised override: approved ---
        set_owner_apps("roomy-client", 7800)
        r = call("roomy-client", "client")
        check("a client with a raised override is NOT blocked at the same total",
              r.get("status") == "approved", f"got {r}")

        # --- admin is exempt regardless of how much they already own ---
        set_owner_apps("zainey", 999999)
        r = call("zainey", "admin")
        check("admin is exempt from the budget check entirely, no matter their existing total",
              r.get("status") == "approved", f"got {r}")

        # --- exactly at the cap (not over) should be approved, not blocked ---
        set_owner_apps("exact-client", 8192 - 512)  # + 512 new = exactly 8192
        r = call("exact-client", "client")
        check("landing exactly ON the cap is approved, not blocked (over, not at-or-over)",
              r.get("status") == "approved", f"got {r}")
    finally:
        agent.load_registry = original["load_registry"]
        agent.clone_repo = original["clone_repo"]
        agent.classify = original["classify"]
        agent.parse_app_yaml = original["parse_app_yaml"]
        m._estimate_memory_from_repo = original["estimate"]
        m._recommend_placement = original["placement"]
        m._caller_identity = original["caller_identity"]
        __import__("shutil").rmtree = original["rmtree"]


def test_agent_set_owner_budget(agent) -> None:
    # Real file, real text surgery -- not mocked, since this is exactly
    # the kind of edit that silently corrupts on a wrong marker or a bad
    # regex (see write_registry()'s bare "apps:" bug for why this repo
    # tests these against a real temp file instead of trusting the logic
    # by inspection).
    original_path = agent.REGISTRY_PATH
    tmp = Path(tempfile.mktemp(suffix=".yaml"))
    tmp.write_text(
        "owner_budgets:\n"
        "  default_mb: 8192\n"
        "  overrides: {}\n"
        "apps: []\n"
    )
    agent.REGISTRY_PATH = tmp
    try:
        agent.set_owner_budget("new-user", 4096)
        reg = yaml.safe_load(tmp.read_text())
        check("first override on an empty {} lands correctly",
              reg["owner_budgets"]["overrides"] == {"new-user": 4096}, str(reg))

        agent.set_owner_budget("second-user", 2048)
        reg = yaml.safe_load(tmp.read_text())
        check("a second override is added alongside the first, not replacing it",
              reg["owner_budgets"]["overrides"] == {"new-user": 4096, "second-user": 2048}, str(reg))

        agent.set_owner_budget("new-user", 8000)
        reg = yaml.safe_load(tmp.read_text())
        check("re-setting an existing owner replaces their value, doesn't duplicate the key",
              reg["owner_budgets"]["overrides"] == {"new-user": 8000, "second-user": 2048}, str(reg))

        raised = False
        try:
            agent.set_owner_budget("bad", 0)
        except ValueError:
            raised = True
        check("zero or negative memory_mb is rejected", raised)
    finally:
        agent.REGISTRY_PATH = original_path
        tmp.unlink(missing_ok=True)


def test_mcp_set_owner_budget_and_list_my_apps(agent, m) -> None:
    original = {
        "load_registry": agent.load_registry,
        "app_status": agent.app_status,
        "owner_memory_total_mb": agent.owner_memory_total_mb,
        "owner_budget_mb": agent.owner_budget_mb,
        "set_owner_budget": agent.set_owner_budget,
        "git_commit_and_push": agent.git_commit_and_push,
        "caller_identity": m._caller_identity,
        "enabled": m.RATE_LIMITS_ENABLED,
    }
    m.RATE_LIMITS_ENABLED = True
    fixture_registry = {
        "owner_budgets": {"default_mb": 8192, "overrides": {}},
        "apps": [
            {"name": "mine-a", "owner": "portal-user", "memory_mb": 256},
            {"name": "mine-b", "owner": "portal-user", "memory_mb": 512},
            {"name": "someone-elses-app", "owner": "other-user", "memory_mb": 999},
        ],
    }
    agent.load_registry = lambda: fixture_registry
    agent.app_status = lambda name: {"name": name, "status": "running"}
    pushed_messages = []
    agent.git_commit_and_push = lambda message: pushed_messages.append(message)
    set_calls = []
    agent.set_owner_budget = lambda owner, mb: set_calls.append((owner, mb))

    def call_set(name: str, role: str, owner: str, memory_mb: int) -> dict:
        m._caller_identity = lambda ctx: {"name": name, "role": role}
        return m.set_owner_budget(ctx=object(), owner=owner, memory_mb=memory_mb)

    def call_list(name: str, role: str) -> dict:
        m._caller_identity = lambda ctx: {"name": name, "role": role}
        return m.list_my_apps(ctx=object())

    try:
        r = call_set("portal-user", "client", "portal-user", 4096)
        check("set_owner_budget refuses a non-admin caller",
              r.get("status") == "rejected" and "admin" in r.get("reason", ""), f"got {r}")
        check("...and never calls agent.set_owner_budget when refused", set_calls == [])

        m._set_owner_budget_timestamps.clear()
        r = call_set("zainey", "admin", "new-portal-user", 4096)
        check("set_owner_budget succeeds for an admin caller",
              r.get("status") == "set" and set_calls == [("new-portal-user", 4096)], f"got {r}, calls={set_calls}")
        check("...and commits+pushes the change", len(pushed_messages) == 1, str(pushed_messages))

        r = call_list("portal-user", "client")
        names = sorted(a["name"] for a in r["apps"])
        check("list_my_apps returns only the caller's own apps", names == ["mine-a", "mine-b"], f"got {names}")
        check("...never another owner's app", "someone-elses-app" not in names)
        check("...and reports current total + budget for that owner",
              r["owner_current_total_mb"] == 768 and r["owner_budget_mb"] == 8192, f"got {r}")

        r = call_list("nobody-owns-anything", "client")
        check("an owner with no apps gets an empty list, not an error", r["apps"] == [], f"got {r}")
    finally:
        agent.load_registry = original["load_registry"]
        agent.app_status = original["app_status"]
        agent.owner_memory_total_mb = original["owner_memory_total_mb"]
        agent.owner_budget_mb = original["owner_budget_mb"]
        agent.set_owner_budget = original["set_owner_budget"]
        agent.git_commit_and_push = original["git_commit_and_push"]
        m._caller_identity = original["caller_identity"]
        m.RATE_LIMITS_ENABLED = original["enabled"]


def main() -> int:
    import agent
    import mcp_server as m

    test_owner_memory_total_and_budget(agent)
    test_analyze_deployment_requirements_budget_gate(agent, m)
    test_agent_set_owner_budget(agent)
    test_mcp_set_owner_budget_and_list_my_apps(agent, m)

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
