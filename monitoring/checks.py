"""Individual health checks. Each check function returns a dict:
    {"status": "ok" | "warn" | "crit", "value": <any, json-safe>, "message": str}
Sustain logic (consecutive-cycles-over-threshold) lives in watchdog.py, not here —
these functions report the instantaneous/raw state only.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("watchdog.checks")

OK, WARN, CRIT = "ok", "warn", "crit"

sys.path.insert(0, str(Path(__file__).parent.parent / "deploy"))
import agent as deploy_agent  # noqa: E402


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------

def cpu_temp(cfg: dict) -> dict:
    warn_c = cfg["thresholds"]["cpu_temp_warn_c"]
    rc, out, err = _run(["sensors", "-j"])
    if rc != 0:
        return {"status": CRIT, "value": None, "message": f"sensors failed: {err.strip()}"}
    try:
        data = json.loads(out)
        coretemp = data.get("coretemp-isa-0000", {})
        core_temps = {}
        for key, val in coretemp.items():
            if not key.startswith("Core "):
                continue
            for field, temp in val.items():
                if field.endswith("_input"):
                    core_temps[key] = temp
    except (json.JSONDecodeError, KeyError) as e:
        return {"status": CRIT, "value": None, "message": f"failed to parse sensors output: {e}"}

    if not core_temps:
        return {"status": CRIT, "value": None, "message": "no coretemp Core sensors found"}

    max_temp = max(core_temps.values())
    hottest = max(core_temps, key=core_temps.get)
    msg = f"{hottest} at {max_temp}C ({', '.join(f'{k}={v}C' for k, v in sorted(core_temps.items()))})"
    # Note: >90C sustain logic is applied by watchdog.py; here >90 is reported
    # as warn (raw), watchdog.py escalates to crit once sustained.
    if max_temp > warn_c:
        return {"status": WARN, "value": max_temp, "message": msg, "raw_over_crit": max_temp > cfg["thresholds"]["cpu_temp_crit_c"]}
    return {"status": OK, "value": max_temp, "message": msg, "raw_over_crit": False}


def gpu_temp(cfg: dict) -> dict:
    warn_c = cfg["thresholds"]["gpu_temp_warn_c"]
    rc, out, err = _run([
        "nvidia-smi",
        "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0:
        return {"status": CRIT, "value": None, "message": f"nvidia-smi failed: {err.strip() or out.strip()}"}
    try:
        temp_s, util_s, mem_used_s, mem_total_s = [x.strip() for x in out.strip().split(",")]
        temp, util, mem_used, mem_total = int(temp_s), int(util_s), int(mem_used_s), int(mem_total_s)
    except ValueError as e:
        return {"status": CRIT, "value": None, "message": f"failed to parse nvidia-smi output: {out!r} ({e})"}

    value = {"temp_c": temp, "util_pct": util, "mem_used_mb": mem_used, "mem_total_mb": mem_total}
    msg = f"{temp}C, {util}% util, {mem_used}/{mem_total}MB VRAM"
    status = WARN if temp > warn_c else OK
    return {"status": status, "value": value, "message": msg}


def _smart_device(dev: str) -> dict:
    rc, out, err = _run(["sudo", "smartctl", "-H", "-A", "--json=c", dev])
    # smartctl returns a bitmask exit code; bit 0/1 are command-line/device-open
    # errors (fatal), bits 2+ are SMART-status/attribute warnings (still parse).
    if rc & 0b11:
        return {"status": CRIT, "value": None, "message": f"smartctl couldn't read {dev}: {err.strip() or out.strip()}"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        return {"status": CRIT, "value": None, "message": f"failed to parse smartctl output for {dev}: {e}"}

    passed = data.get("smart_status", {}).get("passed")
    if passed is False:
        return {"status": CRIT, "value": {"passed": False}, "message": f"{dev}: SMART health check FAILED"}

    if "nvme_smart_health_information_log" in data:
        log_data = data["nvme_smart_health_information_log"]
        critical_warning = log_data.get("critical_warning", 0)
        media_errors = log_data.get("media_errors", 0)
        value = {"passed": passed, "critical_warning": critical_warning, "media_errors": media_errors}
        if critical_warning or media_errors:
            return {"status": CRIT, "value": value,
                     "message": f"{dev}: critical_warning={critical_warning}, media_errors={media_errors}"}
        return {"status": OK, "value": value, "message": f"{dev}: healthy (NVMe)"}

    # ATA/SATA: attribute IDs 5 (reallocated) and 197 (pending), name varies by vendor.
    attrs = {a["id"]: a for a in data.get("ata_smart_attributes", {}).get("table", [])}
    reallocated = attrs.get(5, {}).get("raw", {}).get("value", 0)
    pending = attrs.get(197, {}).get("raw", {}).get("value", 0)
    value = {"passed": passed, "reallocated_sectors": reallocated, "pending_sectors": pending}
    if reallocated or pending:
        return {"status": CRIT, "value": value,
                 "message": f"{dev}: reallocated={reallocated}, pending={pending}"}
    return {"status": OK, "value": value, "message": f"{dev}: healthy (ATA)"}


def smart_all(cfg: dict) -> dict[str, dict]:
    return {dev: _smart_device(dev) for dev in cfg["smart_devices"]}


def disk_usage(path: str, cfg: dict) -> dict:
    warn_pct, crit_pct = cfg["thresholds"]["disk_warn_pct"], cfg["thresholds"]["disk_crit_pct"]
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        return {"status": CRIT, "value": None, "message": f"{path}: {e}"}
    pct_used = round(100 * usage.used / usage.total, 1)
    msg = f"{path}: {pct_used}% used ({usage.used // (1024**3)}G / {usage.total // (1024**3)}G)"
    status = CRIT if pct_used >= crit_pct else WARN if pct_used >= warn_pct else OK
    return {"status": status, "value": pct_used, "message": msg}


def _read_meminfo() -> dict[str, int]:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            info[key] = int(val.strip().split()[0])  # kB
    return info


def ram(cfg: dict) -> dict:
    warn_pct = cfg["thresholds"]["ram_available_warn_pct"]
    info = _read_meminfo()
    total, avail = info["MemTotal"], info["MemAvailable"]
    avail_pct = round(100 * avail / total, 1)
    msg = f"{avail_pct}% available ({avail // 1024}MB / {total // 1024}MB)"
    status = WARN if avail_pct < warn_pct else OK
    return {"status": status, "value": avail_pct, "message": msg}


def swap(cfg: dict) -> dict:
    warn_pct = cfg["thresholds"]["swap_used_warn_pct"]
    info = _read_meminfo()
    total, free = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    if total == 0:
        return {"status": OK, "value": 0, "message": "no swap configured"}
    used_pct = round(100 * (total - free) / total, 1)
    msg = f"{used_pct}% swap used"
    status = WARN if used_pct > warn_pct else OK
    return {"status": status, "value": used_pct, "message": msg}


def load_avg(cfg: dict) -> dict:
    cores = cfg["core_count"]
    _, _, load15 = os.getloadavg()
    msg = f"15min load {load15:.2f} (cores={cores})"
    # raw_over: watchdog.py applies the sustained-cycles escalation.
    return {"status": WARN if load15 > cores else OK, "value": round(load15, 2), "message": msg,
            "raw_over": load15 > cores}


def power_source(cfg: dict) -> dict:
    path = "/sys/class/power_supply/AC/online"
    try:
        with open(path) as f:
            online = f.read().strip() == "1"
    except OSError as e:
        return {"status": WARN, "value": None, "message": f"couldn't read {path}: {e}"}
    if online:
        return {"status": OK, "value": "ac", "message": "on AC power"}
    return {"status": CRIT, "value": "battery", "message": "ON BATTERY — wall power dropped"}


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def _systemctl_is_active(unit: str) -> bool:
    rc, out, _ = _run(["systemctl", "is-active", unit])
    return out.strip() == "active"


def docker_daemon(cfg: dict) -> dict:
    active = _systemctl_is_active("docker")
    return {"status": OK if active else CRIT, "value": active,
            "message": "docker daemon active" if active else "docker daemon NOT active"}


def coolify_containers(cfg: dict) -> dict:
    rc, out, err = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    if rc != 0:
        return {"status": CRIT, "value": None, "message": f"docker ps failed: {err.strip()}"}
    running = {}
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        name, status_str = line.split("\t", 1)
        running[name] = status_str
    missing = [c for c in cfg["coolify_containers"] if c not in running]
    unhealthy = [c for c in cfg["coolify_containers"]
                 if c in running and "healthy" not in running[c] and "Up" in running[c] and "(" in running[c]]
    if missing:
        return {"status": CRIT, "value": {"missing": missing, "running": list(running)},
                 "message": f"missing containers: {', '.join(missing)}"}
    if unhealthy:
        return {"status": WARN, "value": {"unhealthy": unhealthy},
                 "message": f"containers not reporting healthy: {', '.join(unhealthy)}"}
    return {"status": OK, "value": list(running), "message": f"all {len(cfg['coolify_containers'])} containers running"}


def postgres_ready(cfg: dict) -> dict:
    rc, out, err = _run(["docker", "exec", "coolify-db", "pg_isready"])
    if rc == 0:
        return {"status": OK, "value": True, "message": out.strip() or "postgres accepting connections"}
    return {"status": CRIT, "value": False, "message": f"pg_isready failed: {(err or out).strip()}"}


def tailscaled(cfg: dict) -> dict:
    active = _systemctl_is_active("tailscaled")
    if not active:
        return {"status": CRIT, "value": False, "message": "tailscaled not active"}
    rc, out, err = _run(["tailscale", "status", "--json"])
    if rc != 0:
        return {"status": CRIT, "value": None, "message": f"tailscale status failed: {err.strip()}"}
    try:
        data = json.loads(out)
        self_online = data.get("Self", {}).get("Online", None)
    except json.JSONDecodeError:
        self_online = None
    if self_online is False:
        return {"status": CRIT, "value": False, "message": "tailscaled active but node shows offline"}
    return {"status": OK, "value": True, "message": "tailscaled active and connected"}


def cloudflared(cfg: dict) -> dict:
    active = _systemctl_is_active("cloudflared")
    if not active:
        return {"status": CRIT, "value": False, "message": "cloudflared not active"}
    rc, out, err = _run([
        "cloudflared", "--origincert", "/home/zman/.cloudflared/cert.pem",
        "tunnel", "info", cfg["cloudflare_tunnel_name"],
    ])
    if rc != 0:
        return {"status": CRIT, "value": None, "message": f"tunnel info failed: {err.strip() or out.strip()}"}
    if "CONNECTOR ID" not in out and "no connections" in out.lower():
        return {"status": CRIT, "value": False, "message": "tunnel has no active connections"}
    return {"status": OK, "value": True, "message": "cloudflared active with connections"}


def systemd_failed(cfg: dict) -> dict:
    rc, out, err = _run(["systemctl", "--failed", "--no-legend"])
    failed_units = [line.split()[0] for line in out.strip().splitlines() if line.strip()]
    if failed_units:
        return {"status": CRIT, "value": failed_units, "message": f"failed units: {', '.join(failed_units)}"}
    return {"status": OK, "value": [], "message": "no failed units"}


def mounts(cfg: dict) -> dict:
    missing = [m for m in cfg["mounts"] if not os.path.ismount(m)]
    if missing:
        return {"status": CRIT, "value": missing, "message": f"not mounted: {', '.join(missing)}"}
    return {"status": OK, "value": cfg["mounts"], "message": "all expected mounts present"}


def docker_user_port_block(cfg: dict) -> dict:
    ports = cfg["docker_user_block_ports"]
    rc, out, err = _run(["sudo", "iptables", "-L", "DOCKER-USER", "-n"])
    if rc != 0:
        return {"status": CRIT, "value": None, "message": f"couldn't read DOCKER-USER chain: {err.strip()}"}
    missing = [p for p in ports if f"ctorigdstport {p}" not in out]
    if missing:
        return {"status": CRIT, "value": {"missing": missing},
                 "message": f"DOCKER-USER DROP rule MISSING for port(s) {missing} — may be exposed to LAN/tailnet"}
    return {"status": OK, "value": True, "message": f"DOCKER-USER DROP rules live for ports {ports}"}


# ---------------------------------------------------------------------------
# Node self-registration data (feeds nodes/servingz.yaml, see watchdog.py)
# ---------------------------------------------------------------------------

def _detect_accelerator() -> dict | None:
    """NVIDIA discrete GPU (desktop/server, via nvidia-smi) first, then a
    Jetson-style SoC accelerator (no nvidia-smi there -- VRAM is shared
    system RAM, detected instead via the device-tree model string every
    Jetson exposes). Real gap this replaces: the old version only ever
    checked nvidia-smi, so a Jetson board silently reported no accelerator
    at all."""
    rc, out, _ = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"])
    if rc == 0 and out.strip():
        try:
            name, driver, vram_s = [x.strip() for x in out.strip().split(",")]
            vram_mb = int(vram_s.replace(" MiB", "").strip())
            return {"type": "cuda", "name": name, "vram_mb": vram_mb, "driver": driver}
        except ValueError:
            pass

    try:
        model = Path("/proc/device-tree/model").read_text(errors="replace").strip("\x00").strip()
    except OSError:
        model = ""
    if "jetson" in model.lower():
        return {"type": "tegra", "name": model, "vram_mb": None, "driver": None}

    return None


def _detect_power_source() -> dict:
    """Tries every /sys/class/power_supply/*/online path rather than one
    hardcoded name (AC vs ACAD vs AC0 varies by machine) -- and, real gap
    this replaces: the old version raised on any node without one at all
    (most VPS/cloud/desktop nodes have no battery subsystem whatsoever),
    which meant this silently broke self-registration on hostinger-vps. No
    power_supply entries at all is treated as "always-on mains power," the
    correct read for that case, not an error."""
    power_supply_dir = Path("/sys/class/power_supply")
    if not power_supply_dir.is_dir():
        return {"source": "ac", "on_battery": False}
    for entry in power_supply_dir.iterdir():
        online_path = entry / "online"
        if not online_path.exists():
            continue
        try:
            on_ac = online_path.read_text().strip() == "1"
            return {"source": "ac" if on_ac else "battery", "on_battery": not on_ac}
        except OSError:
            continue
    return {"source": "ac", "on_battery": False}


def _detect_cpu() -> dict:
    cpu_model = "unknown"
    core_ids: set[str] = set()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name") and cpu_model == "unknown":
                    cpu_model = line.split(":", 1)[1].strip()
                elif line.startswith("core id"):
                    core_ids.add(line.split(":", 1)[1].strip())
    except OSError:
        pass
    return {"model": cpu_model, "cores": len(core_ids) or None, "threads": os.cpu_count()}


def gather_node_info(cfg: dict, gpu_check_result: dict, cpu_check_result: dict) -> dict:
    """Reuses already-computed gpu/cpu check results to avoid double-probing.

    arch/cpu/ram/power/accelerator below are auto-probed every run;
    capabilities/labels are hand-maintained in this node's config.json --
    kept manual deliberately (what a node is *allowed* to do is a policy
    decision, not something to infer from what hardware happens to be
    present)."""
    info = _read_meminfo()
    ram_mb = info["MemTotal"] // 1024

    return {
        "node": cfg["node"]["name"],
        "tailscale_ip": cfg["node"]["tailscale_ip"],
        "arch": platform.machine(),
        "accelerator": _detect_accelerator(),
        "cpu": _detect_cpu(),
        "ram_mb": ram_mb,
        "power": _detect_power_source(),
        "capabilities": cfg["node"]["capabilities"],
        "labels": cfg["node"]["labels"],
    }


def app_memory_pressure(name: str, memory_mb_limit: int, cfg: dict) -> dict:
    """Live memory usage of a registered app against its declared Coolify
    limit. Alert-only by design -- there's no auto-scale lever on this
    platform (Coolify only supports replica scaling in Swarm mode, which
    this host doesn't use), so this exists to tell a human when an app
    needs attention, not to act on its own.
    """
    if memory_mb_limit <= 0:
        return {"status": OK, "value": 0, "message": f"{name}: no memory limit declared (static site)"}
    warn_pct = cfg["thresholds"].get("app_memory_warn_pct", 80)
    crit_pct = cfg["thresholds"].get("app_memory_crit_pct", 95)
    try:
        usage = deploy_agent.app_resources(name)
    except Exception as e:
        return {"status": OK, "value": None, "message": f"{name}: could not read live usage ({e})"}
    used_mb = usage.get("mem_used_mb", 0)
    pct = round(100 * used_mb / memory_mb_limit, 1) if memory_mb_limit else 0
    msg = f"{name}: {pct}% of {memory_mb_limit}MB limit ({used_mb}MB used, {usage.get('containers', 0)} container(s))"
    # Note: sustain logic is applied by watchdog.py, same as cpu_temp -- here
    # over-warn is reported as warn (raw), watchdog.py escalates to crit once
    # sustained for app_memory_sustained_cycles consecutive cycles.
    if pct >= warn_pct:
        return {"status": WARN, "value": pct, "message": msg, "raw_over_crit": pct >= crit_pct}
    return {"status": OK, "value": pct, "message": msg, "raw_over_crit": False}
