"""Patch engine: wait for a game to boot, patch it in memory over XBDM, verify, and watch.

The engine is UI-agnostic. It reports progress through an `on_event(kind, message)` callback:
    kind = "info" | "ok" | "warn" | "error" | "status"
"status" messages are short one-line states meant for a status bar.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .games import Game, Mod, Patch, Version
from .xbdm import XbdmClient, XbdmError

EventFn = Callable[[str, str], None]

WAIT_POLL = 0.02          # seconds between checks while waiting for the game
RECONNECT_DELAY = 0.5     # seconds before reconnecting after a dropped connection
SETTLE_RETRIES = 25       # x 0.1 s - time allowed for a patch site to finish loading
WATCH_SECONDS = 30        # keep watching after patching in case the game overwrites anything


@dataclass
class RunResult:
    applied: int = 0
    skipped: int = 0
    failed: list[str] | None = None
    cancelled: bool = False
    wrong_version: str | None = None


class Engine:
    def __init__(self, on_event: EventFn):
        self.on_event = on_event
        self._stop = threading.Event()

    # ----------------------------------------------------------------- control
    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _emit(self, kind: str, msg: str) -> None:
        try:
            self.on_event(kind, msg)
        except Exception:  # noqa: BLE001 - never let a UI error kill the engine
            pass

    # ------------------------------------------------------------------- steps
    def _wait_for_game(self, host: str, game: Game, version: Version) -> tuple[XbdmClient | None, str | None]:
        """Block until the selected version's code is in memory.
        Returns (client, None) on success, (None, other_version_name) if a different version
        is running, or (None, None) if cancelled."""
        others = [v for v in game.versions if v.id != version.id]
        client: XbdmClient | None = None
        last_state, last_print, start = "", 0.0, time.time()
        other_seen_since: dict[str, float] = {}
        self._emit("status", f"Waiting for {game.name}… launch it now")
        while not self.stopped:
            try:
                if client is None:
                    client = XbdmClient(host)
                    if last_state.startswith("Can't reach"):
                        self._emit("info", "Connected to console.")
                cur = client.read(version.detect_address, 4)
                if cur in version.detect_values:
                    return client, None
                # A different version is running: wait a moment (the title update loads on top of
                # the base game during boot), then report instead of patching the wrong code.
                for o in others:
                    if client.read(o.detect_address, 4) in o.detect_values:
                        first = other_seen_since.setdefault(o.id, time.time())
                        if time.time() - first > 6:
                            client.close()
                            return None, o.name
                    else:
                        other_seen_since.pop(o.id, None)
                state = "Waiting for the game to start"
            except (OSError, XbdmError) as e:
                if client:
                    client.close()
                client = None
                state = f"Can't reach the console ({e.__class__.__name__}) - retrying"
            now = time.time()
            if state != last_state or now - last_print > 5:
                self._emit("info", f"[{now - start:5.1f}s] {state}")
                last_state, last_print = state, now
            time.sleep(WAIT_POLL if client else RECONNECT_DELAY)
        if client:
            client.close()
        return None, None

    def _apply_mod(self, x: XbdmClient, mod: Mod, version_id: str, restore: bool = False) -> bool:
        patches = mod.patches[version_id]
        for _ in range(SETTLE_RETRIES):
            todo: list[tuple[Patch, bytes]] = []
            bad = None
            for p in patches:
                want_from, want_to = (p.patched, p.original) if restore else (p.original, p.patched)
                cur = x.read(p.address, len(p.original))
                if cur == want_to:
                    continue
                if cur == want_from:
                    todo.append((p, want_to))
                else:
                    bad = (p, cur, want_from)
            if bad is None:
                break
            if self.stopped:
                return False
            time.sleep(0.1)
        if bad is not None:
            p, cur, exp = bad
            got = cur.hex().upper() if cur else "unreadable"
            self._emit("warn", f"{mod.name}: skipped - 0x{p.address:08X} holds {got}, expected "
                               f"{exp.hex().upper()}. Nothing written for this mod.")
            return False
        for p, val in todo:
            x.write(p.address, val)
            if x.read(p.address, len(val)) != val:
                self._emit("error", f"{mod.name}: write at 0x{p.address:08X} did not stick")
                return False
        verb = "restored" if restore else "applied"
        self._emit("ok", f"{mod.name} {verb}" + ("" if todo else " (already set)"))
        return True

    # --------------------------------------------------------------------- run
    def run(self, host: str, game: Game, version_id: str, mods: list[Mod],
            restore: bool = False, wait: bool = True) -> RunResult:
        self._stop.clear()
        result = RunResult(failed=[])
        version = game.version(version_id)
        mods = [m for m in mods if m.available_for(version_id)]
        if not mods:
            self._emit("warn", "No mods selected for this version.")
            return result

        if wait:
            x, other = self._wait_for_game(host, game, version)
            if other:
                result.wrong_version = other
                self._emit("error", f"{game.name} is running as '{other}', but '{version.name}' is "
                                    f"selected. Nothing was changed. Switch the version and try again.")
                self._emit("status", "Wrong game version selected")
                return result
            if x is None:
                result.cancelled = True
                self._emit("status", "Stopped")
                return result
        else:
            x = XbdmClient(host)
            if x.read(version.detect_address, 4) not in version.detect_values:
                x.close()
                self._emit("error", f"{game.name} ({version.name}) is not running.")
                self._emit("status", "Game not running")
                return result

        with x:
            self._emit("status", f"{version.name} detected - patching")
            self._emit("info", f"Detected {game.name} - {version.name}.")
            for m in mods:
                if self.stopped:
                    break
                try:
                    if self._apply_mod(x, m, version_id, restore):
                        result.applied += 1
                    else:
                        result.skipped += 1
                        result.failed.append(m.name)
                except (OSError, XbdmError) as e:
                    self._emit("error", f"{m.name}: connection error ({e})")
                    result.failed.append(m.name)
                    break

            if restore or not wait or self.stopped:
                self._finish(result, restore)
                return result

            # Watch for a while: if a patch site is reloaded with original code, patch it again.
            self._emit("status", "Patched - keeping watch while the game boots")
            self._emit("info", f"Watching for {WATCH_SECONDS} s while the game finishes booting…")
            end = time.time() + WATCH_SECONDS
            others = [v for v in game.versions if v.id != version_id]
            try:
                while time.time() < end and not self.stopped:
                    # The title update loads over the base game during boot. If the base game was
                    # selected but the update takes over, our patches are gone - tell the user.
                    for o in others:
                        if x.read(o.detect_address, 4) in o.detect_values:
                            result.wrong_version = o.name
                            self._emit("error", f"The game turned out to be '{o.name}', not "
                                                f"'{version.name}'. Select '{o.name}' and restart the game.")
                            self._emit("status", "Wrong game version selected")
                            result.failed.append("(wrong version)")
                            return result
                    for m in mods:
                        if m.name in result.failed:
                            continue
                        for p in m.patches[version_id]:
                            if x.read(p.address, len(p.original)) == p.original:
                                x.write(p.address, p.patched)
                                self._emit("warn", f"{m.name}: game reloaded that code - patched again")
                    time.sleep(0.25)
                # Final check
                lost = []
                for m in mods:
                    if m.name in result.failed:
                        continue
                    if any(x.read(p.address, len(p.patched)) != p.patched for p in m.patches[version_id]):
                        lost.append(m.name)
                if lost:
                    self._emit("warn", "No longer in place after boot: " + ", ".join(lost))
                    result.failed.extend(lost)
            except (OSError, XbdmError):
                self._emit("error", "Lost connection while the game was booting. If the game crashed, "
                                    "try again with fewer mods to find the one causing it.")
                result.failed.append("(connection lost)")
        self._finish(result, restore)
        return result

    def _finish(self, result: RunResult, restore: bool) -> None:
        if result.failed:
            self._emit("warn", f"Done with problems: {', '.join(result.failed)}")
            self._emit("status", "Finished with warnings")
        else:
            self._emit("ok", "All done - original settings restored." if restore
                       else "All done. Load your save and enjoy.")
            self._emit("status", "Restored" if restore else "Mods active")


class FpsMonitor:
    """Polls a game's once-per-frame counter over XBDM and reports frames per second.

    on_sample(fps, average, low) is called about once a second; on_end(reason) once when the
    game stops (quit, crash) or stop() is called.
    """

    def __init__(self, host: str, version: Version, on_sample, on_end, interval: float = 1.0):
        self.host, self.version = host, version
        self.on_sample, self.on_end, self.interval = on_sample, on_end, interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        v = self.version
        if v.fps_pointer is None:
            self.on_end("This game has no frame counter defined.")
            return
        samples: list[float] = []
        misses = 0
        try:
            with XbdmClient(self.host) as x:
                last = None
                while not self._stop.is_set():
                    ptr = x.read(v.fps_pointer, 4)
                    cnt = x.read(int.from_bytes(ptr, "big") + v.fps_offset, 4) if ptr else None
                    now = time.time()
                    if cnt is None or x.read(v.detect_address, 4) not in v.detect_values:
                        misses += 1
                        if misses >= 3:
                            self.on_end("The game has closed.")
                            return
                    else:
                        misses = 0
                        c = int.from_bytes(cnt, "big")
                        if last and c >= last[1]:
                            fps = (c - last[1]) / (now - last[0])
                            if fps < 240:                      # ignore garbage during loads
                                samples.append(fps)
                                samples[:] = samples[-600:]    # last ~10 minutes
                                self.on_sample(fps, sum(samples) / len(samples), min(samples))
                        last = (now, c)
                    self._stop.wait(self.interval)
        except (OSError, XbdmError):
            self.on_end("Lost connection to the console.")
            return
        self.on_end("")
