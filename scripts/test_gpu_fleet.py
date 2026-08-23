#!/usr/bin/env python3
"""test_gpu_fleet.py — Phase 6.1 pipeline test (gpu_fleet_status). Run in
CI alongside the other scripts/test_*.py.

Never touches real SSH/nvidia-smi -- agent._nvidia_smi_query_local/
_remote and mcp_server._load_node_telemetry are all monkeypatched.

The scenario that matters most here: a completely NEW, fictional GPU node
that this test invents on the spot (never hardcoded anywhere in
mcp_server.py) shows up in the fleet automatically just by having an
`accelerator` in its telemetry fixture -- proving gpu_fleet_status() is
actually generic over the fleet, not secretly special-cased to today's
three real nodes.

Usage:
    python3 scripts/test_gpu_fleet.py
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


def test_parse_nvidia_smi_csv(agent) -> None:
    single = agent._parse_nvidia_smi_csv("0, NVIDIA GeForce RTX 5090, 35, 12, 26859, 32607")
    check("parses a single-GPU line", single == [{"index": 0, "name": "NVIDIA GeForce RTX 5090", "temp_c": 35,
                                                    "utilization_pct": 12, "mem_used_mb": 26859, "mem_total_mb": 32607}],
          str(single))

    multi = agent._parse_nvidia_smi_csv(
        "0, NVIDIA GeForce RTX 3090, 40, 0, 100, 24576\n1, NVIDIA GeForce RTX 3090, 42, 5, 200, 24576"
    )
    check("parses a multi-GPU (two-line) response", multi is not None and len(multi) == 2, str(multi))

    check("empty output -> None, not a crash", agent._parse_nvidia_smi_csv("") is None)
    check("garbage output -> None, not a crash", agent._parse_nvidia_smi_csv("not,even,close,to,valid,csv,here") is None)

    # Real scenario confirmed live: jetson-thor's nvidia-smi shim reports
    # "[N/A]" for memory.used/memory.total (unified-memory architecture,
    # no separate dedicated VRAM) while temp/utilization are still real
    # numbers on the same line -- must keep the fields it has, not
    # discard the whole card.
    jetson = agent._parse_nvidia_smi_csv("0, NVIDIA Thor, 50, 0, [N/A], [N/A]")
    check("a card with [N/A] memory fields keeps its valid temp/utilization, doesn't get dropped entirely",
          jetson == [{"index": 0, "name": "NVIDIA Thor", "temp_c": 50, "utilization_pct": 0,
                      "mem_used_mb": None, "mem_total_mb": None}],
          str(jetson))


FIXTURE_REGISTRY = {
    "nodes": {
        "servingz": {"backend": "coolify", "tailscale_ip": None},
        "hostinger-vps": {"backend": "coolify", "tailscale_ip": "100.112.175.41"},  # no accelerator -- excluded
        "rtx5090": {"backend": "zorc-agent", "tailscale_ip": "100.124.93.99",
                    "ssh_key": "deploy/secrets/rtx5090_deploy_key", "ssh_user": "zulhaq"},
        "bitbots_gpu": {"backend": "zorc-agent", "tailscale_ip": "100.118.75.128",
                        "ssh_key": "deploy/secrets/bitbots_gpu_deploy_key", "ssh_user": "zul2s"},
        "jetson-thor": {"backend": "zorc-agent", "tailscale_ip": "100.119.248.100",
                        "ssh_key": "deploy/secrets/jetson-thor_deploy_key", "ssh_user": "zain"},
        # A node this test invents on the spot -- proves the function is
        # genuinely generic, not secretly hardcoded to the three real ones.
        "brand-new-gpu-box": {"backend": "zorc-agent", "tailscale_ip": "100.99.99.99",
                               "ssh_key": "deploy/secrets/fake_key", "ssh_user": "someone"},
    },
}

FIXTURE_TELEMETRY = {
    "servingz": {"accelerator": {"type": "cuda", "name": "Quadro K2100M", "vram_mb": 1996, "count": 1}},
    "hostinger-vps": {},  # no accelerator key at all
    "rtx5090": {"accelerator": {"type": "cuda", "name": "NVIDIA GeForce RTX 5090", "vram_mb": 32607, "count": 1}},
    "bitbots_gpu": {"accelerator": {"type": "cuda", "name": "NVIDIA GeForce RTX 3090", "vram_mb": 49152, "count": 2}},
    "jetson-thor": {"accelerator": {"type": "tegra", "name": "NVIDIA Thor", "vram_mb": None, "count": 1}},
    "brand-new-gpu-box": {"accelerator": {"type": "cuda", "name": "NVIDIA Fake GPU 9000", "vram_mb": 8192, "count": 1}},
}


def main() -> int:
    import agent
    import mcp_server as m

    test_parse_nvidia_smi_csv(agent)

    original_load_registry = agent.load_registry
    original_load_telemetry = m._load_node_telemetry
    original_local = agent._nvidia_smi_query_local
    original_remote = agent._nvidia_smi_query_remote

    remote_calls = []

    def fake_remote(tailscale_ip, ssh_key, user):
        remote_calls.append((tailscale_ip, str(ssh_key), user))
        if tailscale_ip == "100.124.93.99":  # rtx5090 -- busy
            return [{"index": 0, "name": "RTX 5090", "temp_c": 60, "utilization_pct": 85,
                      "mem_used_mb": 30000, "mem_total_mb": 32607}]
        if tailscale_ip == "100.118.75.128":  # bitbots_gpu -- idle, two cards
            return [{"index": 0, "name": "RTX 3090", "temp_c": 35, "utilization_pct": 0,
                      "mem_used_mb": 100, "mem_total_mb": 24576},
                    {"index": 1, "name": "RTX 3090", "temp_c": 34, "utilization_pct": 1,
                     "mem_used_mb": 50, "mem_total_mb": 24576}]
        if tailscale_ip == "100.119.248.100":  # jetson-thor -- real temp/util, [N/A] memory (unified memory)
            return [{"index": 0, "name": "NVIDIA Thor", "temp_c": 50, "utilization_pct": 0,
                      "mem_used_mb": None, "mem_total_mb": None}]
        if tailscale_ip == "100.99.99.99":  # the brand-new node -- unreachable
            return None
        return None

    try:
        agent.load_registry = lambda: FIXTURE_REGISTRY
        m._load_node_telemetry = lambda name: FIXTURE_TELEMETRY.get(name, {})
        agent._nvidia_smi_query_local = lambda: [{"index": 0, "name": "Quadro K2100M", "temp_c": 55,
                                                    "utilization_pct": 2, "mem_used_mb": 50, "mem_total_mb": 1996}]
        agent._nvidia_smi_query_remote = fake_remote

        result = m.gpu_fleet_status()
        fleet_by_node = {e["node"]: e for e in result["fleet"]}

        check("hostinger-vps (no accelerator) is excluded from the fleet",
              "hostinger-vps" not in fleet_by_node, str(list(fleet_by_node)))
        check("all 5 GPU-bearing nodes are present, including one invented for this test alone",
              {"servingz", "rtx5090", "bitbots_gpu", "jetson-thor", "brand-new-gpu-box"} <= set(fleet_by_node),
              str(list(fleet_by_node)))

        check("LOCAL_NODE (servingz) uses the local query path, not SSH",
              fleet_by_node["servingz"]["live"] is True, str(fleet_by_node["servingz"]))
        check("...and never shows up as a remote SSH call itself",
              all(ip != "servingz" for ip, _, _ in remote_calls) and len(remote_calls) == 4,
              f"remote_calls={remote_calls}")

        rtx = fleet_by_node["rtx5090"]
        check("rtx5090 reports live=true with real numbers", rtx["live"] is True, str(rtx))
        check("rtx5090 is flagged busy (85% util)", rtx["busy"] is True, str(rtx))
        check("rtx5090 mem_free_mb computed correctly", rtx["mem_free_mb"] == 32607 - 30000, str(rtx))

        bb = fleet_by_node["bitbots_gpu"]
        check("bitbots_gpu (2 cards) averages utilization across both", bb["avg_utilization_pct"] == 0.5, str(bb))
        check("bitbots_gpu sums memory across both cards", bb["mem_used_mb"] == 150 and bb["mem_total_mb"] == 49152,
              str(bb))
        check("bitbots_gpu is NOT flagged busy (near-idle)", bb["busy"] is False, str(bb))

        # jetson-thor: real scenario, unified memory -- live=true, real
        # temp/utilization, but mem fields are None, not 0 or a crash.
        jt = fleet_by_node["jetson-thor"]
        check("jetson-thor reports live=true despite having no memory numbers", jt["live"] is True, str(jt))
        check("jetson-thor's avg_utilization_pct is a real number (0.0), not None",
              jt["avg_utilization_pct"] == 0.0, str(jt))
        check("jetson-thor's memory fields are None (not 0, not a crash) -- unified memory, nothing to report",
              jt["mem_used_mb"] is None and jt["mem_total_mb"] is None and jt["mem_free_mb"] is None, str(jt))
        check("jetson-thor is NOT flagged busy (0% util, no memory signal either)", jt["busy"] is False, str(jt))

        new_node = fleet_by_node["brand-new-gpu-box"]
        check("the brand-new node (query returns None -- simulating unreachable) shows live=false, not a crash",
              new_node["live"] is False, str(new_node))
        check("...with a clear note explaining why, not silently wrong data",
              "note" in new_node and "cards" not in new_node, str(new_node))

        check("remote queries used each node's OWN ssh_key/ssh_user, not a shared hardcoded one",
              any(c[1].endswith("rtx5090_deploy_key") and c[2] == "zulhaq" for c in remote_calls) and
              any(c[1].endswith("bitbots_gpu_deploy_key") and c[2] == "zul2s" for c in remote_calls),
              str(remote_calls))

        check("response includes the queue_depth/no-job-queue-yet disclaimer",
              "queue_depth" in result.get("note", ""), result.get("note"))
    finally:
        agent.load_registry = original_load_registry
        m._load_node_telemetry = original_load_telemetry
        agent._nvidia_smi_query_local = original_local
        agent._nvidia_smi_query_remote = original_remote

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
