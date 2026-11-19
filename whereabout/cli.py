"""CLI entrypoint for `whereabout` (snapshot + place memory subcommands)."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from .cache import init_config
from .engine import snapshot
from .formatters import format_compact, format_json, format_oneline
from .memory import (
    VALID_NOTE_KINDS,
    add_note,
    delete_note,
    list_notes,
    list_places_and_visits,
    set_label,
)
from .protocol import SCHEMA_VERSION, SPEC_TEXT, VERSION

SUBCOMMANDS = ("note", "notes", "label", "forget", "places", "history")


def build_snapshot_parser() -> argparse.ArgumentParser:
    epilog = (
        "Place Memory Subcommands:\n"
        "  whereabout label \"Home\" [--ap | --area]      Name the current router/building, AP, or area\n"
        "  whereabout note \"...\" [--ap | --area]        Attach a curated note to right here\n"
        "  whereabout notes [--all] [--json]            List notes for here (or across all places)\n"
        "  whereabout forget <id>                       Delete a saved note by #id\n"
        "  whereabout places [--json]                   Show known places and recent visit transitions"
    )
    p = argparse.ArgumentParser(
        prog="whereabout",
        description="Fast situational-awareness report (whereabout/v1) and place memory for AI agents.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("-j", "--json", action="store_true", help="Output strict whereabout/v1 JSON.")
    fmt.add_argument("-1", "--oneline", action="store_true", help="Single-line summary (~35-45 tokens).")

    p.add_argument(
        "-f",
        "--fast",
        "--offline",
        dest="fast",
        action="store_true",
        help="Local probes + warm cache only; zero outbound network (<100ms).",
    )
    p.add_argument("--fresh", action="store_true", help="Bypass cache and refresh geo/weather.")
    p.add_argument(
        "--no-track",
        action="store_true",
        help="Read place memory without incrementing visit or dwell timestamps.",
    )
    p.add_argument(
        "--only",
        metavar="SECTIONS",
        help="Comma-separated subset: device,runtime,time,net,location,weather,vitals,nearby,memory,extra",
    )
    p.add_argument(
        "--precise",
        action="store_true",
        help="Include full-precision coordinates instead of ~1km privacy rounding.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=1.8,
        help="Hard overall deadline in seconds (default: 1.8).",
    )
    p.add_argument(
        "--init-config",
        action="store_true",
        help="Create a starter ~/.config/whereabout/config.json for static place rules.",
    )
    p.add_argument("--spec", action="store_true", help="Print the whereabout/v1 protocol specification.")
    p.add_argument("--version", action="version", version=f"whereabout {VERSION} ({SCHEMA_VERSION})")
    return p


def _quick_context(need_area: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Grab current network + location fast for memory subcommands."""
    data = snapshot(fast=not need_area, track=False, only=("network", "location"), timeout=1.4)
    net = data.get("network") or {}
    loc = data.get("location") or {}
    if need_area and "lat" not in loc:
        data = snapshot(fast=False, track=False, only=("network", "location"), timeout=1.5)
        net = data.get("network") or {}
        loc = data.get("location") or {}
    return net, loc


def _scope_from_args(ap: bool, area: bool) -> str:
    if ap:
        return "ap"
    if area:
        return "area"
    return "place"


