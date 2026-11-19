"""Protocol definitions (whereabout/v1) and low-overhead utilities."""

from __future__ import annotations

import os
import socket
import subprocess
from typing import Sequence

VERSION = "0.1.0"
SCHEMA_VERSION = "whereabout/v1"

ALL_SECTIONS = (
    "device",
    "runtime",
    "time",
    "network",
    "location",
    "weather",
    "vitals",
    "nearby",
    "memory",
    "extra",
)

SECTION_ALIASES = {
    "dev": "device",
    "host": "device",
    "sys": "vitals",
    "net": "network",
    "loc": "location",
    "geo": "location",
    "wx": "weather",
    "ctx": "runtime",
    "context": "runtime",
    "env": "runtime",
    "mem": "memory",
    "notes": "memory",
    "providers": "extra",
}

WMO_CODES: dict[int, str] = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Dense drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Light snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm + hail",
    99: "Severe thunderstorm",
}

SPEC_TEXT = """whereabout/v1 — Agent Situational Awareness Protocol
==================================================

Purpose
  Give an AI agent (or human) a fast, bounded, machine-and-LLM-friendly
  snapshot of WHERE it is running: physical body/device, execution runtime,
  local time, network & Wi-Fi/cellular link, geographic place, current weather,
  hardware vitals, and nearby companion devices/services.

Design Contract
  1. Fast & bounded: local probes run in parallel (<150ms). Network probes
     are cached by the router (gateway, gateway MAC, SSID, router id, subnet),
     not by the access point. Walking between extenders keeps the cache.
     Remote probes are bounded by a hard timeout (default 1.8s).
  2. Zero-noise output:
     - default (`compact`): ~7-10 aligned lines (~120-170 tokens), no ASCII art.
     - `-1` / `--oneline`: single line (~35 tokens) for prompt injection.
     - `--json`: strict `whereabout/v1` JSON object.
  3. Graceful degradation: offline, sandboxed, containerized, or missing
     permissions NEVER crash or hang; unavailable fields are omitted or
     marked unknown, and `meta.notes` explains degradations.
  4. Privacy-first: secrets and raw env vars are never read or printed;
     coordinates default to ~1km precision unless `--precise` is passed;
     `--fast` / `--offline` guarantees zero outbound network traffic.

JSON Envelope (`whereabout/v1`)
  {
    "schema": "whereabout/v1",
    "generated_at": "ISO-8601 local timestamp",
    "elapsed_ms": 42,
    "device":   { "kind", "id", "name", "model", "os", "os_version", "arch", "host", "user", "body" },
    "runtime":  { "flags", "tty", "shell", "cwd", "git": { "repo", "branch", "dirty" } },
    "time":     { "iso", "local", "weekday", "tz", "utc_offset", "phase", "uptime_s", "uptime" },
    "network":  { "online", "transport", "interface", "ssid", "bssid", "band", "channel",
                  "freq_mhz", "rssi_dbm", "router_id", "carrier", "rat", "local_ip", "gateway",
                  "gateway_mac", "public_ip", "isp", "vpn", "fingerprint" },
    "location": { "place", "city", "region", "country", "lat", "lon", "accuracy_m", "source", "age_s" },
    "weather":  { "temp_f", "temp_c", "feels_f", "feels_c", "condition", "wind_mph", "wind_kph",
                  "humidity", "precip_mm", "is_day", "age_s" },
    "vitals":   { "battery_pct", "power", "battery_mins", "ram_used_gb", "ram_total_gb",
                  "ram_pct", "load_1m", "cpus", "disk_free_gb" },
    "nearby":   { "services": [...], "adb_devices": [...] },
    "memory":   { "place_key", "ap_key", "area_key", "label", "subplace", "status",
                  "visits", "dwell_s", "dwell", "previous", "notes": [...], "total_notes" },
    "extra":    { "<provider_name>": { ... } },
    "meta":     { "mode", "cache": {...}, "notes": [...] }
  }

Place Memory (`~/.local/share/whereabout/memory.db`)
  Local SQLite ledger (<2ms) combining quiet transition/visit counts with intentional
  agent/user annotations (never a noisy command transcript):
    whereabout label "Home"              # name this router/building (or --ap / --area)
    whereabout note "Printer 192.168.1.50" [--ap | --area] [--kind device|rule|tip|todo|note]
    whereabout notes [--all] [--json]    # list notes for here (or everywhere)
    whereabout forget <id>               # remove a note by id
    whereabout places [--json]           # known places + recent visit transitions

Extension Protocol (Drop-in Providers)
  Any executable file in `~/.config/whereabout/providers.d/` (or `$WHEREABOUT_PROVIDERS_DIR`)
  is invoked in parallel (400ms timeout) with env vars:
    WHEREABOUT_SCHEMA=whereabout/v1
    WHEREABOUT_HOST, WHEREABOUT_OS, WHEREABOUT_DEVICE, WHEREABOUT_FINGERPRINT
  Stdout MUST be a single JSON object. It is merged into `extra.<filename>`.
  Optional reserved keys `_place` (str) or `_body` (str) can enrich location/device.

User Config (`~/.config/whereabout/config.json`)
  Optional JSON to label known places. Match a whole building with gateway_mac
  (or SSID, gateway, subnet, public_ip) and one access point with bssid. The
  access point wins when both match. Also sets `device_id`/`device_name` and
  `units` ("both"|"imperial"|"metric").
  Run `whereabout --init-config` to generate a starter file.
"""


def clean_env() -> dict[str, str]:
    """Return process env without Termux LD_LIBRARY_PATH when calling Android /system/bin."""
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    return env


def run_cmd(
    argv: Sequence[str],
    timeout: float = 0.4,
    strip_ld: bool = False,
) -> tuple[int, str]:
    """Run a command with a tight timeout, returning (returncode, stdout). Never raises."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=clean_env() if strip_ld else None,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception:
        return -1, ""


def read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readline().strip()
    except Exception:
        return ""


def tcp_open(host: str, port: int, timeout: float = 0.06) -> bool:
    """Fast non-blocking-style TCP reachability check for local daemons."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m"
    return f"{s}s"


def fmt_age(seconds: float | int | None) -> str:
    if seconds is None or seconds < 5:
        return "fresh"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"
