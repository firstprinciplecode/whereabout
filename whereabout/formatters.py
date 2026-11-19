"""Token-efficient formatters for AI agents and humans."""

from __future__ import annotations

import json
from typing import Any

from .protocol import fmt_age


def _format_temp(wx: dict[str, Any], units: str) -> str:
    tf = wx.get("temp_f")
    tc = wx.get("temp_c")
    if tf is None and tc is None:
        return ""
    if units == "metric" and tc is not None:
        s = f"{round(tc)}°C"
        if wx.get("feels_c") is not None and abs(wx["feels_c"] - tc) >= 2:
            s += f" (feels {round(wx['feels_c'])}°C)"
        return s
    if units == "imperial" and tf is not None:
        s = f"{round(tf)}°F"
        if wx.get("feels_f") is not None and abs(wx["feels_f"] - tf) >= 3:
            s += f" (feels {round(wx['feels_f'])}°F)"
        return s
    # default: both
    s = f"{round(tf)}°F / {round(tc)}°C" if (tf is not None and tc is not None) else f"{round(tf)}°F"
    if wx.get("feels_f") is not None and tf is not None and abs(wx["feels_f"] - tf) >= 3:
        s += f" (feels {round(wx['feels_f'])}°F)"
    return s


def _format_place(loc: dict[str, Any]) -> str:
    parts = [loc.get(k) for k in ("city", "region", "country") if loc.get(k)]
    # Deduplicate adjacent identical strings (e.g. Singapore, Singapore)
    dedup: list[str] = []
    for p in parts:
        if not dedup or dedup[-1] != p:
            dedup.append(str(p))
    city_str = ", ".join(dedup)
    place = str(loc.get("place") or "")
    if place and city_str:
        return f"{place} ({city_str})"
    return place or city_str


