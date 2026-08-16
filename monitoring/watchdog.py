#!/usr/bin/env python3
"""servingz host watchdog: hardware/service checks, state-change alerting via
ntfy.sh, a healthchecks.io dead-man's-switch ping, a local status page, and
node self-registration (nodes/<name>.yaml) for a future multi-node swarm.

Run once per cycle by zorc-watchdog.timer (systemd). No long-running loop —
each invocation reads state, does its work, writes state, exits.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

import checks
import notify

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state" / "state.json"
LOG_PATH = BASE / "logs" / "watchdog.log"
STATUS_DIR = BASE / "status"
NODES_DIR = BASE.parent / "nodes"

OK, WARN, CRIT = "ok", "warn", "crit"
SEVERITY_RANK = {OK: 0, WARN: 1, CRIT: 2}


def setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("watchdog")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })

    file_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(JsonFormatter())
    log.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    log.addHandler(stream_handler)
    return log


log = setup_logging()


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"checks": {}, "streaks": {}, "last_heartbeat_date": None}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("failed to load state, starting fresh: %s", e)
        return {"checks": {}, "streaks": {}, "last_heartbeat_date": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_PATH)  # atomic


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_sustain(state: dict, results: dict, check_id: str, raw_over: bool,
                   sustain_cycles: int, escalate_to: str) -> None:
    """If raw_over has been true for >= sustain_cycles consecutive cycles,
    escalate results[check_id]'s status to escalate_to. Resets the streak
    the moment raw_over is false."""
    streaks = state.setdefault("streaks", {})
    key = f"{check_id}_streak"
    streak = (streaks.get(key, 0) + 1) if raw_over else 0
    streaks[key] = streak
    if raw_over and streak >= sustain_cycles:
        results[check_id]["status"] = escalate_to
        results[check_id]["message"] += f" (sustained {streak} cycles)"


def handle_state_change(check_id: str, result: dict, state: dict, secrets: dict) -> None:
    prev = state.setdefault("checks", {}).get(check_id, {})
    prev_status = prev.get("status", OK)
    cur_status = result["status"]

    if cur_status != prev_status:
        topic = secrets.get("ntfy_topic", "")
        name = friendly(check_id)
        if cur_status in (WARN, CRIT):
            priority = "urgent" if cur_status == CRIT else "default"
            tags = "rotating_light" if cur_status == CRIT else "warning"
            word = "Critical" if cur_status == CRIT else "Warning"
            notify.send_ntfy(topic, f"{word}: {name} — servingz",
                              result["message"], priority, tags)
        elif prev_status in (WARN, CRIT) and cur_status == OK:
            notify.send_ntfy(topic, f"Recovered: {name} — servingz",
                              result["message"], "low", "white_check_mark")
        log.info("state change: %s %s -> %s (%s)", check_id, prev_status, cur_status, result["message"])

    state["checks"][check_id] = {
        "status": cur_status,
        "since": now_iso() if cur_status != prev_status else prev.get("since", now_iso()),
        "value": result.get("value"),
        "message": result["message"],
    }


def run_all_checks(cfg: dict) -> dict[str, dict]:
    results: dict[str, dict] = {}

    cpu = checks.cpu_temp(cfg)
    results["cpu_temp"] = cpu
    gpu = checks.gpu_temp(cfg)
    results["gpu_temp"] = gpu
    for dev, r in checks.smart_all(cfg).items():
        results[f"smart_{dev}"] = r
    for path in cfg["disks"]:
        results[f"disk_{path}"] = checks.disk_usage(path, cfg)
    results["ram"] = checks.ram(cfg)
    results["swap"] = checks.swap(cfg)
    la = checks.load_avg(cfg)
    results["load_avg"] = la
    results["power_source"] = checks.power_source(cfg)

    results["docker_daemon"] = checks.docker_daemon(cfg)
    results["coolify_containers"] = checks.coolify_containers(cfg)
    results["postgres_ready"] = checks.postgres_ready(cfg)
    results["tailscaled"] = checks.tailscaled(cfg)
    results["cloudflared"] = checks.cloudflared(cfg)
    results["systemd_failed"] = checks.systemd_failed(cfg)
    results["mounts"] = checks.mounts(cfg)
    results["docker_user_port_block"] = checks.docker_user_port_block(cfg)

    for app in checks.deploy_agent.list_apps():
        results[f"app_mem_{app['name']}"] = checks.app_memory_pressure(
            app["name"], app["memory_mb"], cfg
        )

    return results, cpu, gpu, la


def write_node_yaml(cfg: dict, gpu: dict, cpu: dict, overall_status: str) -> None:
    info = checks.gather_node_info(cfg, gpu, cpu)
    info["status"] = overall_status
    info["last_seen"] = now_iso()
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    path = NODES_DIR / f"{cfg['node']['name']}.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(info, f, sort_keys=False, default_flow_style=False)
    tmp.rename(path)


def refresh_remote_nodes(cfg: dict) -> None:
    """Keeps every OTHER registered node's nodes/<name>.yaml fresh, reusing
    the exact same hardware probe gather_node_info() runs locally above --
    just reached over SSH (agent.remote_node_probe()) for nodes that don't
    run this watchdog themselves. Was a one-time manual snapshot for
    hostinger-vps before this; now part of the same periodic cycle
    servingz already self-registers through.

    One remote node being unreachable is logged and marked as such in its
    own file -- never allowed to fail this node's own self-registration
    above, which must always complete regardless."""
    reg = checks.deploy_agent.load_registry()
    local_name = cfg["node"]["name"]
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    for node_name, node_cfg in reg.get("nodes", {}).items():
        if node_name == local_name:
            continue
        path = NODES_DIR / f"{node_name}.yaml"
        existing = {}
        if path.exists():
            try:
                existing = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError:
                existing = {}
        try:
            probed = checks.deploy_agent.remote_node_probe(node_name)
            info = {
                "node": node_name,
                "tailscale_ip": node_cfg.get("tailscale_ip"),
                "arch": probed["arch"],
                "accelerator": probed["accelerator"],
                "cpu": probed["cpu"],
                "ram_mb": probed["ram_mb"],
                "power": probed["power"],
                # Hand-maintained, never returned by the hardware probe --
                # preserved across refreshes rather than wiped.
                "capabilities": existing.get("capabilities", []),
                "labels": existing.get("labels", []),
                "status": "online",
                "last_seen": now_iso(),
            }
        except Exception as e:
            log.warning("remote probe failed for node %r: %s", node_name, e)
            info = dict(existing)
            info["node"] = node_name
            info["status"] = "unreachable"
        tmp = path.with_suffix(".yaml.tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(info, f, sort_keys=False, default_flow_style=False)
        tmp.rename(path)


STATUS_COLORS = {OK: "#2e7d32", WARN: "#f9a825", CRIT: "#c62828"}


FRIENDLY_NAMES = {
    "cloudflared": "Public tunnel",
    "tailscaled": "Private network (Tailscale)",
    "docker_user_port_block": "Firewall rules",
    "disk_/": "Disk (system)",
    "disk_/mnt/data": "Disk (bulk storage)",
    "disk_/mnt/fast": "Disk (fast storage)",
    "mounts": "Storage mounts",
    "smart_/dev/sda": "Drive health (sda)",
    "smart_/dev/sdb": "Drive health (sdb)",
    "smart_/dev/nvme0n1": "Drive health (NVMe)",
    "swap": "Swap usage",
    "cpu_temp": "CPU temperature",
    "gpu_temp": "GPU temperature",
    "load_avg": "System load",
    "ram": "Memory",
    "power_source": "Power source",
    "docker_daemon": "Container engine",
    "coolify_containers": "App platform (Coolify)",
    "postgres_ready": "Shared database",
    "systemd_failed": "Background services",
}


def friendly(check_id: str) -> str:
    return FRIENDLY_NAMES.get(check_id, check_id.replace("_", " "))


def render_status_page(results: dict[str, dict], cfg: dict) -> None:
    # Presentation lives in the static status/index.html + theme.css, which
    # fetch and render this JSON client-side — this function only ever
    # needs to write the data.
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    hostname = socket.gethostname()
    now = datetime.now(timezone.utc)
    try:
        uptime_s = float(open("/proc/uptime").read().split()[0])
        uptime_str = f"{int(uptime_s // 86400)}d {int((uptime_s % 86400) // 3600)}h {int((uptime_s % 3600) // 60)}m"
    except OSError:
        uptime_str = "unknown"

    overall = OK
    for r in results.values():
        if SEVERITY_RANK[r["status"]] > SEVERITY_RANK[overall]:
            overall = r["status"]

    status_json = {
        "hostname": hostname,
        "overall": overall,
        "uptime": uptime_str,
        "last_updated": now.isoformat(),
        "checks": results,
    }
    (STATUS_DIR / "status.json.tmp").write_text(json.dumps(status_json, indent=2))
    (STATUS_DIR / "status.json.tmp").rename(STATUS_DIR / "status.json")


def maybe_send_heartbeat(state: dict, results: dict, cfg: dict, secrets: dict) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    heartbeat_time = cfg.get("heartbeat_time", "09:00")
    now_time = datetime.now().strftime("%H:%M")
    if state.get("last_heartbeat_date") == today or now_time < heartbeat_time:
        return
    issues = [f"{friendly(k)}: {v['message']}" for k, v in results.items() if v["status"] != OK]
    if issues:
        msg = f"{len(issues)} active issue(s):\n" + "\n".join(issues)
        priority, tags = "default", "warning"
    else:
        msg = "All checks green."
        priority, tags = "low", "white_check_mark"
    notify.send_ntfy(secrets.get("ntfy_topic", ""), "Daily heartbeat - servingz", msg, priority, tags)
    state["last_heartbeat_date"] = today


def main() -> int:
    cfg = load_config()
    secrets = notify.load_secrets()
    state = load_state()

    try:
        results, cpu, gpu, la = run_all_checks(cfg)
    except Exception as e:
        log.exception("check cycle crashed: %s", e)
        return 1

    apply_sustain(state, results, "cpu_temp", cpu.get("raw_over_crit", False),
                  cfg["thresholds"]["cpu_temp_crit_sustained_cycles"], CRIT)
    apply_sustain(state, results, "load_avg", la.get("raw_over", False),
                  cfg["thresholds"]["load_avg_sustained_cycles"], WARN)

    for check_id, result in list(results.items()):
        if check_id.startswith("app_mem_"):
            apply_sustain(state, results, check_id, result.get("raw_over_crit", False),
                          cfg["thresholds"]["app_memory_sustained_cycles"], CRIT)

    for check_id, result in results.items():
        handle_state_change(check_id, result, state, secrets)

    overall = OK
    for r in results.values():
        if SEVERITY_RANK[r["status"]] > SEVERITY_RANK[overall]:
            overall = r["status"]
    node_status = "online" if overall != CRIT else "degraded"
    write_node_yaml(cfg, gpu, cpu, node_status)
    try:
        refresh_remote_nodes(cfg)
    except Exception as e:
        # This node's own self-registration above already completed and
        # must not be undone by a fleet-wide refresh problem -- log and
        # move on, same as this cycle already does for a single remote
        # node being unreachable (handled inside refresh_remote_nodes).
        log.warning("refresh_remote_nodes failed: %s", e)

    render_status_page(results, cfg)
    maybe_send_heartbeat(state, results, cfg, secrets)

    # Dead-man's switch: only pings if the cycle above completed without crashing.
    notify.ping_healthchecks(secrets.get("healthchecks_ping_url"))

    save_state(state)
    log.info("cycle complete: overall=%s", overall)
    return 0


if __name__ == "__main__":
    sys.exit(main())
