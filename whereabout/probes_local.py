"""Fast local situational probes (zero outbound network, target <100ms in parallel)."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sys
import time
from datetime import datetime
from typing import Any

from .cache import match_place
from .protocol import fmt_duration, read_first_line, run_cmd, tcp_open

TERMUX_BIN = "/data/data/com.termux/files/usr/bin"


def is_android() -> bool:
    return (
        os.path.exists("/system/bin/getprop")
        or "com.termux" in os.environ.get("PREFIX", "")
        or bool(os.environ.get("ANDROID_ROOT"))
    )


def _read_homephone_device_key() -> str:
    """Safely read ONLY HOMEPHONE_DEVICE from ~/.homephone_env (never reads secrets)."""
    env_val = os.environ.get("WHEREABOUT_DEVICE") or os.environ.get("HOMEPHONE_DEVICE")
    if env_val and env_val.strip():
        return env_val.strip()
    path = os.path.expanduser("~/.homephone_env")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s.startswith("HOMEPHONE_DEVICE="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _mac_platform_model() -> tuple[str, str]:
    """Return (model_id, friendly_name) on macOS; works inside Seatbelt sandboxes too."""
    model_id = ""
    rc, out = run_cmd(["sysctl", "-n", "hw.model"], timeout=0.25)
    if rc == 0 and out:
        model_id = out
    else:
        rc, io_out = run_cmd(["ioreg", "-d2", "-c", "IOPlatformExpertDevice"], timeout=0.35)
        if rc == 0 and io_out:
            m = re.search(r'"model"\s*=\s*<"([^"]+)">', io_out)
            if m:
                model_id = m.group(1)
    rc_n, cname = run_cmd(["scutil", "--get", "ComputerName"], timeout=0.25)
    friendly = cname if rc_n == 0 and cname else ""
    return model_id, friendly


def _linux_os_pretty() -> str:
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return platform.release()


def _detect_container() -> str:
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.path.exists("/run/.containerenv"):
        return "podman"
    if os.environ.get("PROOT_TMP_DIR") or os.environ.get("PROOT_NO_SECCOMP"):
        return "proot"
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as f:
            cg = f.read(2048)
        if "docker" in cg:
            return "docker"
        if "kubepods" in cg:
            return "kubernetes"
        if "lxc" in cg:
            return "lxc"
    except Exception:
        pass
    return ""


def _mac_has_battery() -> bool:
    rc, out = run_cmd(["ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"], timeout=0.35)
    return rc == 0 and "AppleSmartBattery" in out


def _linux_has_battery() -> bool:
    ps = "/sys/class/power_supply"
    if not os.path.isdir(ps):
        return False
    try:
        for name in os.listdir(ps):
            if name.startswith(("BAT", "battery")):
                return True
    except Exception:
        pass
    return False


def probe_device(config: dict[str, Any]) -> dict[str, Any]:
    sys_name = platform.system()
    arch = platform.machine()
    host = socket.gethostname()
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or "unknown"

    os_name = sys_name.lower()
    os_version = platform.release()
    model = ""
    friendly_name = ""
    kind = "machine"
    body = _read_homephone_device_key()

    if sys_name == "Darwin":
        os_name = "macOS"
        mac_v = platform.mac_ver()[0]
        if not mac_v:
            _, mac_v = run_cmd(["sw_vers", "-productVersion"], timeout=0.25)
        os_version = mac_v or platform.release()
        model, friendly_name = _mac_platform_model()
        lower = f"{model} {friendly_name}".lower()
        if "book" in lower or _mac_has_battery():
            kind = "laptop"
        else:
            kind = "desktop"
        if not body and config.get("device_id"):
            body = str(config["device_id"])
    elif is_android():
        os_name = "Android"
        _, rel = run_cmd(["/system/bin/getprop", "ro.build.version.release"], timeout=0.3, strip_ld=True)
        _, mfg = run_cmd(["/system/bin/getprop", "ro.product.manufacturer"], timeout=0.3, strip_ld=True)
        _, mod = run_cmd(["/system/bin/getprop", "ro.product.model"], timeout=0.3, strip_ld=True)
        _, chars = run_cmd(["/system/bin/getprop", "ro.build.characteristics"], timeout=0.3, strip_ld=True)
        os_version = rel or platform.release()
        model = f"{mfg} {mod}".strip() if mfg and mod and not mod.lower().startswith(mfg.lower()) else (mod or mfg)
        kind = "tablet" if "tablet" in chars.lower() else "phone"
        if not body:
            slug = re.sub(r"[^a-z0-9]+", "", mod.lower())
            body = f"{kind}-{slug}" if slug else kind
        friendly_name = model or body
    elif sys_name == "Linux":
        os_name = "Linux"
        os_version = _linux_os_pretty()
        dmi = read_first_line("/sys/devices/virtual/dmi/id/product_name")
        dt_model = read_first_line("/proc/device-tree/model").rstrip("\x00")
        model = dt_model or dmi
        container = _detect_container()
        if container:
            kind = "container"
        elif "raspberry pi" in model.lower() or "jetson" in model.lower():
            kind = "sbc"
        elif _linux_has_battery():
            kind = "laptop"
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            kind = "desktop"
        else:
            kind = "server"
    else:
        os_name = sys_name
        os_version = platform.version()

    device_id = str(config.get("device_id") or body or host)
    device_name = str(config.get("device_name") or friendly_name or model or host)

    result: dict[str, Any] = {
        "kind": kind,
        "id": device_id,
        "name": device_name,
        "os": os_name,
        "os_version": os_version,
        "arch": arch,
        "host": host,
        "user": user,
    }
    if model:
        result["model"] = model
    if body:
        result["body"] = body
    return result


def _git_info(cwd: str) -> dict[str, Any] | None:
    rc, top = run_cmd(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=0.25)
    if rc != 0 or not top:
        return None
    repo = os.path.basename(top)
    _, branch = run_cmd(["git", "-C", cwd, "symbolic-ref", "--short", "HEAD"], timeout=0.2)
    if not branch:
        _, branch = run_cmd(["git", "-C", cwd, "rev-parse", "--short", "HEAD"], timeout=0.2)
    rc_s, st = run_cmd(["git", "-C", cwd, "status", "--porcelain"], timeout=0.3)
    dirty = len(st.splitlines()) if rc_s == 0 and st else 0
    return {"repo": repo, "branch": branch or "detached", "dirty": dirty}


def probe_runtime() -> dict[str, Any]:
    flags: list[str] = []
    if is_android():
        if "com.termux" in os.environ.get("PREFIX", ""):
            flags.append("termux")
        else:
            flags.append("android")
    container = _detect_container()
    if container:
        flags.append(container)
    if os.environ.get("WSL_DISTRO_NAME") or "microsoft" in platform.release().lower():
        flags.append("wsl")
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        flags.append("ssh")
    if platform.system() == "Darwin":
        if os.environ.get("APP_SANDBOX_CONTAINER_ID"):
            flags.append("sandbox")
        else:
            rc, _ = run_cmd(["sysctl", "-n", "kern.ostype"], timeout=0.2)
            if rc != 0:
                flags.append("sandbox")
    if not flags:
        flags.append("local")

    tty = sys.stdout.isatty()
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    short_cwd = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + os.sep) else cwd

    shell = os.path.basename(os.environ.get("SHELL", "")) or None
    out: dict[str, Any] = {
        "flags": flags,
        "tty": tty,
        "cwd": short_cwd,
    }
    if shell:
        out["shell"] = shell
    git = _git_info(cwd)
    if git:
        out["git"] = git
    return out


def _uptime_seconds() -> int | None:
    if os.path.exists("/proc/uptime"):
        raw = read_first_line("/proc/uptime")
        if raw:
            try:
                return int(float(raw.split()[0]))
            except ValueError:
                return None
    if platform.system() == "Darwin":
        rc, out = run_cmd(["sysctl", "-n", "kern.boottime"], timeout=0.25)
        if rc == 0 and out:
            m = re.search(r"sec\s*=\s*(\d+)", out)
            if m:
                return max(0, int(time.time()) - int(m.group(1)))
        rc_u, u_out = run_cmd(["uptime"], timeout=0.25)
        if rc_u == 0 and "up " in u_out:
            seg = u_out.split("up ", 1)[1]
            days = 0
            hours = 0
            mins = 0
            md = re.search(r"(\d+)\s+day", seg)
            if md:
                days = int(md.group(1))
            mhm = re.search(r"(\d+):(\d+)", seg)
            if mhm:
                hours, mins = int(mhm.group(1)), int(mhm.group(2))
            else:
                mm = re.search(r"(\d+)\s+min", seg)
                if mm:
                    mins = int(mm.group(1))
            total = days * 86400 + hours * 3600 + mins * 60
            if total > 0:
                return total
    return None


def probe_time() -> dict[str, Any]:
    now = datetime.now().astimezone()
    hour = now.hour
    if 5 <= hour < 12:
        phase = "morning"
    elif 12 <= hour < 17:
        phase = "afternoon"
    elif 17 <= hour < 21:
        phase = "evening"
    else:
        phase = "night"

    offset = now.strftime("%z")
    utc_offset = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "UTC"
    tz = now.tzname() or utc_offset
    up_s = _uptime_seconds()

    out: dict[str, Any] = {
        "iso": now.isoformat(timespec="seconds"),
        "local": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": now.strftime("%a"),
        "tz": tz,
        "utc_offset": utc_offset,
        "phase": phase,
    }
    if up_s is not None:
        out["uptime_s"] = up_s
        out["uptime"] = fmt_duration(up_s)
    return out


_MAC_RE = re.compile(r"\b([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})\b", re.I)


def _norm_mac(raw: str) -> str:
    parts = raw.strip().lower().split(":")
    if len(parts) != 6:
        return ""
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return ""


def _wifi_band(freq_mhz: int) -> tuple[str, int] | None:
    """Return (band, channel) for a Wi-Fi frequency. None when it is not a standard channel."""
    if 2412 <= freq_mhz <= 2472:
        return "2.4GHz", (freq_mhz - 2407) // 5
    if freq_mhz == 2484:
        return "2.4GHz", 14
    if 5180 <= freq_mhz <= 5885:
        return "5GHz", (freq_mhz - 5000) // 5
    if 5955 <= freq_mhz <= 7115:
        return "6GHz", (freq_mhz - 5950) // 5
    return None


def _apply_radio(net: dict[str, Any], freq_mhz: int | None, bssid: str = "") -> None:
    mac = _norm_mac(bssid) if bssid else ""
    if mac and mac != "00:00:00:00:00:00":
        net["bssid"] = mac
    if isinstance(freq_mhz, int) and freq_mhz > 0:
        net["freq_mhz"] = freq_mhz
        band = _wifi_band(freq_mhz)
        if band:
            net["band"], net["channel"] = band


def _gateway_mac(gateway: str) -> str:
    """MAC of the default router. Stable across extenders; changes when the router changes."""
    if not gateway:
        return ""
    if platform.system() == "Darwin":
        rc, out = run_cmd(["arp", "-n", gateway], timeout=0.25)
        if rc == 0:
            found = _MAC_RE.search(out)
            if found:
                return _norm_mac(found.group(1))
        return ""
    try:
        with open("/proc/net/arp", "r", encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == gateway:
                    mac = _norm_mac(parts[3])
                    if mac and mac != "00:00:00:00:00:00":
                        return mac
    except Exception:
        pass
    return ""


def _dhcp_router_id(iface: str) -> str:
    """Router model and serial from the DHCP vendor payload, when the AP name is hidden.

    macOS redacts the SSID and BSSID for ordinary commands. The DHCP packet is still
    readable, and some routers (Verizon's CR1000A, for one) put their model and serial in it.
    """
    if not iface or platform.system() != "Darwin":
        return ""
    rc, out = run_cmd(["ipconfig", "getpacket", iface], timeout=0.35)
    if rc != 0 or "option_125" not in out:
        return ""
    payload = bytearray()
    capturing = False
    for line in out.splitlines():
        if line.startswith("option_125"):
            capturing = True
            continue
        if not capturing:
            continue
        found = re.match(r"\s*[0-9a-fA-F]{4}\s{2}((?:[0-9a-fA-F]{2}\s+){1,16})", line)
        if not found:
            if line.strip() and not line.startswith((" ", "\t")):
                break
            continue
        for byte in found.group(1).split():
            payload.append(int(byte, 16))
    if len(payload) < 6:
        return ""
    body = payload[5:]
    tokens: list[str] = []
    index = 0
    while index + 2 <= len(body):
        length = body[index + 1]
        chunk = body[index + 2:index + 2 + length]
        index += 2 + length
        try:
            text = chunk.decode("ascii")
        except UnicodeDecodeError:
            continue
        if (
            len(text) >= 7
            and text.isprintable()
            and any(ch.isalpha() for ch in text)
            and any(ch.isdigit() for ch in text)
        ):
            tokens.append(text)
    if not tokens:
        return ""
    tokens.sort(key=len)
    return " ".join(dict.fromkeys(tokens))


def _udp_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.15)
            s.connect(("1.1.1.1", 80))
            return str(s.getsockname()[0])
    except Exception:
        return ""


def _probe_network_macos() -> dict[str, Any]:
    iface = ""
    gateway = ""
    local_ip = ""
    ssid = ""
    rssi: int | None = None
    transport = "unknown"
    vpn = False

    rc, out = run_cmd(["route", "-n", "get", "default"], timeout=0.3)
    if rc == 0 and out:
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("interface:"):
                iface = s.split(":", 1)[1].strip()
            elif s.startswith("gateway:"):
                gateway = s.split(":", 1)[1].strip()

    if iface.startswith(("utun", "ppp", "ipsec", "wg")):
        vpn = True
        transport = "vpn"
    elif iface:
        rc_s, summary = run_cmd(["ipconfig", "getsummary", iface], timeout=0.35)
        if rc_s == 0 and summary:
            for line in summary.splitlines():
                ls = line.strip()
                if ls.startswith("SSID :"):
                    val = ls.split(":", 1)[1].strip()
                    if val and val != "<redacted>":
                        ssid = val
                elif ls.startswith("LinkStatusActive : TRUE"):
                    pass
                elif "AirPort" in ls or "SSID" in ls:
                    transport = "wifi"
                elif ls.startswith("yiaddr =") or ls.startswith("ciaddr ="):
                    cand = ls.split("=", 1)[1].strip()
                    if cand and cand != "0.0.0.0":
                        local_ip = cand
        if transport == "unknown":
            rc_hw, hw = run_cmd(["networksetup", "-listallhardwareports"], timeout=0.35)
            if rc_hw == 0 and hw:
                blocks = hw.split("\n\n")
                for b in blocks:
                    if f"Device: {iface}" in b:
                        if "Wi-Fi" in b or "AirPort" in b:
                            transport = "wifi"
                        elif "Ethernet" in b or "Thunderbolt" in b or "USB" in b:
                            transport = "ethernet"
                        break
        if transport == "wifi" and not ssid:
            rc_w, wout = run_cmd(["networksetup", "-getairportnetwork", iface], timeout=0.35)
            if rc_w == 0 and "Current Wi-Fi Network:" in wout:
                val = wout.split(":", 1)[1].strip()
                if val and val != "<redacted>":
                    ssid = val
        if transport == "unknown" and iface == "en0":
            transport = "wifi"

    if not local_ip and iface:
        rc_ip, ip_out = run_cmd(["ipconfig", "getifaddr", iface], timeout=0.25)
        if rc_ip == 0 and ip_out:
            local_ip = ip_out
    if not local_ip:
        local_ip = _udp_local_ip()

    online = bool(iface or gateway or local_ip)
    net: dict[str, Any] = {
        "online": online,
        "transport": transport if online else "offline",
        "interface": iface,
        "ssid": ssid,
        "rssi_dbm": rssi,
        "local_ip": local_ip,
        "gateway": gateway,
        "vpn": vpn,
    }
    router_id = _dhcp_router_id(iface)
    if router_id:
        net["router_id"] = router_id
    return net


def _probe_network_android_or_linux() -> dict[str, Any]:
    iface = ""
    gateway = ""
    local_ip = ""
    ssid = ""
    rssi: int | None = None
    carrier = ""
    rat = ""
    transport = "unknown"
    vpn = False
    android = is_android()
    ip_bin = "/system/bin/ip" if (android and os.path.exists("/system/bin/ip")) else "ip"

    rc, route = run_cmd([ip_bin, "route", "show", "default"], timeout=0.3, strip_ld=android)
    if rc == 0 and route:
        first = route.splitlines()[0].split()
        if "via" in first:
            idx = first.index("via")
            if idx + 1 < len(first):
                gateway = first[idx + 1]
        if "dev" in first:
            idx = first.index("dev")
            if idx + 1 < len(first):
                iface = first[idx + 1]

    if iface:
        rc_a, addr_out = run_cmd([ip_bin, "-4", "-o", "addr", "show", "dev", iface], timeout=0.3, strip_ld=android)
        if rc_a == 0 and addr_out:
            for line in addr_out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "inet":
                    local_ip = parts[3].split("/")[0]
                    break
    if not local_ip:
        local_ip = _udp_local_ip()

    if iface.startswith(("tun", "wg", "tailscale", "ppp")):
        vpn = True
        transport = "vpn"
    elif iface.startswith(("wlan", "wlp", "wl")):
        transport = "wifi"
    elif iface.startswith(("rmnet", "ccmni", "wwan", "v4-rmnet")):
        transport = "cellular"
    elif iface.startswith(("eth", "enp", "eno", "ens")):
        transport = "ethernet"

    # Wi-Fi signal from /proc/net/wireless
    if os.path.exists("/proc/net/wireless"):
        try:
            with open("/proc/net/wireless", "r", encoding="utf-8", errors="replace") as f:
                for line in f.readlines()[2:]:
                    if ":" in line:
                        w_if, rest = line.split(":", 1)
                        if not iface or w_if.strip() == iface:
                            parts = rest.split()
                            if len(parts) >= 3:
                                lvl = int(float(parts[2].rstrip(".")))
                                if lvl != 0:
                                    rssi = lvl
                                    if transport == "unknown":
                                        transport = "wifi"
        except Exception:
            pass

    wifi_info: dict[str, Any] = {}
    bssid = ""
    freq_mhz: int | None = None
    if android:
        _, carrier_raw = run_cmd(["/system/bin/getprop", "gsm.operator.alpha"], timeout=0.25, strip_ld=True)
        _, rat_raw = run_cmd(["/system/bin/getprop", "gsm.network.type"], timeout=0.25, strip_ld=True)
        carrier = carrier_raw.split(",")[0].strip() if carrier_raw else ""
        rat = rat_raw.split(",")[0].strip() if rat_raw else ""
        if os.path.exists(f"{TERMUX_BIN}/termux-wifi-connectioninfo"):
            # The Termux:API app answers in about a third of a second. 0.4s was dropping it.
            rc_tw, tw = run_cmd([f"{TERMUX_BIN}/termux-wifi-connectioninfo"], timeout=0.9)
            if rc_tw == 0 and tw:
                try:
                    parsed = json.loads(tw)
                    if isinstance(parsed, dict):
                        wifi_info = parsed
                except Exception:
                    pass
        if wifi_info:
            s_val = str(wifi_info.get("ssid") or "").strip().strip('"')
            if s_val and s_val.lower() not in ("<unknown ssid>", "unknown"):
                ssid = s_val
            if isinstance(wifi_info.get("rssi"), int) and wifi_info["rssi"] < 0:
                rssi = wifi_info["rssi"]
            if not local_ip and wifi_info.get("ip"):
                local_ip = str(wifi_info["ip"])
            if wifi_info.get("supplicant_state") == "COMPLETED" or ssid:
                transport = "wifi"
                if not iface:
                    iface = "wlan0"
    else:
        if transport == "wifi" and iface:
            rc_iw, iw = run_cmd(["iw", "dev", iface, "link"], timeout=0.35)
            if rc_iw == 0 and iw:
                for line in iw.splitlines():
                    item = line.strip()
                    if item.startswith("Connected to "):
                        found = _MAC_RE.search(item)
                        if found:
                            bssid = _norm_mac(found.group(1))
                    elif item.startswith("SSID:"):
                        ssid = item.split(":", 1)[1].strip() or ssid
                    elif item.startswith("freq:"):
                        freq_text = item.split(":", 1)[1].strip().split()[0]
                        if freq_text.isdigit():
                            freq_mhz = int(freq_text)
                    elif item.startswith("signal:"):
                        sig = item.split(":", 1)[1].strip().split()[0]
                        try:
                            rssi = int(float(sig))
                        except ValueError:
                            pass
            if not ssid:
                rc_id, iw_id = run_cmd(["iwgetid", "-r"], timeout=0.25)
                if rc_id == 0 and iw_id:
                    ssid = iw_id
                elif shutil.which("nmcli"):
                    rc_nm, nm = run_cmd(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], timeout=0.35)
                    if rc_nm == 0 and nm:
                        for line in nm.splitlines():
                            if line.startswith("yes:"):
                                ssid = line.split(":", 1)[1].strip()
                                break

    online = bool(iface or gateway or local_ip or (carrier and rat) or ssid)
    res: dict[str, Any] = {
        "online": online,
        "transport": transport if online else "offline",
        "interface": iface,
        "ssid": ssid,
        "rssi_dbm": rssi,
        "local_ip": local_ip,
        "gateway": gateway,
        "vpn": vpn,
    }
    if wifi_info:
        freq = wifi_info.get("frequency_mhz")
        _apply_radio(res, freq if isinstance(freq, int) else None, str(wifi_info.get("bssid") or ""))
    elif bssid or freq_mhz:
        _apply_radio(res, freq_mhz, bssid)
    if carrier:
        res["carrier"] = carrier
    if rat and rat.lower() != "unknown":
        res["rat"] = rat
    return res


def probe_network_local() -> dict[str, Any]:
    if platform.system() == "Darwin":
        net = _probe_network_macos()
    else:
        net = _probe_network_android_or_linux()

    mac = _gateway_mac(str(net.get("gateway") or ""))
    if mac:
        net["gateway_mac"] = mac

    local_ip = str(net.get("local_ip") or "")
    subnet = ".".join(local_ip.split(".")[:3]) if local_ip.count(".") == 3 else local_ip
    # The access point (BSSID) is reported, but it is not part of this key.
    # Walking between extenders of one router should not throw away the weather cache.
    tz_name = time.tzname[0] if time.tzname else ""
    fp_parts = [
        net.get("gateway") or "",
        net.get("gateway_mac") or "",
        net.get("ssid") or "",
        net.get("router_id") or "",
        subnet,
        net.get("carrier") or "",
        "vpn" if net.get("vpn") else "",
        tz_name,
    ]
    net["fingerprint"] = hashlib.sha256("|".join(str(part) for part in fp_parts).encode("utf-8")).hexdigest()[:16]
    for key in ("ssid", "rssi_dbm", "interface", "local_ip", "gateway", "bssid", "router_id", "gateway_mac"):
        if net.get(key) in ("", None):
            net.pop(key, None)
    return net


def _probe_vitals_macos() -> dict[str, Any]:
    out: dict[str, Any] = {}
    # Battery via ioreg (works even inside Seatbelt sandbox!)
    rc, ioreg = run_cmd(["ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"], timeout=0.35)
    if rc == 0 and "AppleSmartBattery" in ioreg:
        cur = re.search(r'"CurrentCapacity"\s*=\s*(\d+)', ioreg)
        mx = re.search(r'"MaxCapacity"\s*=\s*(\d+)', ioreg)
        ext = re.search(r'"ExternalConnected"\s*=\s*(Yes|No)', ioreg)
        chg = re.search(r'"IsCharging"\s*=\s*(Yes|No)', ioreg)
        rem = re.search(r'"TimeRemaining"\s*=\s*(\d+)', ioreg)
        if cur and mx and int(mx.group(1)) > 0:
            pct = min(100, round(int(cur.group(1)) * 100 / int(mx.group(1))))
            out["battery_pct"] = pct
        if chg and chg.group(1) == "Yes":
            out["power"] = "charging"
        elif ext and ext.group(1) == "Yes":
            out["power"] = "ac"
        elif ext and ext.group(1) == "No":
            out["power"] = "battery"
        if rem:
            mins = int(rem.group(1))
            if 0 < mins < 6000:
                out["battery_mins"] = mins
    else:
        rc_p, pm = run_cmd(["pmset", "-g", "batt"], timeout=0.25)
        if rc_p == 0 and pm:
            m_pct = re.search(r"(\d+)%", pm)
            if m_pct:
                out["battery_pct"] = int(m_pct.group(1))
            if "AC Power" in pm:
                out["power"] = "charging" if "charging;" in pm else "ac"
            elif "Battery Power" in pm:
                out["power"] = "battery"

    # RAM via sysconf + vm_stat (works inside sandbox!)
    total_bytes = 0
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        rc_m, ms = run_cmd(["sysctl", "-n", "hw.memsize"], timeout=0.2)
        if rc_m == 0 and ms.isdigit():
            total_bytes = int(ms)
    if total_bytes > 0:
        out["ram_total_gb"] = round(total_bytes / (1024 ** 3), 1)
        rc_v, vm = run_cmd(["vm_stat"], timeout=0.25)
        if rc_v == 0 and vm:
            m_ps = re.search(r"page size of (\d+) bytes", vm)
            page_size = int(m_ps.group(1)) if m_ps else 16384
            counts: dict[str, int] = {}
            for line in vm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    v_clean = v.strip().rstrip(".")
                    if v_clean.isdigit():
                        counts[k.strip()] = int(v_clean)
            used_pages = (
                counts.get("Pages active", 0)
                + counts.get("Pages wired down", 0)
                + counts.get("Pages occupied by compressor", 0)
            )
            if used_pages > 0:
                used_bytes = used_pages * page_size
                out["ram_used_gb"] = round(used_bytes / (1024 ** 3), 1)
                out["ram_pct"] = min(100, round(used_bytes * 100 / total_bytes))
    return out


def _probe_vitals_linux() -> dict[str, Any]:
    out: dict[str, Any] = {}
    # Battery
    for bdir in ("/sys/class/power_supply/battery", "/sys/class/power_supply/BAT0", "/sys/class/power_supply/BAT1"):
        if os.path.isdir(bdir):
            cap = read_first_line(os.path.join(bdir, "capacity"))
            status = read_first_line(os.path.join(bdir, "status")).lower()
            if cap.isdigit():
                out["battery_pct"] = int(cap)
            if status:
                out["power"] = "battery" if status == "discharging" else status
            break

    # RAM from /proc/meminfo
    mem: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    parts = v.split()
                    if parts and parts[0].isdigit():
                        mem[k.strip()] = int(parts[0])
    except Exception:
        pass
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    if total_kb > 0:
        used_kb = max(0, total_kb - avail_kb)
        out["ram_total_gb"] = round(total_kb / (1024 * 1024), 1)
        out["ram_used_gb"] = round(used_kb / (1024 * 1024), 1)
        out["ram_pct"] = min(100, round(used_kb * 100 / total_kb))
    return out


def probe_vitals() -> dict[str, Any]:
    out = _probe_vitals_macos() if platform.system() == "Darwin" else _probe_vitals_linux()
    try:
        out["cpus"] = os.cpu_count() or 1
    except Exception:
        pass
    try:
        load1 = os.getloadavg()[0]
        if load1 >= 0:
            out["load_1m"] = round(load1, 2)
    except Exception:
        pass
    try:
        du = shutil.disk_usage(os.getcwd())
        out["disk_free_gb"] = round(du.free / (1024 ** 3), 1)
    except Exception:
        pass
    return out


def probe_location_local(
    config: dict[str, Any],
    net: dict[str, Any],
    device: dict[str, Any],
) -> dict[str, Any]:
    """Resolve location from local sources (saved places, env override, Android GPS cache)."""
    loc: dict[str, Any] = {}

    # 1. Explicit env override
    env_loc = os.environ.get("WHEREABOUT_LOCATION", "").strip()
    if env_loc:
        loc["place"] = env_loc
        loc["source"] = "env"

    # 2. Config place match (by SSID, gateway, local IP prefix, hostname, device)
    place = match_place(
        config,
        ssid=str(net.get("ssid") or ""),
        bssid=str(net.get("bssid") or ""),
        gateway=str(net.get("gateway") or ""),
        gateway_mac=str(net.get("gateway_mac") or ""),
        local_ip=str(net.get("local_ip") or ""),
        host=str(device.get("host") or ""),
        device_id=str(device.get("id") or ""),
    )
    if place:
        loc["place"] = str(place["name"])
        for k in ("city", "region", "country"):
            if place.get(k):
                loc[k] = str(place[k])
        if isinstance(place.get("lat"), (int, float)) and isinstance(place.get("lon"), (int, float)):
            loc["lat"] = float(place["lat"])
            loc["lon"] = float(place["lon"])
            loc["source"] = "config"

    # 3. Android hardware GPS cache (HomePhone MainActivity or termux-location last fix)
    if is_android():
        gps_file = "/sdcard/Download/gps_location.json"
        if os.path.exists(gps_file):
            try:
                with open(gps_file, "r", encoding="utf-8") as gf:
                    g = json.load(gf)
                lat, lon = g.get("latitude"), g.get("longitude")
                ts_ms = float(g.get("timestamp_ms", 0))
                age_s = int(max(0.0, time.time() - ts_ms / 1000.0)) if ts_ms else None
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
                    if age_s is None or age_s < 1800:
                        loc["lat"] = float(lat)
                        loc["lon"] = float(lon)
                        if isinstance(g.get("accuracy"), (int, float)):
                            loc["accuracy_m"] = round(float(g["accuracy"]))
                        loc["source"] = str(g.get("provider") or "gps")
                        if age_s is not None:
                            loc["age_s"] = age_s
                        return loc
            except Exception:
                pass
        if os.path.exists(f"{TERMUX_BIN}/termux-location"):
            rc, out = run_cmd([f"{TERMUX_BIN}/termux-location", "-p", "passive", "-r", "last"], timeout=0.45)
            if rc == 0 and out:
                try:
                    g = json.loads(out)
                    lat, lon = g.get("latitude"), g.get("longitude")
                    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
                        loc["lat"] = float(lat)
                        loc["lon"] = float(lon)
                        if isinstance(g.get("accuracy"), (int, float)):
                            loc["accuracy_m"] = round(float(g["accuracy"]))
                        loc["source"] = "termux-gps"
                except Exception:
                    pass

    return loc


DEFAULT_LOCAL_SERVICES: tuple[tuple[str, int], ...] = (
    ("ollama:11434", 11434),
    ("lmstudio:1234", 1234),
    ("homephone-server:7777", 7777),
    ("homephone-hub:8780", 8780),
    ("homephone-agent:8790", 8790),
)


def probe_nearby(device: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Detect local agent daemons and connected ADB companion phones (never spawns adb)."""
    candidates = list(DEFAULT_LOCAL_SERVICES)
    if isinstance(config, dict) and isinstance(config.get("services"), list):
        for entry in config["services"]:
            if isinstance(entry, dict) and entry.get("name") and isinstance(entry.get("port"), int):
                candidates.append((f"{entry['name']}:{entry['port']}", int(entry["port"])))

    services: list[str] = []
    seen_ports: set[int] = set()
    for label, port in candidates:
        if port in seen_ports:
            continue
        seen_ports.add(port)
        if tcp_open("127.0.0.1", port, timeout=0.05):
            services.append(label)

    adb_devices: list[dict[str, str]] = []
    if not is_android() and shutil.which("adb") and tcp_open("127.0.0.1", 5037, timeout=0.05):
        rc, out = run_cmd(["adb", "devices", "-l"], timeout=0.3)
        if rc == 0 and out:
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    serial = parts[0]
                    model = ""
                    for tok in parts[2:]:
                        if tok.startswith("model:"):
                            model = tok.split(":", 1)[1].replace("_", " ")
                    adb_devices.append({"serial": serial, "model": model or "android"})

    out: dict[str, Any] = {}
    if services:
        out["services"] = services
    if adb_devices:
        out["adb_devices"] = adb_devices
    return out
