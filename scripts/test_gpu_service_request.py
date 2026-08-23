#!/usr/bin/env python3
"""test_gpu_service_request.py — Phase 6.2 pipeline test
(request_gpu_service / approve_action / reject_action for
gpu_service_deploy). Run in CI alongside the other scripts/test_*.py.

Never touches real Coolify/SSH -- agent.deploy/agent.name_taken are
monkeypatched throughout. The property that matters most here: NOTHING
short of an admin calling approve_action() ever results in agent.deploy()
being called for a GPU service request -- not the request itself, not a
non-admin (even the requester) trying to approve it, not a duplicate
request. And separately: env_overrides values are NEVER written to the
on-disk pending-actions file, only held in memory.

Usage:
    python3 scripts/test_gpu_service_request.py
"""
import hashlib
import json
import sys
import tempfile
import time
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


def main() -> int:
    import agent
    import mcp_server as m

    admin_tok, client_tok = "admintok4", "clienttok4"
    tmp_tok = Path(tempfile.mktemp(suffix=".json"))
    tmp_tok.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "zainey", "role": "admin"},
        hashlib.sha256(client_tok.encode()).hexdigest(): {"name": "some-client", "role": "client"},
    }))
    m.MCP_TOKEN_PATH = tmp_tok
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    pending_tmp = Path(tempfile.mktemp(suffix=".json"))
    original_pending_path = m.PENDING_ACTIONS_PATH
    m.PENDING_ACTIONS_PATH = pending_tmp

    original_name_taken = agent.name_taken
    original_deploy = agent.deploy
    deploy_calls = []
    agent.name_taken = lambda name: False
    agent.deploy = lambda **kwargs: (deploy_calls.append(kwargs),
                                      {"status": "deployed", "domain": f"{kwargs['name']}.zaindroid.me"})[1]

    m._gpu_service_request_timestamps.clear()
    m._approve_action_timestamps.clear()
    m._reject_action_timestamps.clear()
    m._pending_gpu_deploy_secrets.clear()

    def make_report(needs_gpu: bool, recommended_node: str = "rtx5090") -> str:
        report_id = "rep-" + str(len(m._approved_reports))
        m._approved_reports[report_id] = {
            "report": {"needs_gpu": needs_gpu, "recommended_node": recommended_node,
                       "recommended_memory_mb": 4096, "repo_kind": "app"},
            "expires_at": time.time() + 3600,
        }
        return report_id

    try:
        # --- refuses a report with needs_gpu=False ---
        rid = make_report(needs_gpu=False)
        r = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-a", rid)
        check("refuses a report whose needs_gpu is false", r.get("status") == "rejected", f"got {r}")
        check("...without calling deploy", deploy_calls == [], f"{deploy_calls}")

        # --- refuses an unknown/expired report ---
        r = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-a", "no-such-report")
        check("refuses an unknown report_id", r.get("status") == "rejected", f"got {r}")

        # --- happy path: queues, does NOT deploy ---
        rid = make_report(needs_gpu=True)
        deploy_calls.clear()
        r = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-a", rid,
                                   env_overrides={"MODEL_API_KEY": "sk-super-secret-value"})
        check("valid GPU-needing report is queued, not deployed", r.get("status") == "requested", f"got {r}")
        check("...and deploy() is NOT called just by requesting", deploy_calls == [], f"{deploy_calls}")
        request_id = r["id"]

        # --- secrets never touch disk ---
        raw_disk_content = pending_tmp.read_text()
        check("env_overrides VALUE never appears in the on-disk pending-actions file",
              "sk-super-secret-value" not in raw_disk_content, "found secret value in pending_actions.json!")
        check("but the in-memory store DOES have it (needed for approval)",
              m._pending_gpu_deploy_secrets.get(request_id) == {"MODEL_API_KEY": "sk-super-secret-value"})

        # --- duplicate request returns existing id ---
        rid2 = make_report(needs_gpu=True)
        r2 = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-a", rid2)
        check("a second request for the same (still-pending) app name returns the existing id",
              r2.get("status") == "already_pending" and r2.get("id") == request_id, f"got {r2}")

        # --- name_taken refuses ---
        agent.name_taken = lambda name: True
        rid3 = make_report(needs_gpu=True)
        r3 = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-b", rid3)
        check("refuses if the app name already exists", r3.get("status") == "rejected", f"got {r3}")
        agent.name_taken = lambda name: False

        # --- non-admin (even the requester) cannot approve ---
        r = m.approve_action(FakeCtx(client_tok), request_id)
        check("the requester (a client, not admin) cannot approve their own GPU service request",
              r.get("status") == "rejected" and "admin-only" in r.get("reason", ""), f"got {r}")
        check("...and deploy() was never called", deploy_calls == [], f"{deploy_calls}")

        # --- admin approves: deploy() called with exactly the captured params ---
        r = m.approve_action(FakeCtx(admin_tok), request_id)
        check("admin approval executes exactly one deploy", r.get("status") == "executed" and len(deploy_calls) == 1,
              f"got {r}, calls={deploy_calls}")
        if deploy_calls:
            call = deploy_calls[0]
            check("deploy() got the requester's name as owner (not the approving admin)",
                  call.get("owner") == "some-client", str(call))
            check("deploy() got the report's recommended_node", call.get("target_node") == "rtx5090", str(call))
            check("deploy() got the report's recommended_memory_mb", call.get("memory_mb_override") == 4096, str(call))
            check("deploy() got needs_gpu=True", call.get("needs_gpu") is True, str(call))
            check("deploy() got the real env_overrides from memory",
                  call.get("env_overrides") == {"MODEL_API_KEY": "sk-super-secret-value"}, str(call))
        check("secrets are cleared from memory after a successful approval",
              request_id not in m._pending_gpu_deploy_secrets)

        # --- re-approving an already-executed id refuses, no second deploy ---
        deploy_calls.clear()
        r = m.approve_action(FakeCtx(admin_tok), request_id)
        check("re-approving an already-executed request refuses, does not re-deploy",
              r.get("status") == "rejected" and deploy_calls == [], f"got {r}")

        # --- simulated restart: env_overrides lost -> approval fails cleanly ---
        rid4 = make_report(needs_gpu=True)
        r = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-c", rid4,
                                   env_overrides={"SOME_KEY": "value"})
        request_id_2 = r["id"]
        m._pending_gpu_deploy_secrets.pop(request_id_2, None)  # simulate a restart wiping memory
        deploy_calls.clear()
        r = m.approve_action(FakeCtx(admin_tok), request_id_2)
        check("approval with lost env_overrides fails cleanly, telling the requester to submit again",
              r.get("status") == "failed" and "request_gpu_service" in r.get("reason", ""), f"got {r}")
        check("...and deploy() was never called with missing secrets", deploy_calls == [], f"{deploy_calls}")

        # --- reject_action: admin rejects, deploy never called, secrets cleared ---
        rid5 = make_report(needs_gpu=True)
        r = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-d", rid5, env_overrides={"K": "v"})
        request_id_3 = r["id"]
        check("secrets held in memory before rejection", request_id_3 in m._pending_gpu_deploy_secrets)
        deploy_calls.clear()
        rr = m.reject_action(FakeCtx(admin_tok), request_id_3)
        check("admin can reject a GPU service request", rr.get("status") == "action_rejected", f"got {rr}")
        check("...deploy() never called", deploy_calls == [], f"{deploy_calls}")
        check("...and its secrets are cleared from memory on rejection too",
              request_id_3 not in m._pending_gpu_deploy_secrets)

        # --- deploy() failure is recorded, not swallowed ---
        rid6 = make_report(needs_gpu=True)
        r = m.request_gpu_service(FakeCtx(client_tok), "org/repo", "svc-e", rid6)
        request_id_4 = r["id"]
        agent.deploy = lambda **kwargs: (_ for _ in ()).throw(agent.DeployError("create_coolify_app", "500 from Coolify"))
        r = m.approve_action(FakeCtx(admin_tok), request_id_4)
        check("a failing deploy() surfaces as status=failed, not a crash",
              r.get("status") == "failed" and "500" in r.get("reason", ""), f"got {r}")

        # --- list_pending_actions never leaks env_overrides values ---
        listing = m.list_pending_actions(FakeCtx(admin_tok))
        listing_text = json.dumps(listing)
        check("list_pending_actions never includes the distinctive secret value from earlier",
              "sk-super-secret-value" not in listing_text, "possible secret leak in list_pending_actions output")
    finally:
        agent.name_taken = original_name_taken
        agent.deploy = original_deploy
        m.AUDIT_LOG_PATH = original_audit_path
        m.PENDING_ACTIONS_PATH = original_pending_path
        m._approved_reports.clear()
        m._pending_gpu_deploy_secrets.clear()
        tmp_tok.unlink(missing_ok=True)
        audit_tmp.unlink(missing_ok=True)
        pending_tmp.unlink(missing_ok=True)

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
