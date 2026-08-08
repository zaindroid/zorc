"""Notification senders: ntfy.sh and healthchecks.io. Stdlib only (urllib)."""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path

log = logging.getLogger("watchdog.notify")

SECRETS_PATH = Path(__file__).parent / "secrets" / "notify.json"


def load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        log.warning("secrets/notify.json not found — notifications disabled")
        return {}
    with open(SECRETS_PATH) as f:
        return json.load(f)


def _ascii_safe(s: str) -> str:
    """HTTP header values must be Latin-1/ASCII; strip anything else rather
    than let urllib raise UnicodeEncodeError and drop the notification."""
    return s.encode("ascii", errors="replace").decode("ascii")


def send_ntfy(topic: str, title: str, message: str, priority: str = "default", tags: str = "") -> bool:
    """POST a message to ntfy.sh. priority: min|low|default|high|urgent. Returns True on success."""
    if not topic:
        log.warning("ntfy topic not configured, skipping notification")
        return False
    url = f"https://ntfy.sh/{topic}"
    headers = {
        "Title": _ascii_safe(title),
        "Priority": _ascii_safe(priority),
    }
    if tags:
        headers["Tags"] = _ascii_safe(tags)
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            log.info("ntfy notification sent (priority=%s, status=%s)", priority, resp.status)
            return ok
    except Exception as e:
        log.error("failed to send ntfy notification: %s", type(e).__name__)
        return False


def ping_healthchecks(url: str | None, suffix: str = "") -> bool:
    """GET the healthchecks.io ping URL (dead-man's switch). No-op if url is empty."""
    if not url:
        log.debug("healthchecks ping URL not configured, skipping")
        return False
    target = url.rstrip("/") + suffix
    try:
        with urllib.request.urlopen(target, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            log.info("healthchecks ping sent%s (status=%s)", suffix or "", resp.status)
            return ok
    except Exception as e:
        log.error("failed to ping healthchecks: %s", type(e).__name__)
        return False
