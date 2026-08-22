#!/usr/bin/env python3
"""backfill_owner.py — one-off migration: stamps `owner: <name>` onto every
registry.yaml app entry that doesn't already have one.

Phase 1 (ownership) makes `owner` required on every NEW entry (see
deploy/agent.py's register_app()) -- this is the one-time catch-up for
every app that was registered before that requirement existed. Idempotent:
an entry that already has an `owner` field is left untouched, so this is
safe to re-run (e.g. after adding a new app.yaml field by hand and wanting
to confirm nothing regressed).

Usage:
    python3 scripts/backfill_owner.py <owner-name> [--dry-run]

Does NOT commit -- review the diff (`git diff registry.yaml`) and commit
yourself, same as any other registry.yaml edit gets a human's eyes on it
per AGENTS.md ("Adding an app = a PR against this file").
"""
import sys
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ZORC_DIR / "registry.yaml"


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]
    if len(args) != 1:
        print(f"usage: {sys.argv[0]} <owner-name> [--dry-run]", file=sys.stderr)
        return 1
    owner = args[0]

    lines = REGISTRY_PATH.read_text().split("\n")
    out = []
    in_apps = False
    current_entry_indent = None
    stamped = 0
    total_app_entries = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if line.strip() == "apps:":
            in_apps = True
            i += 1
            continue
        if in_apps and line.lstrip().startswith("- name:"):
            total_app_entries += 1
            # Collect this entry's lines (until the next "- name:" at the
            # same indent, or dedent back out of the apps: list) to check
            # whether it already declares an owner.
            indent = len(line) - len(line.lstrip())
            entry_lines = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == "" or (len(nxt) - len(nxt.lstrip())) > indent:
                    entry_lines.append(nxt)
                    j += 1
                    continue
                break
            has_owner = any(el.strip().startswith("owner:") for el in entry_lines)
            if not has_owner:
                # Insert right after the `repo:` line if present (matches
                # register_app()'s own field ordering), else right after
                # the `- name:` line.
                insert_at = 1
                for k, el in enumerate(entry_lines[1:], start=1):
                    if el.strip().startswith("repo:"):
                        insert_at = k + 1
                        break
                # Field indent is 2 MORE than the "- name:" line's own
                # indent (the "- " prefix itself is 2 chars) -- e.g. "  -
                # name:" at indent 2 has its sibling "target:"/"repo:"
                # fields at indent 4, not 6.
                entry_lines.insert(insert_at, " " * (indent + 2) + f'owner: "{owner}"')
                stamped += 1
            out.extend(entry_lines[1:])
            i = j
            continue
        i += 1

    print(f"{stamped} entr{'y' if stamped == 1 else 'ies'} stamped with owner={owner!r} "
          f"(out of {total_app_entries} total app entries).")

    if stamped == 0:
        print("nothing to do.")
        return 0

    new_text = "\n".join(out)
    if dry_run:
        print("--dry-run: not writing. Preview:")
        print(new_text)
        return 0

    REGISTRY_PATH.write_text(new_text)
    print(f"wrote {REGISTRY_PATH}. Review with `git diff registry.yaml`, then commit yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
