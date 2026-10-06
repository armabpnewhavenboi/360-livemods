"""Game definitions.

Each game is one JSON file in this folder (see docs/ADDING_GAMES.md). Extra definitions can also
be dropped into the user's games folder (shown in the app's About panel) without rebuilding.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Patch:
    address: int
    original: bytes
    patched: bytes


@dataclass
class Version:
    id: str
    name: str
    hint: str
    detect_address: int
    detect_values: list[bytes]
    fps_pointer: int | None = None     # global holding a pointer to the object with the frame counter
    fps_offset: int = 0                # offset of the once-per-frame counter inside that object


@dataclass
class Mod:
    id: str
    name: str
    description: str
    category: str
    default: bool
    status: str                      # "tested" | "experimental" | "unstable"
    credit: str
    warning: str
    patches: dict[str, list[Patch]]  # version id -> patches
    unavailable: dict[str, str]      # version id -> reason shown in the UI

    def available_for(self, version_id: str) -> bool:
        return version_id in self.patches and version_id not in self.unavailable


@dataclass
class Game:
    id: str
    name: str
    title_id: str
    description: str
    notes: list[str]
    versions: list[Version]
    mods: list[Mod]
    credits: list[str] = field(default_factory=list)
    source: str = ""
    banner: Path | None = None                 # header art; a user file overrides the bundled one
    banner_focus: tuple[float, float] = (0.5, 0.5)

    def version(self, version_id: str) -> Version:
        for v in self.versions:
            if v.id == version_id:
                return v
        raise KeyError(version_id)


def _hex(s: str) -> bytes:
    s = s.lower().removeprefix("0x").replace(" ", "")
    return bytes.fromhex(s)


def _addr(s: str | int) -> int:
    return s if isinstance(s, int) else int(s, 16)


def load_game(path: Path) -> Game:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != 1:
        raise ValueError(f"{path.name}: unsupported schema {raw.get('schema')!r}")
    versions = [
        Version(
            id=v["id"], name=v["name"], hint=v.get("hint", ""),
            detect_address=_addr(v["detect"]["address"]),
            detect_values=[_hex(x) for x in v["detect"]["values"]],
            fps_pointer=_addr(v["fps_counter"]["pointer"]) if v.get("fps_counter") else None,
            fps_offset=_addr(v["fps_counter"]["offset"]) if v.get("fps_counter") else 0,
        )
        for v in raw["versions"]
    ]
    vids = {v.id for v in versions}
    mods = []
    for m in raw["mods"]:
        patches = {}
        for vid, plist in m.get("patches", {}).items():
            if vid not in vids:
                raise ValueError(f"{path.name}: mod {m['id']} references unknown version {vid}")
            parsed = [Patch(_addr(a), _hex(o), _hex(n)) for a, o, n in plist]
            for p in parsed:
                if len(p.original) != len(p.patched):
                    raise ValueError(f"{path.name}: {m['id']} patch at {p.address:#x} size mismatch")
            patches[vid] = parsed
        mods.append(Mod(
            id=m["id"], name=m["name"], description=m.get("description", ""),
            category=m.get("category", "General"), default=bool(m.get("default", False)),
            status=m.get("status", "experimental"), credit=m.get("credit", ""),
            warning=m.get("warning", ""), patches=patches,
            unavailable=dict(m.get("unavailable", {})),
        ))
    b = raw.get("banner") or {}
    banner = None
    for cand in [user_dir() / f"{raw['id']}_banner.{ext}" for ext in ("png", "jpg", "jpeg")] + \
                ([path.parent / b["file"]] if b.get("file") else []):
        if cand.is_file():
            banner = cand
            break
    focus = tuple(b.get("focus", (0.5, 0.5)))
    return Game(
        id=raw["id"], name=raw["name"], title_id=raw["title_id"],
        description=raw.get("description", ""), notes=raw.get("notes", []),
        versions=versions, mods=mods, credits=raw.get("credits", []), source=str(path),
        banner=banner, banner_focus=(float(focus[0]), float(focus[1])),
    )


def builtin_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    p = base / "livemods" / "games"
    return p if p.exists() else Path(__file__).resolve().parent


def user_dir() -> Path:
    root = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    return Path(root) / "360LiveMods" / "games"


def load_all() -> tuple[list[Game], list[str]]:
    """Load built-in and user game definitions. Returns (games, error messages)."""
    games: dict[str, Game] = {}
    errors: list[str] = []
    for folder in (builtin_dir(), user_dir()):
        if not folder.exists():
            continue
        for f in sorted(folder.glob("*.json")):
            try:
                g = load_game(f)
                games[g.id] = g          # user files override built-ins with the same id
            except Exception as e:       # noqa: BLE001 - surface any bad file to the user
                errors.append(f"{f.name}: {e}")
    return sorted(games.values(), key=lambda g: g.name.lower()), errors
