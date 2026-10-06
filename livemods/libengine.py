"""Engine for community-library games.

Hand-made definitions (engine.Engine) know each version's code in advance. Library patches don't:
a patch file only says "write this value at this address" for one exact build. So this engine:

1. waits for a newly started game. Whatever was running when Start was pressed is left alone
   while it keeps running, and the dashboard is recognised and never scanned;
2. the first time it sees a build, reads its code and computes Xenia's build hash (identify.py).
   If that matches a library file for the selected game, the build is remembered together with the
   original bytes at every patch site, and the player is asked to restart the game so every patch
   goes in from the very start;
3. from then on, recognises the build the moment it loads, waits for the system loader to finish
   (so a title update being applied on top of the base game is never disturbed), checks each patch
   site and writes.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import engine as _engine
from .engine import Engine, RunResult, SETTLE_RETRIES, RECONNECT_DELAY
from .games import Game, Mod
from .identify import (BuildCache, FastReader, IdentifyError, fingerprint, hash_image,
                       record_from_scan, thunk_state, title_module)
from .library import Library, LibBuild, LibTitle
from .xbdm import Module, XbdmClient, XbdmError

IDENTIFY_SETTLE = 3.0     # a new build must stay unchanged this long before it is scanned
BOOT_SETTLE = 0.3         # ...and a known one this long before it is patched
NO_THUNK_SETTLE = 2.0     # settle time for a build without an import thunk to watch
MAX_UNKNOWN_SCANS = 2     # builds that never match are scanned at most this many times
POLL = 0.1
CHANGE_CHECK_BYTES = 0x100000   # while scanning, check every MB that the game is still the same

# Dashboards are recognised by their executable's name, or by the name of the folder it runs from
# (never by words anywhere in the path: a game kept in "\FSD Games\..." is still a game).
_DASHBOARD_EXE = re.compile(r"^(aurora|fsd\d?|freestyle\w*|xexmenu|dash|xshell|dashboard)\.xex$", re.I)
_DASHBOARD_DIR = {"aurora", "freestyle", "freestyledash", "fsd", "fsd2", "fsd3", "xexmenu", "dashboard"}
_GOD_TITLE = re.compile(r"\\content\\[0-9a-f]{16}\\([0-9a-f]{8})\\", re.I)


class _Changed(IdentifyError):
    pass


@dataclass
class Snap:
    module: Module
    head: bytes | None
    fp: str | None


class LibraryEngine(Engine):
    def __init__(self, on_event, library: Library, cache: BuildCache | None = None):
        super().__init__(on_event)
        self.library = library
        self.cache = cache or BuildCache()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _snapshot(x: XbdmClient) -> Snap | None:
        m = title_module(x.modules())
        if m is None:
            return None
        head = x.read(m.base, 0x1000)
        return Snap(m, head, fingerprint(m, head) if head and head[:2] == b"MZ" else None)

    def _match(self, rec: dict | None, title: LibTitle) -> tuple[LibTitle, LibBuild] | None:
        """Which library build a cached record is, preferring the selected game."""
        if not rec:
            return None
        hits = []
        for h in rec.get("hashes", {}).values():
            try:
                hits += self.library.by_hash.get(int(h, 16), [])
            except ValueError:
                continue
        if not hits:
            return None
        return next(((t, b) for t, b in hits if t.title_id == title.title_id), hits[0])

    @staticmethod
    def _complete(rec: dict, build: LibBuild) -> bool:
        """Do we know the original bytes at every patch site in this build's code?"""
        lo, hi = rec.get("code", (0, 0))
        orig = rec.get("originals", {})
        return all(f"{w.address:08X}" in orig for p in build.patches for w in p.writes
                   if lo <= w.address and w.address + len(w.data) <= hi)

    @staticmethod
    def _thunks(rec: dict) -> list[int]:
        t = rec.get("thunks")
        if t is None and rec.get("thunk") is not None:
            t = [rec["thunk"]]
        return [int(a) for a in (t or [])]

    def _loader_done(self, x: XbdmClient, rec: dict) -> bool:
        """The system resolves a game's imports after loading it (and after applying a title
        update on top). Until then, nothing may be written."""
        return all(thunk_state(x.read(a, 16)) == "resolved" for a in self._thunks(rec))

    def _hint(self, snap: Snap) -> tuple[str, str | None]:
        """('dashboard' | 'title' | '', title id) from the running executable's name and path.
        Asked on a separate connection: if this XBDM answers in an unexpected way, the main
        connection is never thrown out of step."""
        try:
            with XbdmClient(self._host, timeout=3.0) as t:
                path = t.running_path() or ""
        except Exception:  # noqa: BLE001 - only a hint
            path = ""
        exe = path.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        if exe.endswith((".xex", ".exe")) and exe != snap.module.name.lower():
            path = ""                       # describes something else (e.g. the previous title)
        m = _GOD_TITLE.search(path)
        if m:
            return "title", m.group(1).upper()
        folder = path.replace("/", "\\").rsplit("\\", 2)[-2].lower() if path.count("\\") >= 1 else ""
        if _DASHBOARD_EXE.match(snap.module.name) or folder in _DASHBOARD_DIR:
            return "dashboard", None
        return "", None

    # -------------------------------------------------------------- identify
    def _identify(self, host: str, x: XbdmClient, snap: Snap):
        reader = FastReader(host)
        shown = [-1.0]
        checked = [0]
        done_bytes = [0]

        def progress(f: float, n: int):
            done_bytes[0] = int(f * n)
            if f - shown[0] >= 0.05 or f >= 1:
                shown[0] = f
                self._emit("status", f"Identifying the game that started… {f * 100:.0f}%")

        def stop() -> bool:
            if self.stopped:
                return True
            if done_bytes[0] - checked[0] >= CHANGE_CHECK_BYTES:
                checked[0] = done_bytes[0]
                now = self._snapshot(x)
                if now is None or now.fp != snap.fp:
                    raise _Changed("the game changed while it was being read")
            return False

        def read(a: int, n: int) -> bytes:
            return reader.read(x, a, n, lambda f: progress(f, n), stop)

        t0 = time.time()
        scan = hash_image(snap.module.base, snap.head, read)
        self._emit("info", f"Read {len(scan.image) / 1e6:.1f} MB of code in {time.time() - t0:.0f} s"
                           f"{' (fast mode)' if reader.fast else ''}. Build hash {scan.hashes[0x10000]:016X}.")
        return scan

    def _first_time(self, host: str, x: XbdmClient, snap: Snap, title: LibTitle,
                    old: dict | None) -> str:
        """Scan a build. Returns 'identified', 'other' (another game), 'unknown' or 'changed'."""
        if old and old.get("build"):
            self._emit("info", "The community library has new patches for this version, so it is "
                               "being checked again (about a minute).")
        else:
            self._emit("info", "New game started: reading its code to identify the exact version. "
                               "This takes about a minute and only happens once per version.")
        try:
            scan = self._identify(host, x, snap)
        except _Changed:
            self._emit("info", "The game changed while it was being read (a title update loading on "
                               "top, or the game closed) - looking again.")
            return "changed"
        after = self._snapshot(x)
        if after is None or after.fp != snap.fp:
            self._emit("info", "The game changed while it was being read - looking again.")
            return "changed"
        if scan.thunks and not any(thunk_state(scan.original(t, 16)) == "resolved" for t in scan.thunks):
            # read before the system finished loading it (a title update may still be applied on top)
            self._emit("info", "The game was still loading while it was read - reading it again.")
            return "changed"
        rec = record_from_scan(scan, snap.module)
        hit = self._match(rec, title)
        if hit is None:
            prev = old or {}
            fresh = prev.get("seen", 0) < self.library.fetched_at      # library updated since
            rec["unknown_scans"] = 1 if fresh else int(prev.get("unknown_scans", 0)) + 1
            self.cache.put(snap.fp, rec)
            self._emit("warn", f"The game that started (build {scan.hashes[0x10000]:016X}) isn't a version of "
                               f"{title.name} in the community library, so nothing was changed. If it is "
                               f"{title.name}, restart it while 360 LiveMods is waiting to check once more.")
            return "unknown"
        t, b = hit
        rec["title_id"], rec["build"] = t.title_id, b.id
        lo, hi = rec["code"]
        for p in b.patches:
            for w in p.writes:
                if lo <= w.address < hi:
                    o = scan.original(w.address, len(w.data))
                    if o is not None:
                        rec["originals"][f"{w.address:08X}"] = o.hex().upper()
        if old:
            rec["restore"] = old.get("restore", {})
        if not self.cache.put(snap.fp, rec):
            self._emit("warn", "Couldn't save what was learned about this version (is builds.json open in "
                               "another program?). It will be identified again next time.")
        if t.title_id != title.title_id:
            self._emit("error", f"That's {t.name} ({b.label}), not {title.name}. Nothing was changed.")
            return "other"
        self._emit("ok", f"Identified {title.name} - {b.label}. You won't have to wait for this again.")
        self._emit("warn", f"Now quit {title.name} to the dashboard and start it again: the mods go in the "
                           "moment it loads. Leave 360 LiveMods waiting.")
        return "identified"

    # ---------------------------------------------------------------- patching
    def _apply_mod(self, x: XbdmClient, mod: Mod, build: LibBuild, module: Module, rec: dict,
                   restore: bool = False) -> bool:
        plist = mod.patches.get(build.id)
        if not plist:
            self._emit("info", f"{mod.name}: not part of this version ({build.label}) - skipped")
            return False
        lo, hi = module.base, module.base + module.size
        code_lo, code_hi = rec.get("code", (0, 0))
        originals, saved = rec.setdefault("originals", {}), rec.setdefault("restore", {})
        for p in plist:
            if not (lo <= p.address and p.address + len(p.patched) <= hi):
                self._emit("warn", f"{mod.name}: skipped - 0x{p.address:08X} is outside the game's "
                                   f"memory (this kind of patch only works in the emulator).")
                return False
            if not restore and code_lo <= p.address < code_hi and f"{p.address:08X}" not in originals:
                self._emit("warn", f"{mod.name}: skipped - this patch changed after the game was identified. "
                                   "Select the game again in the sidebar and restart it.")
                return False
        bad = None
        seen: dict[str, str] = {}
        todo: list = []
        for _ in range(SETTLE_RETRIES):
            todo, bad, seen = [], None, {}
            for p in plist:
                key = f"{p.address:08X}"
                n = len(p.patched)
                cur = x.read(p.address, n)
                if cur is None:
                    bad = (p, "isn't readable")
                    continue
                if restore:
                    if cur == p.patched:
                        orig = saved.get(key) or originals.get(key)
                        if not orig or len(orig) != 2 * n:
                            bad = (p, "can't be restored (its original value is unknown)")
                            break
                        todo.append((p, bytes.fromhex(orig)))
                    continue
                if cur == p.patched:
                    continue
                orig = originals.get(key)
                if code_lo <= p.address < code_hi and (not orig or cur.hex().upper() != orig.upper()):
                    bad = (p, f"holds {cur.hex().upper()}, expected {(orig or '?').upper()}")
                    continue
                todo.append((p, p.patched))
                seen[key] = cur.hex().upper()
            if bad is None or (restore and bad[1].startswith("can't")):
                break
            if self.stopped:
                return False
            time.sleep(0.1)
        if bad is not None:
            p, why = bad
            self._emit("warn", f"{mod.name}: skipped - 0x{p.address:08X} {why}. Nothing written for this mod.")
            return False
        saved.update(seen)
        for p, val in todo:
            x.write(p.address, val)
            if x.read(p.address, len(val)) != val:
                self._emit("error", f"{mod.name}: write at 0x{p.address:08X} did not stick")
                return False
        self._emit("ok", f"{mod.name} {'restored' if restore else 'applied'}"
                         + ("" if todo else (" (nothing to undo)" if restore else " (already set)")))
        return True

    def _patch_and_watch(self, x: XbdmClient, title: LibTitle, build: LibBuild, snap: Snap, rec: dict,
                         mods: list[Mod], result: RunResult) -> str:
        """Returns 'done', or 'changed' if a different build took over while watching."""
        self._emit("status", "Version detected - patching")
        self._emit("info", f"Detected {title.name} - {build.label} (build {build.id[:8]}).")
        active = []
        try:
            for m in mods:
                if self.stopped:
                    break
                if self._apply_mod(x, m, build, snap.module, rec):
                    result.applied += 1
                    active.append(m)
                elif m.patches.get(build.id):
                    result.skipped += 1
                    result.failed.append(m.name)
            self.cache.save()
            if self.stopped:
                return "done"
            self._emit("status", "Patched - keeping watch while the game boots")
            self._emit("info", f"Watching for {_engine.WATCH_SECONDS} s while the game finishes booting…")
            end = time.time() + _engine.WATCH_SECONDS
            saved = rec.get("restore", {})
            while time.time() < end and not self.stopped:
                now = self._snapshot(x)
                if now is None or now.fp != snap.fp:
                    self._emit("warn", "The game's code changed while booting (a title update loading on "
                                       "top, or the game closed) - checking what's running now.")
                    return "changed"
                for m in active:
                    for p in m.patches[build.id]:
                        orig = saved.get(f"{p.address:08X}")
                        if orig and x.read(p.address, len(p.patched)) == bytes.fromhex(orig):
                            x.write(p.address, p.patched)
                            self._emit("warn", f"{m.name}: game reloaded that code - patched again")
                time.sleep(0.25)
            lost = [m.name for m in active
                    if any(x.read(p.address, len(p.patched)) != p.patched for p in m.patches[build.id])]
            if lost:
                self._emit("warn", "No longer in place after boot: " + ", ".join(lost))
                result.failed.extend(lost)
        except (OSError, XbdmError):
            self._emit("error", "Lost connection while the game was booting. If the game crashed, try "
                                "again with fewer mods to find the one causing it.")
            result.failed.append("(connection lost)")
        return "done"

    # --------------------------------------------------------------------- run
    def run(self, host: str, game: Game, version_id: str, mods: list[Mod],
            restore: bool = False, wait: bool = True) -> RunResult:
        self._stop.clear()
        self._host = host
        result = RunResult(failed=[])
        title: LibTitle = game.library
        if not mods:
            self._emit("warn", "No mods selected.")
            return result
        if not wait:
            return self._run_now(host, title, mods, restore, result)

        x: XbdmClient | None = None
        first = True
        present: str | None = None          # fingerprint currently running
        skip: set[str] = set()              # leave alone while it keeps running (dashboard, other game)
        hints: dict[str, tuple[str, str | None]] = {}   # per appearance of a build
        relaunch: str | None = None         # identified this run; waiting for a fresh start of it
        relaunch_gone = False
        since = 0.0                         # when `present` appeared
        last_state, last_print, t0 = "", 0.0, time.time()
        lost = False
        self._emit("status", f"Waiting for {title.name}… launch it now")
        while not self.stopped:
            state = "Waiting for the game to start"
            try:
                if x is None:
                    x = XbdmClient(host)
                snap = self._snapshot(x)
                if lost:
                    self._emit("info", "Connected to console.")
                    lost = False
                fp = snap.fp if snap else None
                if fp != present:
                    if present is not None:
                        skip.discard(present)
                        hints.pop(present, None)
                    present, since = fp, time.time()
                    if relaunch and fp != relaunch:
                        relaunch_gone = True
                if first:
                    first = False
                    if fp:
                        rec = self.cache.get(fp)
                        hit = self._match(rec, title)
                        if hit and hit[0].title_id == title.title_id and self._complete(rec, hit[1]):
                            if self._loader_done(x, rec):
                                self._emit("warn", f"{title.name} is already running - patching it now. For "
                                                   "mods that change how the game starts up, restart it.")
                                if self._patch_and_watch(x, title, hit[1], snap, rec, mods, result) == "done":
                                    break
                                result = RunResult(failed=[])
                            continue          # still loading: handled like a fresh launch
                        if fp not in hints:
                            hints[fp] = self._hint(snap)
                        kind, tid = hints[fp]
                        if kind == "dashboard" or (kind == "title" and tid != title.title_id):
                            skip.add(fp)
                        elif hit:
                            skip.add(fp)
                            self._emit("info", f"Something is already running ({snap.module.name}). If it's "
                                               f"{title.name}, quit it to the dashboard and start it again.")
                        else:
                            # possibly the game itself, already running: identifying is read-only, so
                            # check it now instead of asking for an extra restart
                            self._emit("info", f"Something is already running ({snap.module.name}) - checking "
                                               f"whether it's {title.name}.")
                elif fp is None:
                    if relaunch:
                        state = f"Waiting for {title.name} to start again"
                elif fp in skip:
                    if relaunch:
                        state = f"Waiting for {title.name} to start again"
                elif fp == relaunch and not relaunch_gone:
                    state = "Restart the game to apply the mods"
                else:
                    if fp == relaunch:
                        relaunch, relaunch_gone = None, False
                    rec = self.cache.get(fp)
                    hit = self._match(rec, title)
                    if hit and hit[0].title_id != title.title_id:
                        t, b = hit
                        self._emit("error", f"That's {t.name} ({b.label}), not {title.name}. Nothing was changed.")
                        skip.add(fp)
                    elif hit and self._complete(rec, hit[1]):
                        state = "Game loading"
                        settle = BOOT_SETTLE if self._thunks(rec) else NO_THUNK_SETTLE
                        if self._loader_done(x, rec) and time.time() - since >= settle:
                            if self._patch_and_watch(x, title, hit[1], snap, rec, mods, result) == "done":
                                break
                            result = RunResult(failed=[])
                            continue
                    elif rec and not hit and int(rec.get("unknown_scans", 0)) >= MAX_UNKNOWN_SCANS \
                            and rec.get("seen", 0) >= self.library.fetched_at:
                        self._emit("warn", f"The game that started (build {rec.get('hashes', {}).get('65536', '?')}) "
                                           f"isn't a version of {title.name} in the community library.")
                        skip.add(fp)
                    else:
                        settled = time.time() - since >= IDENTIFY_SETTLE
                        if settled and fp not in hints:
                            # asked only once it has settled: right after a launch, XBDM may still
                            # describe the previous title (often the dashboard)
                            hints[fp] = self._hint(snap)
                        kind, tid = hints.get(fp, ("", None))
                        if not settled:
                            state = "Game loading"
                        elif kind == "dashboard":
                            skip.add(fp)
                        elif kind == "title" and tid != title.title_id:
                            other = self.library.by_id.get(tid)
                            self._emit("error", f"That's {other.name if other else 'title ' + tid}, not "
                                                f"{title.name}. Nothing was changed.")
                            skip.add(fp)
                        else:
                            outcome = self._first_time(host, x, snap, title, rec)
                            if outcome == "identified":
                                relaunch, relaunch_gone = fp, False
                                self._emit("status", "Restart the game to apply the mods")
                                last_state, last_print = "Restart the game to apply the mods", time.time()
                            elif outcome == "changed":
                                since = time.time()       # let it settle again before re-reading
                            elif outcome in ("other", "unknown"):
                                skip.add(fp)
                                self._emit("status", f"Waiting for {title.name}… launch it now")
                                last_state = ""
                            continue
            except IdentifyError as e:
                if self.stopped:
                    break
                self._emit("warn", f"Couldn't finish identifying the game ({e}). It will be tried again the "
                                   "next time it starts.")
                if present:
                    skip.add(present)
            except (OSError, XbdmError) as e:
                if x:
                    x.close()
                x = None
                lost = True
                state = f"Can't reach the console ({e.__class__.__name__}) - retrying"
            now_t = time.time()
            if state != last_state or now_t - last_print > 5:
                self._emit("info", f"[{now_t - t0:5.1f}s] {state}")
                if state != last_state and not state.startswith("Waiting for the game"):
                    self._emit("status", state)
                last_state, last_print = state, now_t
            time.sleep(POLL if x else RECONNECT_DELAY)

        if x:
            x.close()
        if self.stopped and not result.applied and not result.failed:
            result.cancelled = True
            self._emit("status", "Stopped")
            return result
        self._finish(result, False)
        return result

    def _run_now(self, host: str, title: LibTitle, mods: list[Mod], restore: bool, result: RunResult) -> RunResult:
        with XbdmClient(host) as x:
            snap = self._snapshot(x)
            rec = self.cache.get(snap.fp) if snap and snap.fp else None
            hit = self._match(rec, title)
            if not hit or hit[0].title_id != title.title_id:
                self._emit("error", f"{title.name} isn't running, or this version hasn't been identified "
                                    f"yet (press Start, then launch the game).")
                self._emit("status", "Game not running")
                return result
            if not self._loader_done(x, rec):
                self._emit("error", f"{title.name} is still loading. Try again in a moment.")
                self._emit("status", "Game not running")
                return result
            b = hit[1]
            mods = [m for m in mods if m.patches.get(b.id)]
            self._emit("status", f"Version detected - {'restoring' if restore else 'patching'}")
            for m in mods:
                if self._apply_mod(x, m, b, snap.module, rec, restore=restore):
                    result.applied += 1
                else:
                    result.skipped += 1
                    result.failed.append(m.name)
            self.cache.save()
        self._finish(result, restore)
        return result
