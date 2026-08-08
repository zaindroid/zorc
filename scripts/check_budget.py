#!/usr/bin/env python3
"""
check_budget.py — validates registry.yaml and enforces the resource budget.

Run in CI on every PR that touches registry.yaml or any app.yaml.
Exits non-zero with an explanation if the platform would be over-committed
or if two apps collide on a name, subdomain, database, or redis index.

Usage:
    python check_budget.py registry.yaml
"""

import sys
from collections import Counter

try:
    import yaml
except ImportError:
    sys.exit("pyyaml required:  pip install pyyaml")


def fail(msg):
    print(f"FAIL  {msg}")
    return 1


def main(path):
    with open(path) as f:
        reg = yaml.safe_load(f)

    errors = 0
    node = reg["node"]
    apps = reg.get("apps") or []
    infra = reg.get("infrastructure") or []

    # ---- budget -----------------------------------------------------------
    ceiling = node["usable_mb"] * node["max_utilisation"]
    app_total = sum(a.get("memory_mb", 0) for a in apps)

    print(f"node          {node['name']}")
    print(f"usable        {node['usable_mb']} MB")
    print(f"ceiling       {ceiling:.0f} MB  ({node['max_utilisation']:.0%})")
    print(f"allocated     {app_total} MB across {len(apps)} apps")
    print(f"headroom      {ceiling - app_total:.0f} MB")
    print()

    if app_total > ceiling:
        errors += fail(
            f"over budget by {app_total - ceiling:.0f} MB. "
            "Reduce an app's footprint, retire something, or add a node. "
            "Do NOT raise max_utilisation."
        )

    # Warn before it becomes a problem.
    if ceiling * 0.85 < app_total <= ceiling:
        print(f"WARN  at {app_total / ceiling:.0%} of ceiling — plan the next node.\n")

    # ---- collisions -------------------------------------------------------
    def dupes(values):
        return [v for v, n in Counter(values).items() if n > 1 and v is not None]

    for field in ("name", "subdomain"):
        for d in dupes([a.get(field) for a in apps]):
            errors += fail(f"duplicate {field}: {d!r}")

    # Databases and redis indexes may be shared only within the same repo
    # (e.g. an api and its worker), so group by repo before checking.
    by_db = {}
    for a in apps:
        db = a.get("database")
        if db:
            by_db.setdefault(db, set()).add(a.get("repo"))
    for db, repos in by_db.items():
        if len(repos) > 1:
            errors += fail(
                f"database {db!r} shared across different repos {sorted(repos)} — "
                "cross-app SQL access is forbidden"
            )

    # ---- reserved names ---------------------------------------------------
    reserved = set(reg.get("reserved_subdomains") or [])
    for a in apps:
        if a.get("subdomain") in reserved:
            errors += fail(f"{a['name']} uses reserved subdomain {a['subdomain']!r}")

    # ---- sanity -----------------------------------------------------------
    for a in apps:
        if a.get("target") == "pages" and a.get("memory_mb", 0) != 0:
            errors += fail(f"{a['name']}: target=pages must declare memory_mb: 0")
        if a.get("target") == "node" and a.get("memory_mb", 0) == 0:
            errors += fail(f"{a['name']}: target=node must declare a memory limit")
        if a.get("memory_mb", 0) > 1024:
            print(f"WARN  {a['name']} requests {a['memory_mb']} MB — justify in the PR")

    infra_total = sum(i.get("memory_mb", 0) for i in infra)
    if infra_total > node["reserved_mb"]:
        errors += fail(
            f"infrastructure declares {infra_total} MB but only "
            f"{node['reserved_mb']} MB is reserved"
        )

    print()
    if errors:
        print(f"{errors} problem(s) found.")
        return 1
    print("OK — registry valid, platform within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "registry.yaml"))
