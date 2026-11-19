"""Local SQLite place memory: quiet visit/transition ledger + intentional annotations.

Design principles:
- Never log raw commands or noisy transcripts.
- Automatically track place visits, dwell time, and transitions (<2ms local SQLite).
- Store explicit, curated place labels and notes scoped to:
  * `ap`    — exact Wi-Fi access point (`ap:<bssid>`, e.g. one floor/extender)
  * `place` — building/network (`gw:<gateway_mac>`, `rtr:<router_id>`, `ssid:<ssid>`, etc.)
  * `area`  — ~1.1km neighborhood grid (`area:<lat2>,<lon2>`) across Wi-Fi & cellular
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

from .protocol import fmt_age, fmt_duration

REVISIT_GAP_SECONDS = 7200  # >2h away counts as a new visit on return
MAX_VISITS_KEPT = 500
VALID_NOTE_KINDS = ("note", "device", "rule", "tip", "todo")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS places (
    place_key   TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    hint        TEXT NOT NULL DEFAULT '',
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    visit_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS visits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    place_key      TEXT NOT NULL,
    ap_key         TEXT NOT NULL DEFAULT '',
    arrived_at     REAL NOT NULL,
    last_seen_at   REAL NOT NULL,
    prev_place_key TEXT NOT NULL DEFAULT '',
    device_id      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_visits_arrived ON visits(arrived_at DESC);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key  TEXT NOT NULL,
    scope      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note',
    text       TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_scope_key ON notes(scope_key);
"""


def data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "whereabout")


def memory_db_path() -> str:
    return os.environ.get("WHEREABOUT_MEMORY_DB") or os.path.join(data_dir(), "memory.db")


def _connect(readonly: bool = False) -> sqlite3.Connection | None:
    path = memory_db_path()
    try:
        if readonly:
            if not os.path.exists(path):
                return None
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = sqlite3.connect(path, timeout=0.8)
            conn.executescript(SCHEMA_SQL)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def derive_keys(network: dict[str, Any], location: dict[str, Any]) -> dict[str, str]:
    """Derive hierarchical scope keys (`ap`, `place`, `area`) and a human hint."""
    keys: dict[str, str] = {}

    bssid = str(network.get("bssid") or "").strip().lower()
    if bssid:
        keys["ap"] = f"ap:{bssid}"

    lat = location.get("lat")
    lon = location.get("lon")
    has_coords = isinstance(lat, (int, float)) and isinstance(lon, (int, float))
    if has_coords:
        keys["area"] = f"area:{round(float(lat), 2):.2f},{round(float(lon), 2):.2f}"

    gw_mac = str(network.get("gateway_mac") or "").strip().lower()
    router_id = str(network.get("router_id") or "").strip()
    ssid = str(network.get("ssid") or "").strip()
    gw_ip = str(network.get("gateway") or "").strip()
    fp = str(network.get("fingerprint") or "").strip()

    if gw_mac:
        keys["place"] = f"gw:{gw_mac}"
    elif router_id:
        keys["place"] = f"rtr:{router_id}"
    elif ssid:
        keys["place"] = f"ssid:{ssid}|{gw_ip}" if gw_ip else f"ssid:{ssid}"
    elif "area" in keys:
        keys["place"] = keys["area"]
    elif fp and network.get("online"):
        keys["place"] = f"fp:{fp}"

    # Human-readable fallback hint for `whereabout places` when unlabeled
    hint_bits: list[str] = []
    if location.get("place"):
        hint_bits.append(str(location["place"]))
    if ssid:
        hint_bits.append(f'"{ssid}"')
    elif router_id:
        hint_bits.append(router_id.split()[0])
    city = str(location.get("city") or "").strip()
    region = str(location.get("region") or "").strip()
    if city:
        hint_bits.append(f"{city}, {region}" if region and region != city else city)
    elif gw_ip:
        hint_bits.append(f"gw {gw_ip}")
    keys["hint"] = " · ".join(dict.fromkeys(b for b in hint_bits if b))
    return keys


