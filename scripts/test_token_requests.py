#!/usr/bin/env python3
"""test_token_requests.py — the unauthenticated token-request flow.

Covers the properties that make a credential-issuing endpoint safe to
expose without auth: a request mints nothing on its own, the role is the
admin's choice rather than the requester's, the token is collectable
exactly once and only with the claim secret, neither the secret nor the
token is ever stored, and the public bypass is narrow enough that every
other route still requires a bearer token.

Never touches the real mcp_token.json, mcp_audit.log or token_requests.json
-- all three point at throwaway temp files for the duration of this run.

Usage:
    python3 scripts/test_token_requests.py
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

# build_app()'s DNS-rebinding protection only trusts a fixed host list;
# TestClient speaks to "testserver", which otherwise 421s exactly like a
# real rebinding attempt would. Must be set before mcp_server is imported.
os.environ["ZORC_MCP_ALLOWED_HOSTS"] = "testserver"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


class FakeCtx:
    def __init__(self, bearer_token: str):
        self.headers = {"authorization": f"Bearer {bearer_token}"}


def main() -> int:
    import mcp_server as m
    from starlette.testclient import TestClient

    admin_tok, client_tok = "admtok-req", "clitok-req"
    tmp_tok = Path(tempfile.mktemp(suffix=".json"))
    tmp_tok.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "root-admin", "role": "admin"},
        hashlib.sha256(client_tok.encode()).hexdigest(): {"name": "some-client", "role": "client"},
    }))
    m.MCP_TOKEN_PATH = tmp_tok
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    m.AUDIT_LOG_PATH = Path(tempfile.mktemp(suffix=".log"))
    m.TOKEN_REQUESTS_PATH = Path(tempfile.mktemp(suffix=".json"))
    m._token_request_timestamps.clear()

    admin_ctx, client_ctx = FakeCtx(admin_tok), FakeCtx(client_tok)
    app = m.build_app()

    try:
        with TestClient(app) as http:
            # --- the endpoint is genuinely reachable without a token -------
            r = http.post("/token-request", json={
                "name": "agent-alpha", "role": "admin", "reason": "deploy the alpha app"})
            check("request accepted with no bearer token at all", r.status_code == 202,
                  f"got {r.status_code}: {r.text[:200]}")
            body = r.json()
            rid, secret = body.get("request_id"), body.get("claim_secret")
            check("returns a request id and a claim secret", bool(rid and secret))
            check("status starts pending", body.get("status") == "pending")

            # --- the bypass is narrow -------------------------------------
            r = http.get("/mcp")
            check("other routes still require a token", r.status_code == 401,
                  f"got {r.status_code}")

            # --- nothing sensitive is at rest -----------------------------
            stored = json.loads(m.TOKEN_REQUESTS_PATH.read_text())
            raw = m.TOKEN_REQUESTS_PATH.read_text()
            check("claim secret is not stored in the clear", secret not in raw)
            check("only its hash is stored",
                  stored[rid]["claim_secret_sha256"] == hashlib.sha256(secret.encode()).hexdigest())

            # --- a request alone mints nothing ----------------------------
            check("no token exists for the requester yet",
                  all(i["name"] != "agent-alpha" for i in m._load_token_map().values()))

            # --- collection requires the right secret ---------------------
            r = http.get(f"/token-request/{rid}", headers={"X-Zorc-Claim-Secret": "wrong"})
            check("wrong claim secret is refused", r.status_code == 403, f"got {r.status_code}")
            r = http.get(f"/token-request/{rid}")
            check("missing claim secret is refused", r.status_code == 401, f"got {r.status_code}")
            r = http.get(f"/token-request/{rid}", headers={"X-Zorc-Claim-Secret": secret})
            check("pending request yields no token", r.json().get("status") == "pending"
                  and "token" not in r.json())

            # --- only an admin decides ------------------------------------
            out = m.decide_token_request(client_ctx, id=rid, decision="approve", role="admin")
            check("non-admin cannot decide", out.get("status") == "rejected")

            out = m.list_token_requests(client_ctx)
            check("non-admin cannot list the queue", out.get("status") == "rejected")
            out = m.list_token_requests(admin_ctx)
            check("admin sees the pending request", out.get("pending") == 1)
            check("queue listing never leaks the secret hash",
                  "claim_secret_sha256" not in json.dumps(out))

            # --- the admin's role wins over the requested one -------------
            out = m.decide_token_request(admin_ctx, id=rid, decision="approve", role="client")
            check("admin approves, downgrading the asked-for role",
                  out.get("status") == "approved" and out.get("role_granted") == "client")

            out = m.decide_token_request(admin_ctx, id=rid, decision="reject")
            check("a decided request cannot be decided again", out.get("status") == "rejected"
                  and "already" in out.get("reason", ""))

            # --- collection, exactly once ---------------------------------
            r = http.get(f"/token-request/{rid}", headers={"X-Zorc-Claim-Secret": secret})
            collected = r.json()
            token = collected.get("token")
            check("approved request yields a token", bool(token), str(collected)[:200])
            check("granted role is the admin's, not the requester's",
                  collected.get("role") == "client")
            check("the minted token actually resolves",
                  (m._resolve_client(token) or {}).get("name") == "agent-alpha")
            check("it resolves with the granted role",
                  (m._resolve_client(token) or {}).get("role") == "client")
            check("raw token is never written to the queue file",
                  token not in m.TOKEN_REQUESTS_PATH.read_text())

            r = http.get(f"/token-request/{rid}", headers={"X-Zorc-Claim-Secret": secret})
            check("a second collection is refused", r.status_code == 410, f"got {r.status_code}")

            # --- rejection ------------------------------------------------
            m._token_request_timestamps.clear()
            r = http.post("/token-request", json={
                "name": "agent-beta", "role": "client", "reason": "nope"})
            rid2, secret2 = r.json()["request_id"], r.json()["claim_secret"]
            m.decide_token_request(admin_ctx, id=rid2, decision="reject")
            r = http.get(f"/token-request/{rid2}", headers={"X-Zorc-Claim-Secret": secret2})
            check("rejected request yields no token",
                  r.status_code == 403 and "token" not in r.json())
            check("no token was minted for the rejected request",
                  all(i["name"] != "agent-beta" for i in m._load_token_map().values()))

            # --- input validation -----------------------------------------
            m._token_request_timestamps.clear()
            bad = [
                ({"name": "bad name!", "role": "client", "reason": "x"}, "name with punctuation"),
                ({"name": "ok", "role": "superuser", "reason": "x"}, "unknown role"),
                ({"name": "ok", "role": "client", "reason": ""}, "empty reason"),
                ({"name": "", "role": "client", "reason": "x"}, "empty name"),
            ]
            for payload, label in bad:
                r = http.post("/token-request", json=payload)
                check(f"rejects {label}", r.status_code == 400, f"got {r.status_code}")
                m._token_request_timestamps.clear()

            # --- expiry ------------------------------------------------------
            r = http.post("/token-request", json={
                "name": "agent-stale", "role": "client", "reason": "will expire"})
            rid3, secret3 = r.json()["request_id"], r.json()["claim_secret"]
            aged = json.loads(m.TOKEN_REQUESTS_PATH.read_text())
            aged[rid3]["requested_at"] = time.time() - (m.TOKEN_REQUEST_TTL_SEC + 60)
            m.TOKEN_REQUESTS_PATH.write_text(json.dumps(aged))
            r = http.get(f"/token-request/{rid3}", headers={"X-Zorc-Claim-Secret": secret3})
            check("an expired request is gone", r.status_code == 404, f"got {r.status_code}")

            # --- per-IP rate limit --------------------------------------------
            m._token_request_timestamps.clear()
            codes = []
            for i in range(m.TOKEN_REQUEST_RATE_LIMIT + 2):
                codes.append(http.post("/token-request", json={
                    "name": f"flood-{i}", "role": "client", "reason": "flooding"}).status_code)
            check("rate limit kicks in for one source",
                  codes.count(202) == m.TOKEN_REQUEST_RATE_LIMIT and codes[-1] == 429,
                  f"codes={codes}")
    finally:
        for p in (tmp_tok, m.AUDIT_LOG_PATH, m.TOKEN_REQUESTS_PATH):
            try:
                Path(p).unlink()
            except OSError:
                pass

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