def format_compact(data: dict[str, Any]) -> str:
    """Dense, aligned ~8-line situational report (~120-160 tokens)."""
    lines: list[str] = []
    schema = data.get("schema", "whereabout/v1")
    elapsed = data.get("elapsed_ms")
    units = str((data.get("meta") or {}).get("units") or "both")

    # Header line with time
    t = data.get("time") or {}
    if t:
        t_bits = [f"{t.get('local', '')} {t.get('tz', '')} ({t.get('utc_offset', '')})".strip()]
        if t.get("weekday") and t.get("phase"):
            t_bits.append(f"{t['weekday']} {t['phase']}")
        if t.get("uptime"):
            t_bits.append(f"up {t['uptime']}")
        if elapsed is not None:
            t_bits.append(f"{elapsed}ms")
        lines.append(f"{schema:<11} {' · '.join(t_bits)}")
    else:
        hdr = f"{elapsed}ms" if elapsed is not None else ""
        lines.append(f"{schema:<11} {hdr}".rstrip())

    # Device / body
    d = data.get("device") or {}
    if d:
        d_bits = [d.get("kind", "machine")]
        spec_parts = [p for p in (d.get("name"), d.get("model")) if p]
        # Avoid repeating identical name and model
        if len(spec_parts) == 2 and spec_parts[1].lower() in spec_parts[0].lower():
            spec_parts = [spec_parts[0]]
        label = " / ".join(spec_parts)
        os_str = " ".join(p for p in (d.get("os"), d.get("os_version")) if p)
        arch = d.get("arch", "")
        if label:
            d_bits.append(f"{label} ({os_str}, {arch})" if os_str else label)
        elif os_str:
            d_bits.append(f"{os_str} ({arch})")
        if d.get("body"):
            d_bits.append(f"body={d['body']}")
        if d.get("user") and d.get("host"):
            d_bits.append(f"{d['user']}@{d['host']}")
        lines.append(f"{'device':<11} {' · '.join(d_bits)}")

    # Runtime / workspace
    rt = data.get("runtime") or {}
    if rt:
        r_bits = ["+".join(rt.get("flags") or ["local"])]
        r_bits.append("tty" if rt.get("tty") else "pipe")
        if rt.get("cwd"):
            cwd_str = f"cwd {rt['cwd']}"
            git = rt.get("git")
            if isinstance(git, dict):
                dirty = f"*{git['dirty']}" if git.get("dirty") else ""
                cwd_str += f" (git: {git.get('branch', '?')}{dirty})"
            r_bits.append(cwd_str)
        lines.append(f"{'runtime':<11} {' · '.join(r_bits)}")

    # Network
    n = data.get("network") or {}
    if n:
        n_bits = ["online" if n.get("online") else "offline"]
        if n.get("online"):
            tr = n.get("transport", "unknown")
            if n.get("interface"):
                tr = f"{tr} ({n['interface']})"
            if n.get("vpn") and not tr.startswith("vpn"):
                tr += "+vpn"
            n_bits.append(tr)
            radio: list[str] = []
            if n.get("ssid"):
                radio.append(f'"{n["ssid"]}"')
            if n.get("band") and n.get("channel"):
                radio.append(f"{n['band']} ch{n['channel']}")
            elif n.get("freq_mhz"):
                radio.append(f"{n['freq_mhz']}MHz")
            if n.get("bssid"):
                radio.append(f"ap {n['bssid']}")
            if n.get("rssi_dbm") is not None:
                radio.append(f"{n['rssi_dbm']}dBm")
            if radio:
                n_bits.append(" ".join(radio))
            if n.get("router_id"):
                n_bits.append(f"router {n['router_id']}")
            if n.get("carrier"):
                cell = n["carrier"]
                if n.get("rat"):
                    cell += f" {n['rat']}"
                n_bits.append(f"cell {cell}")
            if n.get("local_ip"):
                n_bits.append(f"local {n['local_ip']}")
            if n.get("gateway"):
                gw = f"gw {n['gateway']}"
                if n.get("gateway_mac"):
                    gw += f" ({n['gateway_mac']})"
                n_bits.append(gw)
            if n.get("public_ip"):
                pub = f"pub {n['public_ip']}"
                if n.get("isp"):
                    pub += f" ({n['isp']})"
                n_bits.append(pub)
        lines.append(f"{'net':<11} {' · '.join(n_bits)}")

    # Location
    loc = data.get("location")
    if loc is not None:
        if loc:
            l_bits: list[str] = []
            place_str = _format_place(loc)
            if place_str:
                l_bits.append(place_str)
            if "lat" in loc and "lon" in loc:
                l_bits.append(f"{loc['lat']}, {loc['lon']}")
            meta_bits = []
            if loc.get("source"):
                meta_bits.append(f"via {loc['source']}")
            if loc.get("accuracy_m") is not None and loc["accuracy_m"] < 1000:
                meta_bits.append(f"±{loc['accuracy_m']}m")
            if loc.get("age_s") is not None and loc["age_s"] >= 10:
                meta_bits.append(f"cached {fmt_age(loc['age_s'])}")
            if meta_bits:
                if l_bits:
                    l_bits[-1] = f"{l_bits[-1]} ({', '.join(meta_bits)})"
                else:
                    l_bits.append(f"({', '.join(meta_bits)})")
            lines.append(f"{'location':<11} {' · '.join(l_bits)}")
        else:
            mode = (data.get("meta") or {}).get("mode", "")
            reason = "skipped (--fast)" if mode == "fast" else "unavailable"
            lines.append(f"{'location':<11} {reason}")

    # Weather
    wx = data.get("weather")
    if wx is not None:
        if wx:
            w_bits: list[str] = []
            t_str = _format_temp(wx, units)
            if t_str:
                w_bits.append(t_str)
            if wx.get("condition"):
                cond = wx["condition"]
                if wx.get("is_day") is False and cond in ("Clear", "Mainly clear"):
                    cond += " (night)"
                w_bits.append(cond)
            if units == "metric" and wx.get("wind_kph") is not None:
                w_bits.append(f"wind {round(wx['wind_kph'])}kph")
            elif wx.get("wind_mph") is not None:
                w_bits.append(f"wind {round(wx['wind_mph'])}mph")
            if wx.get("humidity") is not None:
                w_bits.append(f"hum {wx['humidity']}%")
            if wx.get("precip_mm"):
                w_bits.append(f"precip {wx['precip_mm']}mm")
            if wx.get("age_s") is not None and wx["age_s"] >= 10:
                tag = f"(cached {fmt_age(wx['age_s'])})"
                if w_bits:
                    w_bits[-1] = f"{w_bits[-1]} {tag}"
                else:
                    w_bits.append(tag)
            lines.append(f"{'weather':<11} {' · '.join(w_bits)}")
        else:
            mode = (data.get("meta") or {}).get("mode", "")
            reason = "skipped (--fast)" if mode == "fast" else "unavailable"
            lines.append(f"{'weather':<11} {reason}")

    # Vitals
    v = data.get("vitals") or {}
    if v:
        v_bits: list[str] = []
        if v.get("battery_pct") is not None:
            b = f"bat {v['battery_pct']}%"
            if v.get("power"):
                b += f" ({v['power']})"
            if v.get("battery_mins"):
                h, m = divmod(int(v["battery_mins"]), 60)
                b += f" {h}h{m:02d}m left" if h else f" {m}m left"
            v_bits.append(b)
        elif v.get("power"):
            v_bits.append(f"power {v['power']}")
        if v.get("ram_used_gb") is not None and v.get("ram_total_gb") is not None:
            v_bits.append(f"ram {v['ram_used_gb']}/{v['ram_total_gb']}GB ({v.get('ram_pct', '?')}%)")
        if v.get("load_1m") is not None:
            v_bits.append(f"load {v['load_1m']}")
        if v.get("cpus"):
            v_bits.append(f"{v['cpus']}c")
        if v.get("disk_free_gb") is not None:
            v_bits.append(f"disk {v['disk_free_gb']}GB free")
        lines.append(f"{'vitals':<11} {' · '.join(v_bits)}")

    # Nearby companion daemons / devices
    nb = data.get("nearby") or {}
    if nb:
        nb_bits: list[str] = []
        if nb.get("services"):
            nb_bits.append("services: " + ", ".join(nb["services"]))
        if nb.get("adb_devices"):
            devs = [f"{x.get('model', 'android')} ({x.get('serial', '?')})" for x in nb["adb_devices"]]
            nb_bits.append("adb: " + ", ".join(devs))
        if nb_bits:
            lines.append(f"{'nearby':<11} {' · '.join(nb_bits)}")

    # Place memory (visits, dwell, transitions, curated notes)
    mem = data.get("memory") or {}
    if mem:
        m_bits: list[str] = []
        lbl_parts = [p for p in (mem.get("label"), mem.get("subplace")) if p]
        if len(lbl_parts) == 2 and lbl_parts[0] == lbl_parts[1]:
            lbl_parts = [lbl_parts[0]]
        if lbl_parts:
            m_bits.append(" / ".join(str(p) for p in lbl_parts))
        visits = mem.get("visits")
        dwell = mem.get("dwell")
        if visits == 1 and mem.get("status") == "new":
            m_bits.append("first visit here")
        elif visits:
            v_str = f"visit #{visits}"
            if dwell:
                v_str += f" ({dwell})"
            m_bits.append(v_str)
        if mem.get("previous"):
            m_bits.append(f"prev: {mem['previous']}")
        mem_notes = mem.get("notes") or []
        total_notes = int(mem.get("total_notes") or len(mem_notes))
        if total_notes and not mem_notes:
            m_bits.append(f"{total_notes} notes")
        elif total_notes > len(mem_notes):
            m_bits.append(f"showing {len(mem_notes)}/{total_notes} notes")
        if m_bits:
            lines.append(f"{'memory':<11} {' · '.join(m_bits)}")
        for item in mem_notes:
            tag = ""
            if item.get("kind") and item["kind"] != "note":
                tag += f"[{item['kind']}] "
            if item.get("scope") in ("ap", "area"):
                tag += f"({item['scope']}) "
            lines.append(f"{'':<11} - #{item.get('id', '?')} {tag}{item.get('text', '')}")

    # Drop-in provider extras
    extra = data.get("extra") or {}
    for pname, pval in extra.items():
        if isinstance(pval, dict):
            summary = " · ".join(
                f"{k}={val}" for k, val in pval.items() if not k.startswith("_") and val not in ("", None)
            )
            if summary:
                lines.append(f"{pname[:11]:<11} {summary}")

    return "\n".join(lines)


