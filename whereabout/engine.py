"""Parallel probe orchestrator with fingerprint-keyed caching and hard deadlines."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from .cache import (
    GEO_TTL_SECONDS,
    GEOCODE_TTL_SECONDS,
    WEATHER_TTL_SECONDS,
    get_cached,
    load_cache,
    load_config,
    match_place,
    put_cached,
    save_cache,
)
from .memory import observe_and_recall
from .probes_local import (
    probe_device,
    probe_location_local,
    probe_nearby,
    probe_network_local,
    probe_runtime,
    probe_time,
    probe_vitals,
)
from .probes_remote import probe_ip_geo, probe_reverse_geocode, probe_weather
from .protocol import ALL_SECTIONS, SCHEMA_VERSION, SECTION_ALIASES
from .providers import run_providers


def normalize_sections(only: Iterable[str] | None) -> set[str]:
    if not only:
        return set(ALL_SECTIONS)
    selected: set[str] = set()
    for raw in only:
        tok = raw.strip().lower()
        if not tok:
            continue
        tok = SECTION_ALIASES.get(tok, tok)
        if tok in ALL_SECTIONS:
            selected.add(tok)
    return selected or set(ALL_SECTIONS)


def snapshot(
    *,
    fast: bool = False,
    fresh: bool = False,
    precise: bool = False,
    track: bool = True,
    only: Iterable[str] | None = None,
    timeout: float = 1.8,
) -> dict[str, Any]:
    """Collect a bounded whereabout/v1 situational snapshot."""
    t0 = time.monotonic()
    deadline = t0 + max(0.3, timeout)
    sections = normalize_sections(only)
    config = load_config()
    cache = {} if fresh else load_cache()
    cache_dirty = False
    cache_meta: dict[str, str] = {}
    notes: list[str] = []

    need_device = bool(sections & {"device", "location", "nearby", "memory", "extra"})
    need_runtime = "runtime" in sections
    need_time = "time" in sections
    need_net = bool(sections & {"network", "location", "weather", "memory", "extra"})
    need_vitals = "vitals" in sections
    need_nearby = "nearby" in sections

    # Phase 1: Run all local probes in parallel (<100ms typical)
    local_results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {}
        if need_device:
            futs["device"] = pool.submit(probe_device, config)
        if need_runtime:
            futs["runtime"] = pool.submit(probe_runtime)
        if need_time:
            futs["time"] = pool.submit(probe_time)
        if need_net:
            futs["network"] = pool.submit(probe_network_local)
        if need_vitals:
            futs["vitals"] = pool.submit(probe_vitals)
        for name, fut in futs.items():
            try:
                local_results[name] = fut.result(timeout=max(0.25, deadline - time.monotonic()))
            except Exception:
                notes.append(f"{name}:timeout")
                local_results[name] = {}

    device = local_results.get("device") or {}
    runtime = local_results.get("runtime") or {}
    time_info = local_results.get("time") or {}
    network = local_results.get("network") or {}
    vitals = local_results.get("vitals") or {}

    # Local location + nearby + drop-in providers (parallel, fast)
    location: dict[str, Any] = {}
    nearby: dict[str, Any] = {}
    extra: dict[str, Any] = {}

    want_loc = bool(sections & {"location", "weather", "memory"})
    want_extra = "extra" in sections

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_loc = pool.submit(probe_location_local, config, network, device) if want_loc else None
        f_near = pool.submit(probe_nearby, device, config) if need_nearby else None
        f_prov = (
            pool.submit(
                run_providers,
                {
                    "WHEREABOUT_HOST": str(device.get("host") or ""),
                    "WHEREABOUT_OS": str(device.get("os") or ""),
                    "WHEREABOUT_DEVICE": str(device.get("id") or ""),
                    "WHEREABOUT_FINGERPRINT": str(network.get("fingerprint") or ""),
                },
            )
            if want_extra
            else None
        )
        if f_loc:
            try:
                location = f_loc.result(timeout=max(0.2, deadline - time.monotonic()))
            except Exception:
                pass
        if f_near:
            try:
                nearby = f_near.result(timeout=max(0.2, deadline - time.monotonic()))
            except Exception:
                pass
        if f_prov:
            try:
                extra = f_prov.result(timeout=max(0.2, min(0.5, deadline - time.monotonic())))
            except Exception:
                pass

    # Allow drop-in providers to supply optional `_place` or `_body` hints
    for prov_data in extra.values():
        if isinstance(prov_data, dict):
            if prov_data.get("_place") and not location.get("place"):
                location["place"] = str(prov_data["_place"])
            if prov_data.get("_body") and not device.get("body"):
                device["body"] = str(prov_data["_body"])

    # Phase 2: Remote enrichment (IP geo -> reverse geocode -> weather), strictly cached & bounded
    need_remote_geo = bool(sections & {"network", "location", "weather"})
    need_weather = "weather" in sections
    fingerprint = str(network.get("fingerprint") or "default")

    ip_geo: dict[str, Any] | None = None
    geo_age: int | None = None

    if need_remote_geo and not fresh:
        ip_geo, geo_age = get_cached(cache, "ip_geo", fingerprint, GEO_TTL_SECONDS)
        if ip_geo:
            cache_meta["geo"] = f"hit:{geo_age}s"

    rem_time = deadline - time.monotonic()
    if need_remote_geo and not ip_geo and not fast and network.get("online", True) and rem_time > 0.25:
        ip_geo = probe_ip_geo(timeout=min(1.2, rem_time * 0.6))
        if ip_geo:
            put_cached(cache, "ip_geo", fingerprint, ip_geo)
            cache_dirty = True
            cache_meta["geo"] = "miss"
            geo_age = 0
        else:
            notes.append("geo:unavailable")
    elif fast and need_remote_geo and not ip_geo:
        cache_meta["geo"] = "skipped:fast"

    if ip_geo:
        if ip_geo.get("public_ip"):
            network["public_ip"] = ip_geo["public_ip"]
        if ip_geo.get("isp"):
            network["isp"] = ip_geo["isp"]
        # Re-check configured places now that public_ip is known
        if not location.get("place") and ip_geo.get("public_ip"):
            p = match_place(config, public_ip=str(ip_geo["public_ip"]))
            if p:
                location["place"] = str(p["name"])
        # Fill missing location fields from IP geo (never overwrite hardware GPS coords!)
        if "lat" not in location and "lat" in ip_geo and "lon" in ip_geo:
            location["lat"] = ip_geo["lat"]
            location["lon"] = ip_geo["lon"]
            location["accuracy_m"] = ip_geo.get("accuracy_m", 5000)
            location["source"] = "ip"
            if geo_age is not None:
                location["age_s"] = geo_age
        for k in ("city", "region", "country"):
            if not location.get(k) and ip_geo.get(k):
                location[k] = ip_geo[k]

    # Phase 3: Reverse geocode (only if GPS coords lack city) + Weather in parallel
    weather: dict[str, Any] = {}
    lat = location.get("lat")
    lon = location.get("lon")
    has_coords = isinstance(lat, (int, float)) and isinstance(lon, (int, float))
    coord_key = f"{round(float(lat), 2):.2f},{round(float(lon), 2):.2f}" if has_coords else ""

    need_rev = has_coords and not location.get("city") and "location" in sections
    rev_data: dict[str, str] | None = None
    if need_rev and not fresh:
        rev_data, _ = get_cached(cache, "geocode", coord_key, GEOCODE_TTL_SECONDS)

    wx_data: dict[str, Any] | None = None
    wx_age: int | None = None
    if need_weather and has_coords and not fresh:
        wx_data, wx_age = get_cached(cache, "weather", coord_key, WEATHER_TTL_SECONDS)
        if wx_data:
            cache_meta["weather"] = f"hit:{wx_age}s"

    rem_time = deadline - time.monotonic()
    fetch_rev = need_rev and not rev_data and not fast and rem_time > 0.2
    fetch_wx = need_weather and has_coords and not wx_data and not fast and rem_time > 0.2

    if fetch_rev or fetch_wx:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_rev = pool.submit(probe_reverse_geocode, float(lat), float(lon), min(1.0, rem_time)) if fetch_rev else None
            f_wx = pool.submit(probe_weather, float(lat), float(lon), min(1.2, rem_time)) if fetch_wx else None
            if f_rev:
                try:
                    rev_data = f_rev.result(timeout=max(0.15, deadline - time.monotonic()))
                    if rev_data:
                        put_cached(cache, "geocode", coord_key, rev_data)
                        cache_dirty = True
                except Exception:
                    pass
            if f_wx:
                try:
                    wx_data = f_wx.result(timeout=max(0.15, deadline - time.monotonic()))
                    if wx_data:
                        put_cached(cache, "weather", coord_key, wx_data)
                        cache_dirty = True
                        cache_meta["weather"] = "miss"
                        wx_age = 0
                    else:
                        notes.append("weather:unavailable")
                except Exception:
                    notes.append("weather:timeout")
    elif fast and need_weather and not wx_data:
        cache_meta["weather"] = "skipped:fast"

    if rev_data:
        for k in ("city", "region", "country"):
            if not location.get(k) and rev_data.get(k):
                location[k] = rev_data[k]

    if wx_data:
        weather = dict(wx_data)
        if wx_age is not None:
            weather["age_s"] = wx_age

    # Privacy rounding on coordinates unless --precise
    if has_coords and not precise:
        location["lat"] = round(float(location["lat"]), 2)
        location["lon"] = round(float(location["lon"]), 2)
    elif has_coords:
        location["lat"] = round(float(location["lat"]), 5)
        location["lon"] = round(float(location["lon"]), 5)

    # Phase 4: Local SQLite place memory (<2ms) — visit/transition ledger + curated notes
    memory: dict[str, Any] = {}
    if sections & {"memory", "location"}:
        try:
            memory = observe_and_recall(
                network,
                location,
                device,
                track=track and ("memory" in sections or only is None),
            )
        except Exception:
            notes.append("memory:unavailable")
        if memory and not location.get("place"):
            lbl = str(memory.get("label") or "")
            sub = str(memory.get("subplace") or "")
            if lbl and sub and lbl != sub:
                location["place"] = f"{lbl} / {sub}"
            elif lbl or sub:
                location["place"] = lbl or sub

    if cache_dirty:
        save_cache(cache)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    out: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": time_info.get("iso") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_ms": elapsed_ms,
    }
    if "device" in sections:
        out["device"] = device
    if "runtime" in sections:
        out["runtime"] = runtime
    if "time" in sections:
        out["time"] = time_info
    if "network" in sections:
        out["network"] = network
    if "location" in sections:
        out["location"] = location
    if "weather" in sections:
        out["weather"] = weather
    if "vitals" in sections:
        out["vitals"] = vitals
    if "nearby" in sections and nearby:
        out["nearby"] = nearby
    if "memory" in sections and memory:
        out["memory"] = memory
    if "extra" in sections and extra:
        out["extra"] = extra

    meta: dict[str, Any] = {
        "mode": "fast" if fast else ("fresh" if fresh else "auto"),
        "units": str(config.get("units") or "both"),
    }
    if cache_meta:
        meta["cache"] = cache_meta
    if notes:
        meta["notes"] = notes
    out["meta"] = meta
    return out
