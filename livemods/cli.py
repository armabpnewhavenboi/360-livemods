"""Command-line interface (for power users and scripting).

    python -m livemods --list
    python -m livemods fable2 --ip 192.168.1.50 --version tu --mods bloom,motion_blur,fps60
    python -m livemods fable2 --ip 192.168.1.50 --version base --all
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .engine import Engine
from .games import load_all


def main(argv: list[str] | None = None) -> int:
    games, errors = load_all()
    ap = argparse.ArgumentParser(prog="livemods", description="360 LiveMods - live game mods over XBDM")
    ap.add_argument("game", nargs="?", help="game id (see --list)")
    ap.add_argument("--list", action="store_true", help="list games, versions and mods")
    ap.add_argument("--ip", help="console IP address")
    ap.add_argument("--version", dest="game_version", help="game version id (e.g. base, tu)")
    ap.add_argument("--mods", default="", help="comma-separated mod ids")
    ap.add_argument("--all", action="store_true", help="all mods marked as default")
    ap.add_argument("--now", action="store_true", help="patch the running game instead of waiting for launch")
    ap.add_argument("--restore", action="store_true", help="put original values back (game must be running)")
    ap.add_argument("-V", action="version", version=f"360 LiveMods {__version__}")
    a = ap.parse_args(argv)
    for e in errors:
        print("warning:", e, file=sys.stderr)

    if a.list or not a.game:
        for g in games:
            print(f"{g.id}  -  {g.name} [{g.title_id}]")
            print("   versions: " + ", ".join(f"{v.id} ({v.name})" for v in g.versions))
            for m in g.mods:
                avail = ",".join(v.id for v in g.versions if m.available_for(v.id))
                print(f"   {m.id:14} {m.name:28} [{m.status}] versions: {avail}")
        return 0

    game = next((g for g in games if g.id == a.game), None)
    if not game:
        ap.error(f"unknown game '{a.game}'")
    if not a.ip or not a.game_version:
        ap.error("--ip and --version are required")
    ids = [m.id for m in game.mods if m.default] if a.all else [s for s in a.mods.split(",") if s]
    mods = [m for m in game.mods if m.id in ids]
    for m in mods:
        if not m.available_for(a.game_version):
            print(f"skipping {m.id}: {m.unavailable.get(a.game_version, 'not available')}")

    def on_event(kind: str, msg: str) -> None:
        if kind != "status":
            print({"ok": "[ OK ]", "warn": "[WARN]", "error": "[FAIL]"}.get(kind, "      "), msg, flush=True)

    eng = Engine(on_event)
    try:
        r = eng.run(a.ip, game, a.game_version, mods, restore=a.restore, wait=not (a.now or a.restore))
    except KeyboardInterrupt:
        eng.stop()
        return 130
    return 1 if (r.failed or r.wrong_version) else 0


if __name__ == "__main__":
    raise SystemExit(main())