def format_oneline(data: dict[str, Any]) -> str:
    """Ultra-compact single line (~30-45 tokens) for prompt headers or quick checks."""
    parts: list[str] = []
    d = data.get("device") or {}
    if d:
        who = d.get("body") or d.get("host") or d.get("name") or ""
        os_tag = " ".join(p for p in (d.get("os"), d.get("os_version")) if p)
        parts.append(f"{d.get('kind', 'machine')} {who} ({os_tag})".strip())

    t = data.get("time") or {}
    if t:
        parts.append(f"{t.get('weekday', '')} {t.get('local', '')} {t.get('tz', '')}".strip())

    n = data.get("network") or {}
    if n:
        if n.get("online"):
            net_s = n.get("ssid") or n.get("transport", "online")
            if n.get("bssid"):
                net_s += " ap " + str(n["bssid"])[-8:]
            elif n.get("gateway_mac"):
                net_s += " gw " + str(n["gateway_mac"])[-8:]
            elif n.get("local_ip"):
                net_s += f" {n['local_ip']}"
            parts.append(net_s)
        else:
            parts.append("offline")

    loc = data.get("location") or {}
    place = _format_place(loc)
    if place:
        parts.append(place)

    mem = data.get("memory") or {}
    if mem:
        m_short: list[str] = []
        if mem.get("visits"):
            m_short.append(f"visit #{mem['visits']}")
        if mem.get("total_notes"):
            n_cnt = int(mem["total_notes"])
            m_short.append(f"{n_cnt} note" + ("s" if n_cnt != 1 else ""))
        if m_short:
            parts.append(" ".join(m_short))

    wx = data.get("weather") or {}
    if wx and wx.get("temp_f") is not None:
        parts.append(f"{round(wx['temp_f'])}°F {wx.get('condition', '')}".strip())

    v = data.get("vitals") or {}
    if v:
        v_s = []
        if v.get("battery_pct") is not None:
            v_s.append(f"bat {v['battery_pct']}%")
        if v.get("ram_pct") is not None:
            v_s.append(f"ram {v['ram_pct']}%")
        if v_s:
            parts.append(" ".join(v_s))

    rt = data.get("runtime") or {}
    if rt.get("cwd"):
        c = rt["cwd"]
        git = rt.get("git")
        if isinstance(git, dict):
            dirty = f"*{git['dirty']}" if git.get("dirty") else ""
            c += f" ({git.get('branch', '?')}{dirty})"
        parts.append(c)

    return " | ".join(p for p in parts if p)


def format_json(data: dict[str, Any], indent: int = 2) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False)
