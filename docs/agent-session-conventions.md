# Agent session conventions (operational lessons, not the app contract)

Read `AGENTS.md` first — that's the actual platform/app contract. This file
is narrower: operational lessons an agent working ON zorc itself (not an
app deployed to it) should know before touching the MCP server, the fleet,
or the registry. Written 2026-10-11 after a session that shipped
`preferred_node` placement, `add_custom_domain`, and a node-probing fix
(PRs #2/#3/#5), and scoped the Redis/Infisical work (PR #6,
`docs/infisical-plan.md`).

## Access

Any agent spawned via the Agent tool from a session that already has the
`zorc` MCP server configured inherits the exact same `mcp__zorc__*` tools
automatically — no separate token needed. A genuinely external tool/session
needs its own bearer token, self-service via `mint_client_token(name, role)`
(not admin-gated — reaching zorc-mcp at all is the real trust boundary) —
the user should mint and hold that themselves, not have an agent mint and
store it on their behalf.

## Hard operational rules, learned the hard way

- **Never self-merge a PR.** Blocked outright ("Merge Without Review") —
  open it, ask the user to merge, then pull + restart `zorc-mcp` yourself.
- **Never edit `registry.yaml` directly on the live server.** Blocked
  ("Modify Shared Resources") — always a branch + PR. It's the platform's
  single source of truth, deliberately human-reviewed.
- **Never try to generate, embed, store, or go hunting for credential
  values via your own tool calls** — this gets blocked under several
  different names (Secret-Store Writes, Credential Materialization,
  Credential Leakage, Credential Exploration) and the pattern is
  deliberate, not a bug. Generating secrets through zorc's own existing
  `generate: hex` app.yaml mechanism is fine (already-trusted path); don't
  hand-roll anything adjacent to it.
- **Check whether a file already exists before writing it wholesale.** A
  blind overwrite of `AGENTS.md` almost shipped in this same session —
  caught only because git showed it as modified, not new. Read first, or
  write a new, clearly-scoped file instead of a big shared one.
- **SSH quoting across Git-Bash → ssh → remote bash breaks on nested
  quotes.** Write any non-trivial remote script locally with Write, `scp`
  it over, then `ssh servingz-ts "python3 /tmp/script.py"`.
- **`zorc-mcp` must be restarted after any `deploy/agent.py` or
  `deploy/mcp_server.py` change**: `ssh servingz-ts "sudo systemctl restart
  zorc-mcp"`. Your own MCP tool list is cached at connection time — a
  brand-new tool won't be callable by you mid-session; test new functions
  directly via a scp'd Python script against the real `agent.py` instead.
- **Long-running tool calls (builds, teardowns) sometimes report a false
  "transport dropped mid-call" failure even when the action actually
  succeeded server-side.** Always independently verify via
  `get_app_status`/`diagnose_app`/`git log` before trusting a reported
  failure.
- Syntax-check every Python edit before committing:
  `python3 -c "import ast; ast.parse(open(path,encoding='utf-8').read())"`.
- Mirror existing patterns, don't invent new ones — `_coolify_headers()`
  and the existing PATCH/POST conventions in `agent.py` are the template
  for any new Coolify API call; `_score_node`/`_node_probe_address` are the
  template for any new placement/reachability logic.

## In-flight / recently shipped (check PR state before assuming)

- PRs #2, #3, #5 (placement `preferred_node`, `add_custom_domain`, public-IP
  node-probing fix) — merged and live-verified as of 2026-10-11.
- `docs/infisical-plan.md` (PR #6) — Redis provisioning + self-hosted
  Infisical + a `from_infisical` secret-reference layer. Read it before
  starting that work; don't re-derive the plan.
