"""The community patch library.

The Xenia project keeps a library of game patches (github.com/xenia-canary/game-patches): one
`.patch.toml` file per game build, each naming the build by a hash of its code. 360 LiveMods
downloads that library to the user's app-data folder (it is not bundled), reads it the same way
Xenia does, and offers its patches for real consoles. The engine only applies a file's patches to a
console after confirming that the running game is that exact build (see identify.py).
"""
from __future__ import annotations

import io
import json
import re
import shutil
import struct
import time
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import app_dir
from .games import Game, Mod, Patch, Version

SOURCE_REPO = "xenia-canary/game-patches"
PAGE_URL = f"https://github.com/{SOURCE_REPO}"
ZIP_URL = f"https://codeload.github.com/{SOURCE_REPO}/zip/refs/heads/main"
MAX_ZIP_BYTES = 60 * 1024 * 1024
REFRESH_DAYS = 7
AUTO = "*"            # the "version" a library game runs as: worked out on the console

# Patches made to get around problems in the Xenia emulator. A real console doesn't need them, and
# they change code that runs fine on hardware, so they are hidden behind a warning.
_EMULATOR_WORDS = (
    "xenia", "emulator", "shading fix", "occlusion quer", "texture noise", "nvidia", "amd gpu",
    "corrupted graphics", "pixelated graphics", "flickering", "horizontal line fix", "audio heap",
    "audio loop", "looping audio", "hang on bink", "black screen", "blackscreen", "crash",
    "stutter fix", "texture streaming fix", "ghost glow", "upscaling", "resolution scaling",
    "xstringverify", "loading check", "launch argument", "-dvd", "debug log",
    "debug_print", "texture morphing", "readback", "memexport",
)
# descriptions often mention Xenia in passing ("works best with vsync off in Xenia"), so only
# phrases that say the patch exists *because of* the emulator count there
_EMULATOR_DESC = re.compile(r"(xenia|emulator)[^.]{0,40}\b(bug|issue|problem|glitch|broken)"
                            r"|\b(workaround|fix|fixes|fixed)\b[^.]{0,30}\b(xenia|emulator)", re.I)
_CATEGORIES = (
    ("Performance", ("fps", "frame rate", "framerate", "resolution", "msaa", "anti-alias", "720p",
                     "1080p", "tick rate", "tickrate", "performance", "vsync", "v-sync", "lod",
                     "draw distance", "x720", "x1080", "60hz", "uncapped", "unlock frame")),
    ("Visual", ("bloom", "blur", "depth of field", "dof", "lens", "flare", "shadow", "ambient occlusion",
                "post-process", "post process", "hdr", "fxaa", "anisotropic", "fog", "vignette", "grain",
                "aspect", "widescreen", "ultrawide", "fov", "field of view", "hud", "chromatic", "sharpen",
                "lighting", "letterbox", "glow", "color", "colour", "grass", "film", "tonemap", "sun")),
    ("Gameplay", ("infinite", "unlimited", "unlock", "god mode", "money", "health", "ammo", "stamina",
                  "cheat", "invincib", "one hit", "max ", "difficulty", "speed")),
    ("Extras", ("skip", "intro", "logo", "show ", "debug", "console", "menu", "frametime", "graph",
                "region", "achievement", "language")),
)


def library_dir() -> Path:
    return app_dir() / "library"


@dataclass
class Write:
    address: int
    data: bytes


@dataclass
class LibPatch:
    name: str
    desc: str
    author: str
    writes: list[Write]
    emulator_only: bool = False


@dataclass
class LibBuild:
    id: str                    # first hash, as 16 hex digits
    hashes: list[int]
    label: str
    file: str
    patches: list[LibPatch]


@dataclass
class LibTitle:
    title_id: str              # 8 hex digits, upper case
    name: str
    builds: list[LibBuild] = field(default_factory=list)

    @property
    def game_id(self) -> str:
        return f"lib-{self.title_id}"

    @property
    def patch_count(self) -> int:
        return len({p.name for b in self.builds for p in b.patches})


