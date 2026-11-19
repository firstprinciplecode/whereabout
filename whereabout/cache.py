"""Fingerprint-keyed cache and optional user configuration (~/.config/whereabout)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

GEO_TTL_SECONDS = 1800      # 30 minutes (invalidated immediately on network fingerprint change)
WEATHER_TTL_SECONDS = 900   # 15 minutes per rounded (lat, lon)
GEOCODE_TTL_SECONDS = 86400 # 24 hours per rounded (lat, lon)


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "whereabout")


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "whereabout")


def config_path() -> str:
    return os.environ.get("WHEREABOUT_CONFIG") or os.path.join(config_dir(), "config.json")


def cache_path() -> str:
    return os.environ.get("WHEREABOUT_CACHE") or os.path.join(cache_dir(), "cache.json")


def load_config() -> dict[str, Any]:
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def init_config(force: bool = False) -> tuple[str, bool]:
    """Write a starter ~/.config/whereabout/config.json. Returns (path, created)."""
    path = config_path()
    if os.path.exists(path) and not force:
        return path, False
    starter = {
        "device_id": "",
        "device_name": "",
        "units": "both",
        "places": [
            {
                "name": "Home",
                "ssid": ["YourHomeWiFi"],
                "bssid": [],
                "gateway": ["192.168.1.1"],
                "gateway_mac": [],
                "city": "",
                "region": "",
                "country": "",
            }
        ],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(starter, f, indent=2)
        f.write("\n")
    return path, True


def load_cache() -> dict[str, Any]:
    path = cache_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(data: dict[str, Any]) -> bool:
    """Atomically persist cache. Fails silently in read-only sandboxes."""
    path = cache_path()
    try:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".whereabout-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp_path, path)
            return True
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return False
    except Exception:
        return False


def get_cached(cache: dict[str, Any], bucket: str, key: str, ttl: int) -> tuple[dict[str, Any] | None, int | None]:
    """Return (entry_data, age_seconds) if present and fresh, else (None, None)."""
    section = cache.get(bucket)
    if not isinstance(section, dict):
        return None, None
    entry = section.get(key)
    if not isinstance(entry, dict):
        return None, None
    ts = entry.get("ts")
    payload = entry.get("data")
    if not isinstance(ts, (int, float)) or not isinstance(payload, dict):
        return None, None
    age = int(max(0.0, time.time() - ts))
    if age > ttl:
        return None, None
    return payload, age


def put_cached(cache: dict[str, Any], bucket: str, key: str, payload: dict[str, Any], max_entries: int = 16) -> None:
    section = cache.setdefault(bucket, {})
    if not isinstance(section, dict):
        section = {}
        cache[bucket] = section
    section[key] = {"ts": time.time(), "data": payload}
    if len(section) > max_entries:
        oldest = sorted(section.items(), key=lambda kv: kv[1].get("ts", 0))
        for old_k, _ in oldest[: len(section) - max_entries]:
            section.pop(old_k, None)


def _as_list(val: Any) -> list[str]:
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return []


def _macs(val: Any) -> set[str]:
    return {item.lower() for item in _as_list(val)}


def match_place(
    config: dict[str, Any],
    *,
    ssid: str = "",
    bssid: str = "",
    gateway: str = "",
    gateway_mac: str = "",
    local_ip: str = "",
    public_ip: str = "",
    host: str = "",
    device_id: str = "",
) -> dict[str, Any] | None:
    """Match the current network against named places.

    An access-point match outranks a whole-router match, which outranks a shared
    gateway address. Put the specific place and the building place both in the
    file and the closer one wins.
    """
    places = config.get("places")
    if not isinstance(places, list):
        return None
    best: dict[str, Any] | None = None
    best_score = 0
    for place in places:
        if not isinstance(place, dict) or not place.get("name"):
            continue
        score = 0
        if bssid and bssid.lower() in _macs(place.get("bssid")):
            score += 8
        if gateway_mac and gateway_mac.lower() in _macs(place.get("gateway_mac")):
            score += 4
        if ssid and ssid in _as_list(place.get("ssid")):
            score += 2
        if gateway and gateway in _as_list(place.get("gateway")):
            score += 1
        if public_ip and public_ip in _as_list(place.get("public_ip")):
            score += 1
        if host and host in _as_list(place.get("host")):
            score += 1
        if device_id and device_id in _as_list(place.get("device_id")):
            score += 1
        for prefix in _as_list(place.get("subnet_prefix")):
            if local_ip and local_ip.startswith(prefix):
                score += 1
        if score > best_score:
            best = place
            best_score = score
    return best
