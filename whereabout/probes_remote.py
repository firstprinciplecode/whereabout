"""Timeout-bounded remote probes for IP geolocation, reverse geocoding, and weather."""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Any

from .protocol import VERSION, WMO_CODES

USER_AGENT = f"whereabout/{VERSION}"

_CA_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/data/data/com.termux/files/usr/etc/tls/cert.pem",
)

_SSL_CTX: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context even on macOS Python.org installs missing certifi."""
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.exists(paths.cafile)) or (paths.capath and os.path.isdir(paths.capath)):
        _SSL_CTX = ssl.create_default_context()
        return _SSL_CTX
    env_ca = os.environ.get("SSL_CERT_FILE", "")
    for ca in (env_ca, *_CA_CANDIDATES):
        if ca and os.path.exists(ca):
            _SSL_CTX = ssl.create_default_context(cafile=ca)
            return _SSL_CTX
    try:
        import certifi  # type: ignore
        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        return _SSL_CTX
    except Exception:
        _SSL_CTX = ssl.create_default_context()
        return _SSL_CTX


def _fetch_json(url: str, timeout: float) -> dict[str, Any] | None:
    if timeout <= 0.1:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def probe_ip_geo(timeout: float = 1.4) -> dict[str, Any] | None:
    """Resolve public IP, ISP, city/region/country, and approximate coordinates."""
    # Try ipinfo.io first (fast, reliable, no key needed), then ipapi.co
    per_try = min(timeout, 1.1)
    data = _fetch_json("https://ipinfo.io/json", timeout=per_try)
    if data and data.get("ip"):
        lat, lon = None, None
        loc_str = str(data.get("loc") or "")
        if "," in loc_str:
            parts = loc_str.split(",", 1)
            try:
                lat, lon = float(parts[0]), float(parts[1])
            except ValueError:
                pass
        org = str(data.get("org") or "").strip()
        # Strip leading "AS12345 " for a cleaner ISP label
        isp = org.split(" ", 1)[1] if org.startswith("AS") and " " in org else org
        out: dict[str, Any] = {
            "public_ip": str(data.get("ip")),
            "city": str(data.get("city") or ""),
            "region": str(data.get("region") or ""),
            "country": str(data.get("country") or ""),
            "isp": isp,
            "source": "ip",
            "accuracy_m": 5000,
        }
        if lat is not None and lon is not None:
            out["lat"] = lat
            out["lon"] = lon
        return {k: v for k, v in out.items() if v not in ("", None)}

    rem = max(0.3, timeout - per_try)
    data = _fetch_json("https://ipapi.co/json/", timeout=rem)
    if data and data.get("ip") and not data.get("error"):
        out = {
            "public_ip": str(data.get("ip")),
            "city": str(data.get("city") or ""),
            "region": str(data.get("region_code") or data.get("region") or ""),
            "country": str(data.get("country_code") or data.get("country") or ""),
            "isp": str(data.get("org") or ""),
            "source": "ip",
            "accuracy_m": 5000,
        }
        if isinstance(data.get("latitude"), (int, float)) and isinstance(data.get("longitude"), (int, float)):
            out["lat"] = float(data["latitude"])
            out["lon"] = float(data["longitude"])
        return {k: v for k, v in out.items() if v not in ("", None)}

    return None


def probe_reverse_geocode(lat: float, lon: float, timeout: float = 1.0) -> dict[str, str] | None:
    """Turn raw GPS lat/lon into city/region/country via BigDataCloud (free, no key)."""
    url = (
        "https://api.bigdatacloud.net/data/reverse-geocode-client"
        f"?latitude={lat:.5f}&longitude={lon:.5f}&localityLanguage=en"
    )
    data = _fetch_json(url, timeout=timeout)
    if not data:
        return None
    city = str(data.get("city") or data.get("locality") or "").strip()
    region = str(data.get("principalSubdivision") or "").strip()
    country = str(data.get("countryCode") or "").strip()
    out = {"city": city, "region": region, "country": country}
    return {k: v for k, v in out.items() if v} or None


def probe_weather(lat: float, lon: float, timeout: float = 1.2) -> dict[str, Any] | None:
    """Fetch current weather from Open-Meteo (free, fast, no API key)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m,is_day"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto"
    )
    data = _fetch_json(url, timeout=timeout)
    if not data or not isinstance(data.get("current"), dict):
        return None
    cur = data["current"]
    temp_f = cur.get("temperature_2m")
    if temp_f is None:
        return None
    feels_f = cur.get("apparent_temperature")
    wind_mph = cur.get("wind_speed_10m")
    code = int(cur.get("weather_code", 0))
    out: dict[str, Any] = {
        "temp_f": round(float(temp_f), 1),
        "temp_c": round((float(temp_f) - 32.0) * 5.0 / 9.0, 1),
        "condition": WMO_CODES.get(code, f"Code {code}"),
        "code": code,
        "is_day": bool(cur.get("is_day", 1)),
    }
    if feels_f is not None:
        out["feels_f"] = round(float(feels_f), 1)
        out["feels_c"] = round((float(feels_f) - 32.0) * 5.0 / 9.0, 1)
    if wind_mph is not None:
        out["wind_mph"] = round(float(wind_mph), 1)
        out["wind_kph"] = round(float(wind_mph) * 1.60934, 1)
    if cur.get("relative_humidity_2m") is not None:
        out["humidity"] = int(round(float(cur["relative_humidity_2m"])))
    if cur.get("precipitation") is not None and float(cur["precipitation"]) > 0:
        out["precip_mm"] = round(float(cur["precipitation"]), 2)
    return out
