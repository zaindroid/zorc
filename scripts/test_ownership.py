#!/usr/bin/env python3
"""test_ownership.py — Phase 1 ownership pipeline test. Run in CI alongside
scripts/test_mcp_auth.py on every PR that touches deploy/mcp_server.py or
deploy/agent.py's register_app()/deploy().

Proves _require_owner_or_admin's three required behaviors from a fixture
registry (never the real one -- agent.load_registry is monkeypatched for
the duration of this script only):

  1. A client cannot act on another client's app.
  2. An admin can act on any app, regardless of owner.
  3. Acting on a nonexistent app returns a clean structured refusal
     ({"ok": False, "reason": ...}), never an exception/stack trace.

Plus the two edge cases the docstring calls out explicitly: an app with no
`owner` field refuses every client (never treated as "anyone may act"),
and role is checked from the resolved caller dict, never trusted from
anywhere else.

Entirely self-contained -- never touches the real registry.yaml, never
starts a server process.

Usage:
    python3 scripts/test_ownership.py
"""
import sys
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


FIXTURE_REGISTRY = {
    "apps": [
        {"name": "app-a", "owner": "client-a", "target": "servingz"},
        {"name": "app-b", "owner": "client-b", "target": "servingz"},
        {"name": "app-unowned", "target": "servingz"},  # no owner field at all
        {"name": "app-empty-owner", "owner": "", "target": "servingz"},  # owner present but empty
    ]
}


def main() -> int:
    import agent
    import mcp_server as m

    original_load_registry = agent.load_registry
    agent.load_registry = lambda: FIXTURE_REGISTRY
    try:
        client_a = {"name": "client-a", "role": "client"}
        client_b = {"name": "client-b", "role": "client"}
        admin = {"name": "zainey", "role": "admin"}

        # 1. Owner acting on their own app -- allowed.
        r = m._require_owner_or_admin(client_a, "app-a")
        check("owner can act on their own app", r.get("ok") is True and r.get("app", {}).get("name") == "app-a",
              f"got {r}")

        # 2. A client cannot act on another client's app.
        r = m._require_owner_or_admin(client_a, "app-b")
        check("client A cannot act on client B's app", r.get("ok") is False, f"got {r}")
        check("...and gives a reason, not just False", bool(r.get("reason")), f"got {r}")

        r = m._require_owner_or_admin(client_b, "app-a")
        check("client B cannot act on client A's app (symmetric)", r.get("ok") is False, f"got {r}")

        # 3. Admin can act on both.
        r = m._require_owner_or_admin(admin, "app-a")
        check("admin can act on client A's app", r.get("ok") is True, f"got {r}")
        r = m._require_owner_or_admin(admin, "app-b")
        check("admin can act on client B's app", r.get("ok") is True, f"got {r}")

        # 4. Nonexistent app -- clean refusal, not a stack trace (the
        # "not a KeyError/IndexError" part is proven simply by reaching
        # this line at all -- an unhandled exception would have aborted
        # the script already).
        r = m._require_owner_or_admin(client_a, "does-not-exist")
        check("nonexistent app gives a clean refusal", r.get("ok") is False and "not a registered app" in r.get("reason", ""),
              f"got {r}")
        r = m._require_owner_or_admin(admin, "does-not-exist")
        check("nonexistent app refuses even admin (nothing to act on)", r.get("ok") is False, f"got {r}")

        # 5. An app with no owner field refuses every client -- never
        # treated as "unowned, anyone may act on it."
        r = m._require_owner_or_admin(client_a, "app-unowned")
        check("app with no owner field refuses a client", r.get("ok") is False, f"got {r}")
        r = m._require_owner_or_admin(admin, "app-unowned")
        check("app with no owner field still allows admin", r.get("ok") is True, f"got {r}")

        # 6. An app with owner: "" (present but empty) behaves the same as
        # missing -- falsy owner is never a match target.
        r = m._require_owner_or_admin(client_a, "app-empty-owner")
        check("app with empty-string owner refuses a client", r.get("ok") is False, f"got {r}")

        # 7. No substring/prefix matching -- "client-a" must not match an
        # owner of "client-a-extra" or similar.
        agent.load_registry = lambda: {"apps": [{"name": "app-c", "owner": "client-a-extra"}]}
        r = m._require_owner_or_admin(client_a, "app-c")
        check("owner match is exact, not prefix/substring", r.get("ok") is False, f"got {r}")

    finally:
        agent.load_registry = original_load_registry

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
