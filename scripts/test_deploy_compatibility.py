#!/usr/bin/env python3
"""test_deploy_compatibility.py — check_deploy_compatibility() and its
four checks. Run in CI alongside the other scripts/test_*.py.

test_npm_lockfile_compat uses REAL blylinks-crm files (fetched once via
gh api, not vendored -- pinned to specific commit SHAs so this stays
deterministic) and a real npm ci --dry-run against a real, if old, npm
version -- this is precisely the class of bug that only a real npm
invocation catches; a mock would defeat the point of the test. The
other three checks are pure static analysis against synthetic temp
repos, no network involved.

Usage:
    python3 scripts/test_deploy_compatibility.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

FAILURES: list[str] = []

# The exact commits from the real blylinks-crm incident -- bb4cee3 looked
# fixed (a real npm 11.x locally passed npm ci against it) but still
# failed on Coolify three times; cc99e25 is the actual fix, verified live
# against Coolify's own npm 10.9.0.
BLYLINKS_REPO = "zaindroid/blylinks-crm"
BLYLINKS_BROKEN_SHA = "bb4cee37bcc009e0de661b2d1e32f565946f1d69"
BLYLINKS_FIXED_SHA = "cc99e258fddfaa1405d3b6bb3026a32b5bf60155"


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _fetch_file(repo: str, path: str, ref: str, dest: Path) -> bool:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".content"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return False
    import base64
    dest.write_bytes(base64.b64decode(proc.stdout))
    return True


def test_npm_lockfile_compat(agent) -> None:
    for label, sha, expect_blocked in (
        ("known-broken commit", BLYLINKS_BROKEN_SHA, True),
        ("known-fixed commit", BLYLINKS_FIXED_SHA, False),
    ):
        workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
        try:
            ok_pkg = _fetch_file(BLYLINKS_REPO, "package.json", sha, workdir / "package.json")
            ok_lock = _fetch_file(BLYLINKS_REPO, "package-lock.json", sha, workdir / "package-lock.json")
            if not (ok_pkg and ok_lock):
                check(f"npm_lockfile_compat fetches real files for {label}", False,
                      "gh api call failed -- network or auth issue, not a real test failure")
                continue
            classification = {"language": "node", "kind": "app"}
            issues = agent._check_npm_lockfile_compat(workdir, classification)
            blocked = any(i["severity"] == "blocking" for i in issues)
            check(f"npm_lockfile_compat on {label}: blocked={expect_blocked}",
                  blocked == expect_blocked, f"issues={issues}")
            if expect_blocked:
                check(f"...and the message names the real npm version and mentions esbuild",
                      issues and agent.COOLIFY_BUILD_NPM_VERSION in issues[0]["message"]
                      and "esbuild" in issues[0]["message"].lower(), str(issues))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # Real bug found live: zorc-mcp's own systemd unit runs with
    # ProtectHome=read-only, so npm's default cache location
    # (~/.npm/_cacache) is read-only to the actual running service even
    # though it runs as a user who could normally write there --
    # discovered by calling the real MCP tool against production right
    # after deploying this check, which came back with a raw npm EROFS
    # error instead of a lockfile verdict. Confirms the fix: the
    # subprocess call always gets an isolated, real, writable cache dir
    # via npm_config_cache, and that dir doesn't leak afterward.
    workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
    try:
        ok_pkg = _fetch_file(BLYLINKS_REPO, "package.json", BLYLINKS_FIXED_SHA, workdir / "package.json")
        ok_lock = _fetch_file(BLYLINKS_REPO, "package-lock.json", BLYLINKS_FIXED_SHA, workdir / "package-lock.json")
        if ok_pkg and ok_lock:
            captured_env = {}
            original_run = subprocess.run
            def capturing_run(cmd, **kw):
                captured_env.update(kw.get("env") or {})
                return original_run(cmd, **kw)
            subprocess.run = capturing_run
            try:
                agent._check_npm_lockfile_compat(workdir, {"language": "node", "kind": "app"})
            finally:
                subprocess.run = original_run
            cache_dir = captured_env.get("npm_config_cache", "")
            check("the npm subprocess gets an explicit npm_config_cache override",
                  bool(cache_dir), f"captured env keys: {sorted(captured_env.keys())[:5]}...")
            check("...pointing at a real path outside the default ~/.npm location",
                  cache_dir and ".npm" not in cache_dir, cache_dir)
            check("...that gets cleaned up afterward, not left behind",
                  cache_dir and not Path(cache_dir).exists(), cache_dir)
        else:
            check("npm cache isolation test fetches real files", False,
                  "gh api call failed -- network or auth issue, not a real test failure")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Skip conditions -- none of these should invoke npm at all, verified
    # by monkeypatching subprocess.run to blow up if called.
    workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
    try:
        original_run = subprocess.run
        def blow_up(*a, **kw):
            raise AssertionError("should not have run npm for a case with no lockfile")
        subprocess.run = blow_up
        try:
            check("no package.json/lockfile -- skipped without running npm",
                  agent._check_npm_lockfile_compat(workdir, {"language": "node", "kind": "app"}) == [])
            check("non-node language -- skipped without running npm",
                  agent._check_npm_lockfile_compat(workdir, {"language": "python", "kind": "app"}) == [])
            check("dockerfile kind -- skipped without running npm (custom npm version is the app's business)",
                  agent._check_npm_lockfile_compat(workdir, {"language": "node", "kind": "dockerfile"}) == [])
        finally:
            subprocess.run = original_run
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_nextjs_standalone_output(agent) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
    try:
        (workdir / "next.config.mjs").write_text(
            "const nextConfig = {\n  output: \"standalone\",\n};\nexport default nextConfig;\n"
        )
        issues = agent._check_nextjs_standalone_output(workdir, {"kind": "app"})
        check("flags output: \"standalone\" in next.config.mjs",
              len(issues) == 1 and issues[0]["severity"] == "blocking", str(issues))
        check("...names the actual config file in the message",
              "next.config.mjs" in issues[0]["message"], str(issues))

        check("a Dockerfile-kind app is never checked (controls its own start command)",
              agent._check_nextjs_standalone_output(workdir, {"kind": "dockerfile"}) == [])

        (workdir / "next.config.mjs").write_text("const nextConfig = {};\nexport default nextConfig;\n")
        check("a plain config with no standalone output is not flagged",
              agent._check_nextjs_standalone_output(workdir, {"kind": "app"}) == [])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_hardcoded_port(agent) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
    try:
        (workdir / "package.json").write_text(json.dumps({"scripts": {"start": "next start -p 3000"}}))
        issues = agent._check_hardcoded_port(workdir, {"kind": "app"})
        check("flags a start script hardcoding port 3000",
              len(issues) == 1 and issues[0]["severity"] == "blocking", str(issues))
        check("...names the actual wrong port in the message", "3000" in issues[0]["message"], str(issues))

        (workdir / "package.json").write_text(json.dumps({"scripts": {"start": "next start -p 8080"}}))
        check("port 8080 explicitly is NOT flagged (already correct)",
              agent._check_hardcoded_port(workdir, {"kind": "app"}) == [])

        (workdir / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
        check("a start script with no -p flag at all is not flagged",
              agent._check_hardcoded_port(workdir, {"kind": "app"}) == [])

        (workdir / "package.json").write_text(json.dumps({"scripts": {"start": "next start -p 3000"}}))
        check("a Dockerfile-kind app is never checked (controls its own start command)",
              agent._check_hardcoded_port(workdir, {"kind": "dockerfile"}) == [])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_dockerfile_healthcheck_tools(agent) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
    try:
        (workdir / "Dockerfile").write_text("FROM alpine:3.20\nCOPY . /app\nCMD [\"/app/server\"]\n")
        issues = agent._check_dockerfile_healthcheck_tools(workdir, {"kind": "dockerfile"})
        check("flags a Dockerfile with neither curl nor wget",
              len(issues) == 1 and issues[0]["severity"] == "warning", str(issues))

        (workdir / "Dockerfile").write_text(
            "FROM alpine:3.20\nRUN apk add curl\nCOPY . /app\nCMD [\"/app/server\"]\n"
        )
        check("a Dockerfile with curl installed is not flagged",
              agent._check_dockerfile_healthcheck_tools(workdir, {"kind": "dockerfile"}) == [])

        (workdir / "Dockerfile").write_text("FROM alpine:3.20\nCOPY . /app\nCMD [\"/app/server\"]\n")
        check("severity is a warning, not blocking -- false-positive risk is real enough to not hard-block",
              issues[0]["severity"] == "warning")
        check("a non-dockerfile app is never checked at all",
              agent._check_dockerfile_healthcheck_tools(workdir, {"kind": "app"}) == [])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_dispatcher_aggregates_all_checks(agent) -> None:
    # A synthetic repo that trips two checks at once (standalone output +
    # hardcoded port) -- confirms check_deploy_compatibility() itself
    # actually calls and merges every check, not just wired the first one.
    workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
    try:
        (workdir / "next.config.mjs").write_text(
            "const nextConfig = {\n  output: \"standalone\",\n};\nexport default nextConfig;\n"
        )
        (workdir / "package.json").write_text(json.dumps({"scripts": {"start": "next start -p 3000"}}))
        issues = agent.check_deploy_compatibility(workdir, {"kind": "app", "language": "node"})
        found_checks = {i["check"] for i in issues}
        check("both applicable checks fire and are aggregated together",
              {"nextjs_standalone_output", "hardcoded_port"} <= found_checks, str(found_checks))

        clean = Path(tempfile.mkdtemp(prefix="compat-test-"))
        try:
            check("a repo with nothing wrong returns an empty list, not an error",
                  agent.check_deploy_compatibility(clean, {"kind": "app", "language": "go"}) == [])
        finally:
            shutil.rmtree(clean, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_deploy_refuses_on_blocking_issue(agent) -> None:
    # Exercises deploy()'s own wiring, not just the standalone checks --
    # a repo with a blocking issue must never reach create_coolify_app.
    original = {
        "clone_repo": agent.clone_repo,
        "classify": agent.classify,
        "node_config": agent.node_config,
        "create_coolify_app": agent.create_coolify_app,
    }
    workdir = Path(tempfile.mkdtemp(prefix="compat-test-"))
    (workdir / "next.config.mjs").write_text(
        "const nextConfig = {\n  output: \"standalone\",\n};\nexport default nextConfig;\n"
    )
    create_coolify_app_called = []
    try:
        agent.clone_repo = lambda owner_repo, git_branch="main": workdir
        agent.classify = lambda repo_dir: {"kind": "app", "language": "node", "memory_mb": 256,
                                            "reason": "package.json with a server script"}
        agent.node_config = lambda name: {
            "backend": "coolify", "server_uuid": "srv-1", "tailscale_ip": None,
        }
        agent.create_coolify_app = lambda **kw: create_coolify_app_called.append(kw) or {"uuid": "should-not-happen"}

        raised = False
        message = ""
        try:
            agent.deploy(owner_repo="x/blocked-app", name="blocked-app", owner="test-owner")
        except agent.DeployError as e:
            raised = True
            message = str(e)
        check("deploy() raises DeployError on a blocking compatibility issue", raised)
        check("...naming the real problem (standalone output) in the error",
              "standalone" in message.lower(), message)
        check("...and never reaches create_coolify_app at all", create_coolify_app_called == [])
    finally:
        agent.clone_repo = original["clone_repo"]
        agent.classify = original["classify"]
        agent.node_config = original["node_config"]
        agent.create_coolify_app = original["create_coolify_app"]
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    import agent

    test_npm_lockfile_compat(agent)
    test_nextjs_standalone_output(agent)
    test_hardcoded_port(agent)
    test_dockerfile_healthcheck_tools(agent)
    test_dispatcher_aggregates_all_checks(agent)
    test_deploy_refuses_on_blocking_issue(agent)

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
