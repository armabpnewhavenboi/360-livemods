"""Engine for community-library games.

Hand-made definitions (engine.Engine) know each version's code in advance. Library patches don't:
a patch file only says "write this value at this address" for one exact build. So this engine:

1. waits for a newly started game (whatever was running when Start was pressed - usually the
   dashboard - is ignored),
2. the first time it sees a build, reads its code and computes Xenia's build hash (identify.py).
   If that matches a library file for the selected game, the build is remembered and the player is
   asked to restart the game, so every patch goes in from the very start;
3. from then on, recognises the build the moment it loads, waits for the system loader to finish
   (so a title update being applied on top is never disturbed), checks each patch site and writes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .engine import Engine, RunResult, SETTLE_RETRIES, WAIT_POLL, RECONNECT_DELAY
from . import engine as _engine
from .games import Game, Mod
from .identify import (BuildCache, FastReader, IdentifyError, fingerprint, hash_image,
                       record_from_scan, thunk_state, title_module)
from .library import Library, LibBuild, LibTitle
from .xbdm import Module, XbdmClient, XbdmError

IDENTIFY_SETTLE = 3.0     # a new build must stay unchanged this long before it is scanned
BOOT_SETTLE = 0.3         # ...and a known one this long before it is patched
POLL = 0.1


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
            hits += self.library.by_hash.get(int(h, 16), [])
        if not hits:
            return None
        return next(((t, b) for t, b in hits if t.title_id == title.title_id), hits[0])

    @staticmethod
    def _loader_done(x: XbdmClient, rec: dict) -> bool:
        """The system resolves a game's imports after loading it (and after applying a title
        update on top). Until then, nothing may be written."""
        t = rec.get("thunk")
        return t is None or thunk_state(x.read(int(t), 16)) == "resolved"

    # -------------------------------------------------------------- identify
    def _identify(self, host: str, x: XbdmClient, snap: Snap, title: LibTitle):
        reader = FastReader(host)
        last = [-1.0]

        def progress(f: float):
            if f - last[0] >= 0.05 or f >= 1:
                last[0] = f
                self._emit("status", f"Identifying your copy of {title.name}… {f * 100:.0f}%")
        t0 = time.time()
        scan = hash_image(snap.module.base, snap.head,
                          lambda a, n: reader.read(x, a, n, progress, lambda: self.stopped))
        self._emit("info", f"Read {len(scan.image) / 1e6:.1f} MB of code in {time.time() - t0:.0f} s"
                           f"{' (fast mode)' if reader.fast else ''}. Build hash "
                           f"{scan.hashes[0x10000]:016X}.")
        return scan

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
        bad = None
        for _ in range(SETTLE_RETRIES):
            todo, bad = [], None
            for p in plist:
                key = f"{p.address:08X}"
                n = len(p.patched)
                cur = x.read(p.address, n)
                if cur is None:
                    bad = (p, "unreadable")
                    continue
                if restore:
                    if cur == p.patched:
                        orig = saved.get(key) or originals.get(key)
                        if not orig or len(orig) != 2 * n:
                            bad = (p, "original value unknown")
                            continue
                        todo.append((p, bytes.fromhex(orig)))
                    continue
                if cur == p.patched:
                    continue
                orig = originals.get(key)
                in_code = code_lo <= p.address < code_hi
                if in_code and orig and len(orig) == 2 * n and cur.hex().upper() != orig.upper():
                    bad = (p, f"holds {cur.hex().upper()}, expected {orig.upper()}")
                    continue
                todo.append((p, p.patched))
                saved[key] = cur.hex().upper()
            if bad is None or bad[1] == "original value unknown":
                break
            if self.stopped:
                return False
            time.sleep(0.1)
        if bad is not None:
            p, why = bad
            self._emit("warn", f"{mod.name}: skipped - 0x{p.address:08X} {why}. Nothing written for this mod.")
            return False
        for p, val in todo:
            x.write(p.address, val)
            if x.read(p.address, len(val)) != val:
                self._emit("error", f"{mod.name}: write at 0x{p.address:08X} did not stick")
                return False
        self._emit("ok", f"{mod.name} {'restored' if restore else 'applied'}"
                         + ("" if todo else (" (nothing to undo)" if restore else " (already set)")))
        return True

    def _patch_and_watch(self, x: XbdmClient, title: LibTitle, build: LibBuild, snap: Snap, rec: dict,
                         mods: list[Mod], watch: bool, result: RunResult) -> str:
        """Returns 'done', or 'changed' if a different build took over while watching."""
        self._emit("status", "Version detected - patching")
        self._emit("info", f"Detected {title.name} - {build.label} (build {build.id[:8]}).")
        active = []
        for m in mods:
            if self.stopped:
                break
            if self._apply_mod(x, m, build, snap.module, rec):
                result.applied += 1
                active.append(m)
            else:
                result.skipped += 1
                result.failed.append(m.name)
        self.cache.save()
        if not watch or self.stopped:
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
        return "done"

    # --------------------------------------------------------------------- run
    def run(self, host: str, game: Game, version_id: str, mods: list[Mod],
            restore: bool = False, wait: bool = True) -> RunResult:
        self._stop.clear()
        result = RunResult(failed=[])
        title: LibTitle = game.library
        if not mods:
            self._emit("warn", "No mods selected.")
            return result
        if not wait:
            return self._run_now(host, title, mods, restore, result)

        x: XbdmClient | None = None
        ignore: set[str] = set()
        first = True
        relaunch: str | None = None      # identified this run: wait for the game to be restarted
        pending: tuple[str, float] | None = None
        last_state, last_print, t0 = "", 0.0, time.time()
        self._emit("status", f"Waiting for {title.name}… launch it now")
        while not self.stopped:
            try:
                if x is None:
                    x = XbdmClient(host)
                    if last_state.startswith("Can't reach"):
                        self._emit("info", "Connected to console.")
                snap = self._snapshot(x)
                fp = snap.fp if snap else None
                state = "Waiting for the game to start"
                if first:
                    first = False
                    rec = self.cache.get(fp) if fp else None
                    hit = self._match(rec, title)
                    if hit and hit[0].title_id == title.title_id:
                        if self._loader_done(x, rec):
                            self._emit("warn", f"{title.name} is already running - patching it now. For "
                                               "mods that change how the game starts up, restart it.")
                            if self._patch_and_watch(x, title, hit[1], snap, rec, mods, True, result) == "done":
                                break
                            result = RunResult(failed=[])
                        continue          # still loading: handled like a fresh launch
                    if fp:
                        ignore.add(fp)   # the dashboard (or another game) that was already running
                        if snap and not snap.module.name.lower().startswith(("aurora", "fsd", "dash")):
                            self._emit("info", f"Something is already running ({snap.module.name}). "
                                               f"If it's {title.name}, quit it to the dashboard and "
                                               "start it again.")
                elif fp is None:
                    if relaunch:
                        state = f"Waiting for {title.name} to start again"
                    pending = None
                else:
                    if relaunch and fp != relaunch:
                        relaunch = None
                    if fp in ignore:
                        pending = None
                    elif fp == relaunch:
                        state = f"Restart {title.name} to apply the mods"
                    else:
                        rec = self.cache.get(fp)
                        hit = self._match(rec, title)
                        if rec and not hit and rec.get("hashes"):
                            self._emit("error", f"This version of the game (build {rec['hashes'].get('65536', '?')}) "
                                                f"isn't in the community library for {title.name}. Nothing was changed.")
                            self._emit("status", "Unknown game version")
                            ignore.add(fp)
                        elif rec and hit:
                            t, b = hit
                            if t.title_id != title.title_id:
                                self._emit("error", f"That's {t.name} ({b.label}), not {title.name}. "
                                                    f"Nothing was changed.")
                                ignore.add(fp)
                            elif self._loader_done(x, rec):
                                if pending is None or pending[0] != fp:
                                    pending = (fp, time.time())
                                elif time.time() - pending[1] >= BOOT_SETTLE:
                                    outcome = self._patch_and_watch(x, t, b, snap, rec, mods, True, result)
                                    if outcome == "done":
                                        break
                                    result, pending = RunResult(failed=[]), None
                                    continue
                                state = "Game loading"
                            else:
                                state = "Game loading"
                        else:
                            # not seen before: let it settle, then identify it
                            if pending is None or pending[0] != fp:
                                pending = (fp, time.time())
                                self._emit("info", f"New game started ({snap.module.name}). Waiting for it "
                                                   "to finish loading before identifying it…")
                            elif time.time() - pending[1] >= IDENTIFY_SETTLE:
                                self._first_time(host, x, snap, title, ignore)
                                pending = None
                                now = self._snapshot(x)
                                if now and now.fp == fp and fp not in ignore:
                                    relaunch = fp
                                continue
                            state = "Game loading"
            except IdentifyError as e:
                self._emit("error", f"Couldn't identify the game: {e}")
                if snap and snap.fp:
                    ignore.add(snap.fp)
                state = "Couldn't identify the game"
            except (OSError, XbdmError) as e:
                if x:
                    x.close()
                x = None
                pending = None
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
        if self.stopped and not result.applied:
            result.cancelled = True
            self._emit("status", "Stopped")
            return result
        self._finish(result, False)
        return result

    def _first_time(self, host: str, x: XbdmClient, snap: Snap, title: LibTitle, ignore: set[str]) -> None:
        self._emit("info", f"First time with this version: reading its code to identify it exactly. "
                           f"This takes about a minute and only happens once.")
        scan = self._identify(host, x, snap, title)
        after = self._snapshot(x)
        if after is None or after.fp != snap.fp:
            self._emit("info", "The game changed while it was being read (a title update loading on top?) "
                               "- trying again.")
            return
        rec = record_from_scan(scan, snap.module)
        hit = self._match(rec, title)
        if hit is None:
            self._emit("error", f"The game that started (build {scan.hashes[0x10000]:016X}) isn't any version "
                                f"of {title.name} in the community library, so nothing was changed. If it is "
                                f"{title.name}, restart it with 360 LiveMods waiting to try once more.")
            self._emit("status", "Unknown game version")
            ignore.add(snap.fp)
            return
        t, b = hit
        rec["title_id"], rec["build"] = t.title_id, b.id
        lo, hi = rec["code"]
        for tb in t.builds:
            if tb.id != b.id:
                continue
            for p in tb.patches:
                for w in p.writes:
                    if lo <= w.address < hi:
                        o = scan.original(w.address, len(w.data))
                        if o is not None:
                            rec["originals"][f"{w.address:08X}"] = o.hex().upper()
        self.cache.put(snap.fp, rec)
        if t.title_id != title.title_id:
            self._emit("error", f"That's {t.name} ({b.label}), not {title.name}. Nothing was changed.")
            self._emit("status", "A different game is running")
            ignore.add(snap.fp)
            return
        self._emit("ok", f"Identified {title.name} - {b.label}. You won't have to wait for this again.")
        self._emit("warn", f"Now quit {title.name} to the dashboard and start it again: the mods go in the "
                           "moment it loads. Leave 360 LiveMods waiting.")
        self._emit("status", f"Restart {title.name} to apply the mods")

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
            b = hit[1]
            self._emit("status", f"Version detected - {'restoring' if restore else 'patching'}")
            for m in mods:
                if self._apply_mod(x, m, b, snap.module, rec, restore=restore):
                    result.applied += 1
                elif m.patches.get(b.id):
                    result.skipped += 1
                    result.failed.append(m.name)
            self.cache.save()
        self._finish(result, restore)
        return result