def _handle_subcommand(cmd: str, rest: Sequence[str]) -> int:
    if cmd == "label":
        p = argparse.ArgumentParser(prog="whereabout label", description="Name the current place, AP, or area.")
        scope_grp = p.add_mutually_exclusive_group()
        scope_grp.add_argument("--ap", action="store_true", help="Bind label to this exact Wi-Fi access point (BSSID).")
        scope_grp.add_argument("--area", action="store_true", help="Bind label to this ~1km geographic area.")
        p.add_argument("--clear", action="store_true", help="Clear the saved label for this scope.")
        p.add_argument("-j", "--json", action="store_true", help="Output JSON.")
        p.add_argument("name", nargs="*", help="Label text, e.g. \"Home\" or \"Upstairs Studio\".")
        args = p.parse_args(rest)

        label_text = "" if args.clear else " ".join(args.name).strip()
        if not args.clear and not label_text:
            p.error("Provide a label name (e.g. `whereabout label \"Home\"`) or `--clear`.")

        scope = _scope_from_args(args.ap, args.area)
        net, loc = _quick_context(need_area=(scope == "area"))
        try:
            res = set_label(net, loc, label_text, scope=scope)
        except Exception as e:
            sys.stderr.write(f"whereabout label: {e}\n")
            return 1
        if args.json:
            print(format_json(res))
        elif label_text:
            print(f"Labeled {res['scope']} ({res['key']}) -> {res['label']}")
        else:
            print(f"Cleared label on {res['scope']} ({res['key']})")
        return 0

    if cmd == "note":
        p = argparse.ArgumentParser(prog="whereabout note", description="Attach an intentional note to right here.")
        scope_grp = p.add_mutually_exclusive_group()
        scope_grp.add_argument("--ap", action="store_true", help="Scope note to this exact Wi-Fi access point (BSSID).")
        scope_grp.add_argument("--area", action="store_true", help="Scope note to this ~1km neighborhood area.")
        p.add_argument(
            "-k",
            "--kind",
            choices=VALID_NOTE_KINDS,
            default="note",
            help="Category tag (default: note).",
        )
        p.add_argument("-j", "--json", action="store_true", help="Output JSON.")
        p.add_argument("text", nargs="+", help="Note text to remember for this place.")
        args = p.parse_args(rest)

        scope = _scope_from_args(args.ap, args.area)
        net, loc = _quick_context(need_area=(scope == "area"))
        try:
            res = add_note(net, loc, " ".join(args.text), scope=scope, kind=args.kind)
        except Exception as e:
            sys.stderr.write(f"whereabout note: {e}\n")
            return 1
        if args.json:
            print(format_json(res))
        else:
            tag = f"[{res['kind']}] " if res["kind"] != "note" else ""
            print(f"#{res['id']} ({res['action']} on {res['scope']} {res['key']}): {tag}{res['text']}")
        return 0

    if cmd == "notes":
        p = argparse.ArgumentParser(prog="whereabout notes", description="List saved place notes.")
        p.add_argument("-a", "--all", action="store_true", help="List notes across all known places.")
        p.add_argument("-j", "--json", action="store_true", help="Output JSON.")
        args = p.parse_args(rest)

        if args.all:
            items = list_notes(all_places=True)
        else:
            net, loc = _quick_context(need_area=False)
            items = list_notes(net, loc, all_places=False)

        if args.json:
            print(format_json({"notes": items}))
            return 0
        if not items:
            print("No notes saved for here." if not args.all else "No place notes saved yet.")
            return 0
        for it in items:
            tag = f"[{it['kind']}] " if it["kind"] != "note" else ""
            where = f"{it['place']} ({it['scope']}) · " if args.all else f"({it['scope']}) "
            print(f"#{it['id']:<3} {where}{tag}{it['text']}  [{it['age']}]")
        return 0

    if cmd == "forget":
        p = argparse.ArgumentParser(prog="whereabout forget", description="Delete a saved place note by #id.")
        p.add_argument("note_id", help="Note ID (e.g. 3 or #3).")
        p.add_argument("-j", "--json", action="store_true", help="Output JSON.")
        args = p.parse_args(rest)
        raw = str(args.note_id).lstrip("#")
        if not raw.isdigit():
            p.error("Note ID must be a number (e.g. `whereabout forget 2`).")
        res = delete_note(int(raw))
        if args.json:
            print(format_json(res))
            return 0 if res.get("ok") else 1
        if not res.get("ok"):
            sys.stderr.write(f"whereabout forget: {res.get('error')}\n")
            return 1
        print(f"Forgot #{res['id']}: {res['text']}")
        return 0

    if cmd in ("places", "history"):
        p = argparse.ArgumentParser(prog=f"whereabout {cmd}", description="Show known places and visit transitions.")
        p.add_argument("-n", "--limit", type=int, default=15, help="Max entries to show (default: 15).")
        p.add_argument("-j", "--json", action="store_true", help="Output JSON.")
        args = p.parse_args(rest)

        data = list_places_and_visits(limit=max(1, args.limit))
        if args.json:
            print(format_json(data))
            return 0
        places = data.get("places") or []
        visits = data.get("recent_visits") or []
        if not places and not visits:
            print("No places recorded yet. Run `whereabout` to start your place ledger.")
            return 0
        if places:
            print("Known places:")
            for pl in places:
                title = pl["label"] or pl["hint"] or pl["key"]
                extra = f" ({pl['key']})" if pl["label"] else ""
                n_str = f" · {pl['notes']} notes" if pl["notes"] else ""
                print(f"  {title}{extra} — {pl['visits']} visit(s){n_str} · last seen {pl['last_seen']}")
        if visits:
            print("\nRecent arrivals:")
            for v in visits[:10]:
                dev = f" [{v['device']}]" if v.get("device") else ""
                print(f"  {v['arrived']} · {v['place']} (dwell {v['dwell']}){dev}")
        return 0

    return 1


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in SUBCOMMANDS:
        return _handle_subcommand(raw_argv[0], raw_argv[1:])

    parser = build_snapshot_parser()
    args = parser.parse_args(raw_argv)

    if args.spec:
        print(SPEC_TEXT.strip())
        return 0

    if args.init_config:
        path, created = init_config()
        if created:
            print(f"Created starter config: {path}")
        else:
            print(f"Config already exists: {path}")
        return 0

    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    data = snapshot(
        fast=args.fast,
        fresh=args.fresh,
        precise=args.precise,
        track=not args.no_track,
        only=only,
        timeout=args.timeout,
    )

    if args.json:
        print(format_json(data))
    elif args.oneline:
        print(format_oneline(data))
    else:
        print(format_compact(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