@dataclass
class Library:
    titles: list[LibTitle]
    errors: list[str]
    fetched_at: float = 0.0

    def __post_init__(self):
        self.by_id = {t.title_id: t for t in self.titles}
        self.by_hash: dict[int, list[tuple[LibTitle, LibBuild]]] = {}
        for t in self.titles:
            for b in t.builds:
                for h in b.hashes:
                    self.by_hash.setdefault(h, []).append((t, b))

    def __len__(self) -> int:
        return len(self.titles)

    def search(self, text: str) -> list[LibTitle]:
        q = _fold(text)
        if not q:
            return list(self.titles)
        words = q.split()
        starts, contains = [], []
        for t in self.titles:
            hay = _fold(t.name)
            if all(w in hay or w in t.title_id.lower() for w in words):
                (starts if hay.startswith(q) or t.title_id.lower() == q else contains).append(t)
        return starts + contains


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower().replace("&", " and ")).strip()


# ------------------------------------------------------------------------------ parsing
def _value_bytes(kind: str, value) -> bytes:
    """Encode one patch value exactly as Xenia's patch_db.cc does."""
    if kind == "be8":
        return bytes([int(value) & 0xFF])
    if kind == "be16":
        return (int(value) & 0xFFFF).to_bytes(2, "big")
    if kind == "be32":
        return (int(value) & 0xFFFFFFFF).to_bytes(4, "big")
    if kind == "be64":
        return (int(value) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")
    if kind == "f32":
        return struct.pack(">f", float(value))
    if kind == "f64":
        return struct.pack(">d", float(value))
    if kind == "string":
        return str(value).encode("utf-8")
    if kind == "u16string":
        return str(value).encode("utf-16-le")      # Xenia copies the host-order string as-is
    if kind == "array":
        s = str(value).strip()
        s = s[2:] if s.lower().startswith("0x") else s
        return bytes.fromhex(s.replace(" ", ""))
    raise ValueError(f"unknown patch type {kind}")


_KINDS = ("be8", "be16", "be32", "be64", "f32", "f64", "string", "u16string", "array")


def _label_from_filename(fname: str) -> str:
    m = re.search(r"\(([^()]*)\)\.patch\.toml$", fname)
    if not m:
        return "Standard"
    s = m.group(1).replace("_", "/").strip()
    return f"Release {s}" if s.isdigit() else s


def is_emulator_only(name: str, desc: str) -> bool:
    n = name.lower()
    return any(w in n for w in _EMULATOR_WORDS) or bool(_EMULATOR_DESC.search(desc))


def categorize(name: str) -> str:
    s = name.lower()
    for cat, words in _CATEGORIES:
        if any(w in s for w in words):
            return cat
    return "Other"


def parse_patch_file(path: Path) -> tuple[str, str, LibBuild]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    tid = str(raw.get("title_id", "")).strip().upper()
    name = str(raw.get("title_name", "")).strip()
    if not re.fullmatch(r"[0-9A-F]{8}", tid) or not name:
        raise ValueError("missing title_id or title_name")
    h = raw.get("hash")
    hashes = [int(str(x), 16) for x in (h if isinstance(h, list) else [h]) if str(x or "").strip()]
    if not hashes:
        raise ValueError("no build hash")
    patches: list[LibPatch] = []
    for p in raw.get("patch", []) or []:
        if not isinstance(p, dict):
            continue
        writes: list[Write] = []
        ok = True
        for kind in _KINDS:
            for e in p.get(kind, []) or []:
                try:
                    addr = int(e["address"])
                    data = _value_bytes(kind, e["value"])
                except (KeyError, TypeError, ValueError, OverflowError, struct.error):
                    ok = False
                    continue
                if not (0 < addr <= 0xFFFFFFFF) or not data or addr + len(data) > 0x100000000:
                    ok = False
                    continue
                writes.append(Write(addr, data))
        pname = str(p.get("name") or "Unnamed patch").strip()
        if not ok or not writes:
            continue                           # Xenia also skips patches it can't read
        desc = str(p.get("desc") or p.get("Desc") or "").strip()
        patches.append(LibPatch(pname, desc, str(p.get("author") or "").strip(), writes,
                                is_emulator_only(pname, desc)))
    build = LibBuild(f"{hashes[0]:016X}", hashes, _label_from_filename(path.name), path.name, patches)
    return tid, name, build


def load_library(folder: Path | None = None) -> Library:
    folder = folder or library_dir()
    titles: dict[str, LibTitle] = {}
    errors: list[str] = []
    files = sorted((folder / "patches").glob("*.patch.toml")) if (folder / "patches").is_dir() else []
    for f in files:
        try:
            tid, name, build = parse_patch_file(f)
        except Exception as e:  # noqa: BLE001 - a broken file must not break the library
            errors.append(f"{f.name}: {e}")
            continue
        if not build.patches:
            continue
        t = titles.setdefault(tid, LibTitle(tid, name))
        if len(name) < len(t.name) or not t.name:
            t.name = name                      # several files per game: keep the plainest name
        t.builds.append(build)
    for t in titles.values():
        t.builds.sort(key=lambda b: (b.label != "Standard", b.label.lower()))
        seen: dict[str, int] = {}
        for b in t.builds:                     # make labels unique within a title
            n = seen[b.label] = seen.get(b.label, 0) + 1
            if n > 1:
                b.label = f"{b.label} ({n})"
    meta = {}
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    # Latin names A-Z, then names in other scripts
    return Library(sorted(titles.values(), key=lambda t: (not _fold(t.name), _fold(t.name) or t.name)), errors,
                   float(meta.get("fetched_at", 0)))


def needs_refresh(lib: Library | None) -> bool:
    return lib is None or not lib.titles or time.time() - lib.fetched_at > REFRESH_DAYS * 86400


def download_library(progress=None, folder: Path | None = None, url: str = ZIP_URL) -> Library:
    """Download the latest library and swap it in. The old copy stays if anything fails."""
    folder = folder or library_dir()
    req = urllib.request.Request(url, headers={"User-Agent": "360LiveMods"})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > MAX_ZIP_BYTES:
                raise OSError("The library download is unexpectedly large - stopped.")
            if progress:
                progress(buf.tell(), total)
    tmp = folder.with_name(folder.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "patches").mkdir(parents=True)
    count = 0
    with zipfile.ZipFile(buf) as z:
        for info in z.infolist():
            parts = info.filename.split("/")
            # <repo>-main/patches/<file>.patch.toml  - take only those, flattened (no path tricks)
            if len(parts) == 3 and parts[1] == "patches" and parts[2].endswith(".patch.toml") \
                    and "\\" not in parts[2] and not parts[2].startswith(".") and info.file_size < 2_000_000:
                (tmp / "patches" / parts[2]).write_bytes(z.read(info))
                count += 1
    if count == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise OSError("The download didn't contain any patch files.")
    (tmp / "meta.json").write_text(json.dumps({"fetched_at": time.time(), "files": count,
                                               "source": PAGE_URL}), encoding="utf-8")
    lib = load_library(tmp)
    if not lib.titles:
        shutil.rmtree(tmp, ignore_errors=True)
        raise OSError("None of the downloaded patch files could be read.")
    old = folder.with_name(folder.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if folder.exists():
        folder.rename(old)
    tmp.rename(folder)
    shutil.rmtree(old, ignore_errors=True)
    return load_library(folder)


# ------------------------------------------------------------------------ app model
EMULATOR_WARNING = ("This patch was made to work around a problem in the Xenia emulator. A real console "
                    "doesn't have that problem, and changing working code may crash the game.")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "patch"


def to_game(title: LibTitle) -> Game:
    """Present a library title with the same model the app uses for hand-made definitions.
    Each build is a 'version'; a mod collects the same-named patch from every build that has it."""
    versions = [Version(b.id, b.label, f"Build {b.id[:8]}", 0, []) for b in title.builds]
    mods: dict[str, Mod] = {}
    for b in title.builds:
        names: dict[str, int] = {}
        for p in b.patches:
            n = names[p.name] = names.get(p.name, 0) + 1
            name = p.name if n == 1 else f"{p.name} ({n})"
            key = _slug(name)
            m = mods.get(key)
            if m is None:
                m = mods[key] = Mod(
                    id=key, name=name, description=p.desc,
                    category="Emulator fixes" if p.emulator_only else categorize(name),
                    default=False, status="unstable" if p.emulator_only else "community",
                    credit=p.author, warning=EMULATOR_WARNING if p.emulator_only else "",
                    patches={}, unavailable={})
            elif not m.description and p.desc:
                m.description = p.desc
            m.patches[b.id] = [Patch(w.address, b"", w.data) for w in p.writes]
    order = {"Performance": 0, "Visual": 1, "Gameplay": 2, "Extras": 3, "Other": 4, "Emulator fixes": 5}
    ordered = sorted(mods.values(), key=lambda m: (order.get(m.category, 9), m.name.lower()))
    n = len(title.builds)
    return Game(
        id=title.game_id, name=title.name, title_id=title.title_id,
        description=f"Community patches from the Xenia library · {n} known version{'s' * (n != 1)}",
        notes=[], versions=versions, mods=ordered,
        credits=[f"Patches from the Xenia community library ({PAGE_URL}); authors are shown on each patch"],
        source="library", banner=None, library=title)