def observe_and_recall(
    network: dict[str, Any],
    location: dict[str, Any],
    device: dict[str, Any],
    *,
    track: bool = True,
    note_limit: int = 5,
) -> dict[str, Any]:
    """Record current presence (if `track=True`) and recall labels, visit stats, and notes."""
    keys = derive_keys(network, location)
    place_key = keys.get("place", "")
    ap_key = keys.get("ap", "")
    area_key = keys.get("area", "")
    hint = keys.get("hint", "")
    if not place_key and not ap_key and not area_key:
        return {}

    now = time.time()
    device_id = str(device.get("id") or device.get("host") or "")

    conn = _connect(readonly=not track)
    if conn is None and track:
        # Fallback to read-only if filesystem is sandboxed/read-only
        conn = _connect(readonly=True)
        track = False
    if conn is None:
        return {"place_key": place_key} if place_key else {}

    try:
        visit_count = 1
        dwell_s = 0
        first_seen = now
        is_new_place = False
        prev_key = ""
        prev_label = ""

        if track and place_key:
            row = conn.execute(
                "SELECT label, hint, first_seen, last_seen, visit_count FROM places WHERE place_key = ?",
                (place_key,),
            ).fetchone()

            last_visit = conn.execute(
                "SELECT id, place_key, ap_key, arrived_at, last_seen_at, prev_place_key "
                "FROM visits ORDER BY id DESC LIMIT 1"
            ).fetchone()

            same_ongoing_visit = (
                last_visit is not None
                and last_visit["place_key"] == place_key
                and (now - float(last_visit["last_seen_at"])) <= REVISIT_GAP_SECONDS
            )

            if row is None:
                is_new_place = True
                visit_count = 1
                first_seen = now
                conn.execute(
                    "INSERT INTO places (place_key, scope, label, hint, first_seen, last_seen, visit_count) "
                    "VALUES (?, 'place', '', ?, ?, ?, 1)",
                    (place_key, hint, now, now),
                )
            else:
                first_seen = float(row["first_seen"])
                visit_count = int(row["visit_count"]) + (0 if same_ongoing_visit else 1)
                new_hint = hint or str(row["hint"] or "")
                conn.execute(
                    "UPDATE places SET last_seen = ?, visit_count = ?, hint = ? WHERE place_key = ?",
                    (now, visit_count, new_hint, place_key),
                )

            if same_ongoing_visit and last_visit is not None:
                dwell_s = int(max(0.0, now - float(last_visit["arrived_at"])))
                prev_key = str(last_visit["prev_place_key"] or "")
                conn.execute(
                    "UPDATE visits SET last_seen_at = ?, ap_key = ? WHERE id = ?",
                    (now, ap_key or str(last_visit["ap_key"] or ""), int(last_visit["id"])),
                )
            else:
                prev_key = str(last_visit["place_key"]) if last_visit and last_visit["place_key"] != place_key else ""
                dwell_s = 0
                conn.execute(
                    "INSERT INTO visits (place_key, ap_key, arrived_at, last_seen_at, prev_place_key, device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (place_key, ap_key, now, now, prev_key, device_id),
                )
                conn.execute(
                    "DELETE FROM visits WHERE id NOT IN ("
                    "  SELECT id FROM visits ORDER BY id DESC LIMIT ?"
                    ")",
                    (MAX_VISITS_KEPT,),
                )

            if ap_key:
                ap_row = conn.execute("SELECT place_key FROM places WHERE place_key = ?", (ap_key,)).fetchone()
                if ap_row is None:
                    conn.execute(
                        "INSERT INTO places (place_key, scope, label, hint, first_seen, last_seen, visit_count) "
                        "VALUES (?, 'ap', '', ?, ?, ?, 1)",
                        (ap_key, hint, now, now),
                    )
                else:
                    conn.execute("UPDATE places SET last_seen = ? WHERE place_key = ?", (now, ap_key))

            conn.commit()
        elif place_key:
            row = conn.execute(
                "SELECT first_seen, visit_count FROM places WHERE place_key = ?",
                (place_key,),
            ).fetchone()
            if row:
                first_seen = float(row["first_seen"])
                visit_count = int(row["visit_count"])
            last_visit = conn.execute(
                "SELECT place_key, arrived_at, prev_place_key FROM visits ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last_visit and last_visit["place_key"] == place_key:
                dwell_s = int(max(0.0, now - float(last_visit["arrived_at"])))
                prev_key = str(last_visit["prev_place_key"] or "")

        # Resolve saved labels across scopes (ap, place, area) + previous place
        lookup_keys = [k for k in (ap_key, place_key, area_key, prev_key) if k]
        labels_by_key: dict[str, tuple[str, str]] = {}
        if lookup_keys:
            q_marks = ",".join("?" for _ in lookup_keys)
            for r in conn.execute(
                f"SELECT place_key, label, hint FROM places WHERE place_key IN ({q_marks})",
                lookup_keys,
            ):
                labels_by_key[str(r["place_key"])] = (str(r["label"] or ""), str(r["hint"] or ""))

        place_label = labels_by_key.get(place_key, ("", ""))[0]
        ap_label = labels_by_key.get(ap_key, ("", ""))[0]
        area_label = labels_by_key.get(area_key, ("", ""))[0]
        if prev_key and prev_key in labels_by_key:
            p_lbl, p_hint = labels_by_key[prev_key]
            prev_label = p_lbl or p_hint or prev_key

        # Query matching notes ordered by specificity: ap -> place -> area, newest first
        scope_keys = list(dict.fromkeys(k for k in (ap_key, place_key, area_key) if k))
        notes_out: list[dict[str, Any]] = []
        total_notes = 0
        if scope_keys:
            q_marks = ",".join("?" for _ in scope_keys)
            rows = conn.execute(
                f"SELECT id, scope_key, scope, kind, text, updated_at FROM notes "
                f"WHERE scope_key IN ({q_marks})",
                scope_keys,
            ).fetchall()
            rank = {ap_key: 0, place_key: 1, area_key: 2}
            sorted_rows = sorted(rows, key=lambda r: (rank.get(str(r["scope_key"]), 9), -float(r["updated_at"])))
            total_notes = len(sorted_rows)
            for r in sorted_rows[:note_limit]:
                notes_out.append(
                    {
                        "id": int(r["id"]),
                        "scope": str(r["scope"]),
                        "kind": str(r["kind"]),
                        "text": str(r["text"]),
                        "age": fmt_age(max(0.0, now - float(r["updated_at"]))),
                    }
                )

        result: dict[str, Any] = {
            "place_key": place_key,
            "status": "new" if is_new_place else ("familiar" if visit_count >= 3 else "seen"),
            "visits": visit_count,
            "dwell_s": dwell_s,
            "dwell": fmt_duration(dwell_s) if dwell_s >= 60 else "just arrived",
            "first_seen_age": fmt_age(max(0.0, now - first_seen)),
        }
        if ap_key:
            result["ap_key"] = ap_key
        if area_key:
            result["area_key"] = area_key
        if place_label or area_label:
            result["label"] = place_label or area_label
        if ap_label:
            result["subplace"] = ap_label
        if prev_label:
            result["previous"] = prev_label
        if notes_out:
            result["notes"] = notes_out
            result["total_notes"] = total_notes
        return result
    finally:
        conn.close()


def set_label(
    network: dict[str, Any],
    location: dict[str, Any],
    label: str,
    *,
    scope: str = "place",
) -> dict[str, Any]:
    """Attach or clear a human name for the current `place`, `ap`, or `area`."""
    keys = derive_keys(network, location)
    target_key = keys.get(scope) or ""
    if not target_key:
        raise ValueError(f"Cannot resolve a `{scope}` identifier right now (try default place scope).")

    conn = _connect(readonly=False)
    if conn is None:
        raise RuntimeError(f"Cannot open memory database at {memory_db_path()}")

    now = time.time()
    clean_label = " ".join(label.strip().split())
    hint = keys.get("hint", "")
    try:
        row = conn.execute("SELECT place_key FROM places WHERE place_key = ?", (target_key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO places (place_key, scope, label, hint, first_seen, last_seen, visit_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (target_key, scope, clean_label, hint, now, now),
            )
        else:
            conn.execute(
                "UPDATE places SET label = ?, scope = ?, last_seen = ? WHERE place_key = ?",
                (clean_label, scope, now, target_key),
            )
        conn.commit()
        return {"ok": True, "scope": scope, "key": target_key, "label": clean_label}
    finally:
        conn.close()


def add_note(
    network: dict[str, Any],
    location: dict[str, Any],
    text: str,
    *,
    scope: str = "place",
    kind: str = "note",
) -> dict[str, Any]:
    """Save a curated note tied to the current `place`, `ap`, or `area`."""
    clean_text = " ".join(text.strip().split())
    if not clean_text:
        raise ValueError("Note text cannot be empty.")
    norm_kind = kind.strip().lower() if kind else "note"
    if norm_kind not in VALID_NOTE_KINDS:
        norm_kind = "note"

    keys = derive_keys(network, location)
    target_key = keys.get(scope) or ""
    if not target_key:
        raise ValueError(f"Cannot resolve a `{scope}` identifier right now.")

    conn = _connect(readonly=False)
    if conn is None:
        raise RuntimeError(f"Cannot open memory database at {memory_db_path()}")

    now = time.time()
    hint = keys.get("hint", "")
    try:
        # Ensure place row exists so `whereabout places` / `notes --all` has its hint/label
        p_row = conn.execute("SELECT place_key FROM places WHERE place_key = ?", (target_key,)).fetchone()
        if p_row is None:
            conn.execute(
                "INSERT INTO places (place_key, scope, label, hint, first_seen, last_seen, visit_count) "
                "VALUES (?, ?, '', ?, ?, ?, 1)",
                (target_key, scope, hint, now, now),
            )

        existing = conn.execute(
            "SELECT id FROM notes WHERE scope_key = ? AND lower(text) = lower(?)",
            (target_key, clean_text),
        ).fetchone()
        if existing:
            note_id = int(existing["id"])
            conn.execute(
                "UPDATE notes SET kind = ?, text = ?, updated_at = ? WHERE id = ?",
                (norm_kind, clean_text, now, note_id),
            )
            action = "updated"
        else:
            cur = conn.execute(
                "INSERT INTO notes (scope_key, scope, kind, text, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (target_key, scope, norm_kind, clean_text, now, now),
            )
            note_id = int(cur.lastrowid or 0)
            action = "saved"
        conn.commit()
        return {
            "ok": True,
            "action": action,
            "id": note_id,
            "scope": scope,
            "key": target_key,
            "kind": norm_kind,
            "text": clean_text,
        }
    finally:
        conn.close()


def delete_note(note_id: int) -> dict[str, Any]:
    conn = _connect(readonly=False)
    if conn is None:
        raise RuntimeError(f"Cannot open memory database at {memory_db_path()}")
    try:
        row = conn.execute("SELECT id, scope_key, text FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            return {"ok": False, "id": note_id, "error": f"Note #{note_id} not found."}
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
        return {"ok": True, "id": note_id, "scope_key": str(row["scope_key"]), "text": str(row["text"])}
    finally:
        conn.close()


def list_notes(
    network: dict[str, Any] | None = None,
    location: dict[str, Any] | None = None,
    *,
    all_places: bool = False,
) -> list[dict[str, Any]]:
    conn = _connect(readonly=True)
    if conn is None:
        return []
    now = time.time()
    try:
        if all_places or network is None or location is None:
            rows = conn.execute(
                "SELECT n.id, n.scope_key, n.scope, n.kind, n.text, n.updated_at, "
                "COALESCE(p.label, '') AS label, COALESCE(p.hint, '') AS hint "
                "FROM notes n LEFT JOIN places p ON p.place_key = n.scope_key "
                "ORDER BY n.updated_at DESC LIMIT 100"
            ).fetchall()
        else:
            keys = derive_keys(network, location)
            scope_keys = [k for k in (keys.get("ap"), keys.get("place"), keys.get("area")) if k]
            if not scope_keys:
                return []
            q_marks = ",".join("?" for _ in scope_keys)
            rows = conn.execute(
                f"SELECT n.id, n.scope_key, n.scope, n.kind, n.text, n.updated_at, "
                f"COALESCE(p.label, '') AS label, COALESCE(p.hint, '') AS hint "
                f"FROM notes n LEFT JOIN places p ON p.place_key = n.scope_key "
                f"WHERE n.scope_key IN ({q_marks}) ORDER BY n.updated_at DESC",
                scope_keys,
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "scope": str(r["scope"]),
                "scope_key": str(r["scope_key"]),
                "place": str(r["label"] or r["hint"] or r["scope_key"]),
                "kind": str(r["kind"]),
                "text": str(r["text"]),
                "age": fmt_age(max(0.0, now - float(r["updated_at"]))),
            }
            for r in rows
        ]
    finally:
        conn.close()


def list_places_and_visits(limit: int = 15) -> dict[str, Any]:
    conn = _connect(readonly=True)
    if conn is None:
        return {"places": [], "recent_visits": []}
    now = time.time()
    try:
        place_rows = conn.execute(
            "SELECT p.place_key, p.scope, p.label, p.hint, p.first_seen, p.last_seen, p.visit_count, "
            "(SELECT COUNT(*) FROM notes n WHERE n.scope_key = p.place_key) AS note_count "
            "FROM places p ORDER BY p.last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        visit_rows = conn.execute(
            "SELECT v.id, v.place_key, v.ap_key, v.arrived_at, v.last_seen_at, v.prev_place_key, v.device_id, "
            "COALESCE(p.label, '') AS label, COALESCE(p.hint, '') AS hint "
            "FROM visits v LEFT JOIN places p ON p.place_key = v.place_key "
            "ORDER BY v.id DESC LIMIT ?",
            (limit,),
        ).fetchall()

        places = [
            {
                "key": str(r["place_key"]),
                "scope": str(r["scope"]),
                "label": str(r["label"]),
                "hint": str(r["hint"]),
                "visits": int(r["visit_count"]),
                "notes": int(r["note_count"]),
                "last_seen": fmt_age(max(0.0, now - float(r["last_seen"]))),
            }
            for r in place_rows
        ]
        visits = []
        for r in visit_rows:
            dur_s = int(max(0.0, float(r["last_seen_at"]) - float(r["arrived_at"])))
            arrived_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r["arrived_at"])))
            visits.append(
                {
                    "arrived": arrived_str,
                    "place": str(r["label"] or r["hint"] or r["place_key"]),
                    "key": str(r["place_key"]),
                    "dwell": fmt_duration(dur_s) if dur_s >= 60 else "<1m",
                    "device": str(r["device_id"]),
                }
            )
        return {"places": places, "recent_visits": visits}
    finally:
        conn.close()
