# whereabout

Fast situational-awareness CLI, protocol (`whereabout/v1`), and local place memory for AI agents and humans.

Just as `whoami` answers *which user am I*, `whereabout` answers *where am I running right now*—in a single bounded command (~75ms warm, zero third-party dependencies):

- **Body & device:** laptop, desktop, server, phone/tablet (Android/Termux), container, SBC
- **Runtime:** local, SSH, Docker/Podman/proot, WSL, sandbox, TTY vs pipe, `cwd` + git branch/dirty state
- **Time:** local timestamp, timezone, UTC offset, phase of day, system uptime
- **Network & radio:** Wi-Fi SSID, access point BSSID (`ap`), band & channel, RSSI, router MAC (`gateway_mac`), cellular carrier/RAT, VPN, public IP & ISP
- **Location & weather:** GPS or IP location + Open-Meteo live conditions, cached by router fingerprint (roaming across mesh extenders keeps the cache warm)
- **Vitals & nearby:** battery %, power source, time remaining, RAM, CPU load, free disk, local agent services, and connected ADB devices
- **Place memory:** quiet SQLite visit/transition ledger (`~/.local/share/whereabout/memory.db`) plus intentional, scoped notes (`ap`, `place`, `area`)

```text
$ whereabout
whereabout/v1 2026-11-19 17:40 EST (UTC-05:00) · Thu evening · up 12d 4h · 76ms
device      laptop · Studio-MBP / MacBookPro18,2 (macOS, arm64) · alex@Studio-MBP
runtime     local · tty · cwd ~/projects/app (git: main*2)
net         online · wifi (en0) · local 192.168.1.42 · gw 192.168.1.1 (78:67:0e:aa:bb:cc) · pub 203.0.113.10 (Example Fiber)
location    Home (Brooklyn, New York, US) · 40.71, -73.96 (via ip, cached 4m)
weather     57°F / 14°C (feels 51°F) · Clear (night) · wind 4mph · hum 34% (cached 4m)
vitals      bat 88% (battery) 4h10m left · ram 18.2/32.0GB (57%) · load 1.84 · 10c · disk 412.0GB free
nearby      services: ollama:11434 · adb: Pixel 9 (ABCD123456)
memory      Home / Upstairs Desk · visit #14 (1h 20m) · prev: Coffee Shop
            - #2 (ap) Thunderbolt dock provides gigabit Ethernet on en5
            - #1 [device] Office printer is at 192.168.1.50
```

## Install

Requires Python 3.11+ (uses only the Python standard library).

```bash
pip install git+https://github.com/firstprinciplecode/whereabout.git
```

Or build a single 35 KB standalone executable (`dist/whereabout`) you can drop anywhere on `$PATH` (macOS, Linux, or Android/Termux):

```bash
python3 scripts/build-whereabout.py
cp dist/whereabout ~/.local/bin/whereabout

# For Android / Termux (which lacks /usr/bin/env):
python3 scripts/build-whereabout.py \
  --interpreter /data/data/com.termux/files/usr/bin/python3 \
  --out dist/whereabout-phone
```

## Usage

```bash
whereabout                        # ~8-line report (parallel probes + warm cache)
whereabout -1                     # Single-line ~35-token summary for prompt headers
whereabout --json                 # Strict whereabout/v1 JSON
whereabout --fast                 # Local probes + warm cache only, zero network (<100ms)
whereabout --fresh                # Force refresh geo & weather
whereabout --only device,net,mem  # Run only selected sections
whereabout --precise              # Full-precision coordinates (default rounds to ~1km)
whereabout --spec                 # Print the whereabout/v1 protocol specification
```

## Place Memory

`whereabout` never logs command transcripts. Instead, it keeps a tiny local SQLite database (`~/.local/share/whereabout/memory.db`, `<2ms` per run) with **automatic visit/transition counts** and **explicit annotations** scoped to three levels:

- **Default (`place`)**: the router/building (`gateway_mac`, DHCP `router_id`, or `ssid`)
- **`--ap`**: this exact Wi-Fi access point / extender (`bssid`)
- **`--area`**: ~1 km neighborhood grid (`lat, lon`), preserved across Wi-Fi and cellular

```bash
# Label places without editing config files
whereabout label "Home"
whereabout label --ap "Upstairs Desk"
whereabout label --area "Williamsburg"

# Attach durable notes (--kind: note | device | rule | tip | todo)
whereabout note --kind device "Office printer is at 192.168.1.50"
whereabout note --ap "Thunderbolt dock provides gigabit Ethernet on en5"
whereabout note --area --kind tip "Hardware store on Grand St closes at 6pm"

# Inspect or prune
whereabout notes                  # Notes matching right here (ap + place + area)
whereabout notes --all            # All notes across every place
whereabout forget 2               # Delete note #2
whereabout places                 # Known places, visit counts, and recent arrivals
```

## Drop-in Providers (`whereabout/v1`)

Any executable in `~/.config/whereabout/providers.d/` (or `$WHEREABOUT_PROVIDERS_DIR`) runs in parallel (`400ms` budget) and prints a single JSON object merged into `extra.<name>`. Optional `_place` and `_body` keys enrich `location.place` and `device.body`.

## License

[MIT](LICENSE)
