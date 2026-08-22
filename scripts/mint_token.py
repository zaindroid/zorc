#!/usr/bin/env python3
"""mint_token.py — mints a new zorc-mcp bearer token for one client.

Prints the raw token ONCE (stdout); stores only its sha256 hash, plus the
client's name and role, in deploy/secrets/mcp_token.json. The raw token is
never written to disk or logged anywhere -- if it's lost, mint a new one.

Usage:
    python3 scripts/mint_token.py <name> <admin|client>

Re-minting an existing <name> replaces that client's entry (old hash
dropped, new one added) -- every OTHER client's entry, and their token,
is left untouched. This is the platform's rotation mechanism: rotating one
compromised/leaked client's token never requires touching anyone else's.

Nothing needs restarting afterward -- mcp_server.py reloads the token map
automatically on its next request (see _load_token_map's mtime check).
"""
import hashlib
import json
import secrets
import sys
from pathlib import Path

TOKEN_PATH = Path(__file__).resolve().parent.parent / "deploy" / "secrets" / "mcp_token.json"

VALID_ROLES = ("admin", "client")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in VALID_ROLES:
        print(f"usage: {sys.argv[0]} <name> <{'|'.join(VALID_ROLES)}>", file=sys.stderr)
        return 1
    name, role = sys.argv[1], sys.argv[2]

    token_map = json.loads(TOKEN_PATH.read_text()) if TOKEN_PATH.exists() else {}

    # Drop any existing entry for this name first -- re-minting replaces,
    # it doesn't add a second live token for the same client.
    before = len(token_map)
    token_map = {h: info for h, info in token_map.items() if info.get("name") != name}
    replaced = len(token_map) < before

    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_map[token_hash] = {"name": name, "role": role}

    TOKEN_PATH.write_text(json.dumps(token_map, indent=2) + "\n")

    action = "rotated" if replaced else "minted"
    print(f"{action} token for {name!r} (role={role}). This is shown ONCE -- store it now:\n")
    print(token)
    print(f"\n{TOKEN_PATH} now holds {len(token_map)} client(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
