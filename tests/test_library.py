"""Community library: parsing, build identification and the library engine, against a fake console.

Most tests use a small synthetic game image, so they run anywhere. Two extra tests check the hash
against Xenia's real values for Fable II and need memory images (not shipped):
    LIVEMODS_BASE_IMAGE=<base-game image>   LIVEMODS_TU_PRISTINE=<title-update image>
"""
from __future__ import annotations

import http.server
import io
import os
import random
import struct
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_xbdm import FakeXbdm  # noqa: E402
import livemods.xbdm as xb  # noqa: E402
from livemods import engine as eng_mod, libengine as lib_eng  # noqa: E402
from livemods.identify import BuildCache, hash_image, thunk_state  # noqa: E402
from livemods.library import (download_library, is_emulator_only, load_library,  # noqa: E402
                              parse_patch_file, to_game)
from livemods.libengine import LibraryEngine  # noqa: E402

BASE = 0x82000000


# ------------------------------------------------------------------ synthetic game images
def make_image(seed: int, thunks: str = "raw", size: int = 0x40000) -> bytearray:
    """A tiny 'game': PE headers, 128 KB of code with an import-thunk table, then data."""
    rnd = random.Random(seed)
    img = bytearray(rnd.getrandbits(8) for _ in range(size))
    img[0:0x400] = bytes(0x400)
    img[0:2] = b"MZ"
    struct.pack_into("<I", img, 0x3C, 0x80)
    img[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHI", img, 0x84, 0x1F2, 2, seed)          # machine, sections, timestamp
    struct.pack_into("<H", img, 0x80 + 20, 0xE0)                   # optional header size
    struct.pack_into("<I", img, 0x80 + 24 + 60, 0x400)             # size of headers
    sec = 0x80 + 24 + 0xE0
    for i, (nm, va, vs, ch) in enumerate([(b".text", 0x10000, 0x20000, 0x60000020),
                                          (b".data", 0x30000, 0x8000, 0xC0000040)]):
        o = sec + i * 40
        img[o:o + 8] = nm.ljust(8, b"\0")
        struct.pack_into("<II", img, o + 8, vs, va)
        struct.pack_into("<I", img, o + 36, ch)
    for k in range(4):                                             # import thunks at the end of code
        o = 0x2FF00 + k * 16
        if thunks == "raw":
            img[o:o + 8] = bytes.fromhex(f"0100{k:04X}0200{k:04X}")
        else:
            img[o:o + 8] = bytes.fromhex(f"3D608007396B{0x1000 + k * 0x10:04X}")
        img[o + 8:o + 16] = bytes.fromhex("7D6903A64E800420")
    return img


def xenia_hash(img: bytes) -> str:
    s = hash_image(BASE, bytes(img[:0x1000]), lambda a, n: bytes(img[a - BASE:a - BASE + n]))
    return f"{s.hashes[0x10000]:016X}"


def write_patches(folder: Path, files: dict[str, str]) -> Path:
    (folder / "patches").mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (folder / "patches" / name).write_text(body, encoding="utf-8")
    return folder


GAME_A = make_image(1)
GAME_A_TU = make_image(2)
OTHER = make_image(3)
DASH = make_image(4)


def resolved(img: bytearray) -> bytearray:
    out = bytearray(img)
    for k in range(4):
        o = 0x2FF00 + k * 16
        out[o:o + 8] = bytes.fromhex(f"3D608007396B{0x1000 + k * 0x10:04X}")
    return out


def lib_files() -> dict[str, str]:
    a, tu, o = xenia_hash(GAME_A), xenia_hash(GAME_A_TU), xenia_hash(OTHER)
    code_a = 0x82012340
    return {
        "4D530001 - Test Game.patch.toml": f'''
title_name = "Test Game"
title_id = "4D530001"
hash = "{a}"

[[patch]]
    name = "60 FPS"
    author = "someone"
    [[patch.be32]]
        address = {code_a:#x}
        value = 0x39600001

[[patch]]
    name = "Brighter"
    desc = "A data value"
    [[patch.f32]]
        address = 0x82030010
        value = 1.5
    [[patch.be8]]
        address = 0x82030020
        value = 0x1FF

[[patch]]
    name = "Heap patch"
    [[patch.be32]]
        address = 0x40001000
        value = 1

[[patch]]
    name = "Black Shading Fix"
    [[patch.be16]]
        address = 0x82012400
        value = 0x4800
''',
        "4D530001 - Test Game (TU1).patch.toml": f'''
title_name = "Test Game"
title_id = "4D530001"
hash = "{tu}"

[[patch]]
    name = "60 FPS"
    [[patch.be32]]
        address = 0x82012350
        value = 0x39600001
''',
        "4D530002 - Other Game.patch.toml": f'''
title_name = "Other Game"
title_id = "4D530002"
hash = ["{o}", "0000000000000001"]

[[patch]]
    name = "Something"
    [[patch.array]]
        address = 0x82011000
        value = "0xDEADBEEF"
''',
        "broken.patch.toml": "title_name = 'x'\n[[[",
    }


@pytest.fixture
def lib(tmp_path):
    return load_library(write_patches(tmp_path / "lib", lib_files()))


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(eng_mod, "WATCH_SECONDS", 1)
    monkeypatch.setattr(lib_eng, "IDENTIFY_SETTLE", 0.4)
    monkeypatch.setattr(lib_eng, "BOOT_SETTLE", 0.1)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


def run_engine(timeline, lib, title_id, mod_names, cache, stop_after=None, restore=False, wait=True,
               getmemex=True):
    srv = FakeXbdm(timeline, port=0, getmemex=getmemex)
    events = []
    e = LibraryEngine(lambda k, m: events.append((k, m)), lib, cache)
    game = to_game(lib.by_id[title_id])
    mods = [m for m in game.mods if m.name in mod_names]
    xb.XbdmClient.__init__.__defaults__ = (srv.port, 5.0)
    timer = threading.Timer(stop_after, e.stop) if stop_after else None
    if timer:
        timer.start()
    try:
        r = e.run("127.0.0.1", game, "*", mods, restore=restore, wait=wait)
    finally:
        if timer:
            timer.cancel()
        xb.XbdmClient.__init__.__defaults__ = (730, 5.0)
        srv.close()
    return r, events, srv


# ------------------------------------------------------------------------------- parsing
def test_parse_values_like_xenia(lib):
    assert len(lib) == 2 and len(lib.errors) == 1                  # the broken file is reported
    t = lib.by_id["4D530001"]
    assert [b.label for b in t.builds] == ["Standard", "TU1"]
    std = t.builds[0]
    bright = next(p for p in std.patches if p.name == "Brighter")
    w = {x.address: x.data for x in bright.writes}
    assert w[0x82030010] == struct.pack(">f", 1.5)
    assert w[0x82030020] == b"\xff"                                # be8 keeps the low byte
    assert next(p for p in std.patches if p.name == "Black Shading Fix").emulator_only
    other = lib.by_id["4D530002"].builds[0]
    assert other.patches[0].writes[0].data == bytes.fromhex("DEADBEEF")
    assert len(other.hashes) == 2 and 1 in lib.by_hash


def test_bad_entries_are_skipped(tmp_path):
    f = write_patches(tmp_path, {"4D530009 - X.patch.toml": '''
title_name = "X"
title_id = "4D530009"
hash = "0123456789ABCDEF"
[[patch]]
    name = "too big address"
    [[patch.be32]]
        address = 0x1830000000
        value = 1
[[patch]]
    name = "fine"
    [[patch.be16]]
        address = 0x82000010
        value = 0x12345
'''}) / "patches" / "4D530009 - X.patch.toml"
    _, _, b = parse_patch_file(f)
    assert [p.name for p in b.patches] == ["fine"]
    assert b.patches[0].writes[0].data == bytes.fromhex("2345")


def test_game_model_and_search(lib):
    g = to_game(lib.by_id["4D530001"])
    fps = next(m for m in g.mods if m.name == "60 FPS")
    assert set(fps.patches) == {b.id for b in lib.by_id["4D530001"].builds}
    assert fps.available_for("*") and fps.status == "community"
    shade = next(m for m in g.mods if m.name == "Black Shading Fix")
    assert shade.category == "Emulator fixes" and shade.status == "unstable" and shade.warning
    assert [t.title_id for t in lib.search("test")] == ["4D530001"]
    assert [t.title_id for t in lib.search("4d530002")] == ["4D530002"]
    assert len(lib.search("")) == 2


def test_emulator_detection_ignores_passing_mentions():
    assert not is_emulator_only("Unlock FPS", "Use with Xenia's vsync turned off.")
    assert is_emulator_only("Fix hang", "Works around a Xenia bug in the audio driver.")
    assert is_emulator_only("Occlusion Query Fix", "")


def test_download_extracts_only_patch_files(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("game-patches-main/patches/4D530001 - Test Game.patch.toml",
                   lib_files()["4D530001 - Test Game.patch.toml"])
        z.writestr("game-patches-main/patches/../../evil.patch.toml", "x")
        z.writestr("game-patches-main/README.md", "hi")
    body = buf.getvalue()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        dest = tmp_path / "library"
        got = download_library(folder=dest, url=f"http://127.0.0.1:{srv.server_port}/x.zip")
    finally:
        srv.shutdown()
    assert [t.title_id for t in got.titles] == ["4D530001"] and got.fetched_at > 0
    assert sorted(p.name for p in (dest / "patches").iterdir()) == ["4D530001 - Test Game.patch.toml"]
    assert not (tmp_path / "evil.patch.toml").exists()


# --------------------------------------------------------------------------- identification
def test_hash_ignores_how_imports_were_resolved():
    assert xenia_hash(GAME_A) == xenia_hash(resolved(GAME_A))
    assert thunk_state(bytes(resolved(GAME_A)[0x2FF00:0x2FF10])) == "resolved"
    assert thunk_state(bytes(GAME_A[0x2FF00:0x2FF10])) == "raw"


@pytest.mark.skipif(not (os.environ.get("LIVEMODS_BASE_IMAGE") and os.environ.get("LIVEMODS_TU_PRISTINE")),
                    reason="game images not provided")
def test_matches_xenia_hash_for_fable2():
    for env, want in (("LIVEMODS_BASE_IMAGE", "4145F96D2DEE2AB5"), ("LIVEMODS_TU_PRISTINE", "EE56F849188A6A20")):
        assert xenia_hash(bytearray(Path(os.environ[env]).read_bytes())) == want


# --------------------------------------------------------------------------------- engine
def boot(img, at, stages=(0.0, 0.25)):
    """A game booting at time `at`: raw thunks first, then resolved by the loader."""
    return [(at + stages[0], img), (at + stages[1], resolved(img))]


def test_first_launch_identifies_then_patches_from_boot(lib, tmp_path):
    cache = BuildCache(tmp_path / "builds.json")
    tl = [(0, DASH, "Aurora.xex")] + boot(GAME_A, 0.5) + [(4.0, DASH, "Aurora.xex")] + boot(GAME_A, 4.6)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS", "Brighter", "Heap patch"}, cache)
    msgs = " | ".join(m for k, m in ev if isinstance(m, str))
    assert "Identified Test Game" in msgs and "start it again" in msgs, msgs
    written = dict(srv.writes)
    assert written[0x82012340] == bytes.fromhex("39600001")
    assert written[0x82030010] == struct.pack(">f", 1.5) and written[0x82030020] == b"\xff"
    assert 0x40001000 not in written                         # outside the game's memory: skipped
    assert r.applied == 2 and "Heap patch" in r.failed
    rec = next(iter(cache.data["builds"].values()))
    assert rec["originals"]["82012340"] == GAME_A[0x12340:0x12344].hex().upper()


def test_known_build_waits_for_loader_and_title_update(lib, tmp_path):
    """Base build known, title update loads on top during boot: nothing may be written to the base
    code (the update is applied against it), and the update's own patches go in once it settles."""
    cache = BuildCache(tmp_path / "builds.json")
    # learn both builds first
    run_engine([(0, DASH, "Aurora.xex")] + boot(GAME_A, 0.3), lib, "4D530001", {"60 FPS"}, cache, stop_after=3)
    run_engine([(0, DASH, "Aurora.xex")] + boot(GAME_A_TU, 0.3), lib, "4D530001", {"60 FPS"}, cache, stop_after=3)
    assert len(cache.data["builds"]) == 2
    tl = [(0, DASH, "Aurora.xex"), (0.5, GAME_A), (1.5, GAME_A_TU), (1.8, resolved(GAME_A_TU))]
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache)
    assert srv.writes == [(0x82012350, bytes.fromhex("39600001"))], srv.writes
    assert r.applied == 1 and not r.failed


def test_other_game_is_not_touched(lib, tmp_path):
    cache = BuildCache(tmp_path / "builds.json")
    tl = [(0, DASH, "Aurora.xex")] + boot(OTHER, 0.3)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=4)
    assert srv.writes == []
    assert any("Other Game" in m and "not Test Game" in m for k, m in ev if isinstance(m, str))


def test_unknown_build_is_reported(lib, tmp_path):
    cache = BuildCache(tmp_path / "builds.json")
    tl = [(0, DASH, "Aurora.xex")] + boot(make_image(99), 0.3)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=4, getmemex=False)
    assert srv.writes == []
    assert any("isn't a version of Test Game" in m for k, m in ev if isinstance(m, str))


def test_changed_code_is_not_patched_and_restore_works(lib, tmp_path):
    cache = BuildCache(tmp_path / "builds.json")
    run_engine([(0, DASH, "Aurora.xex")] + boot(GAME_A, 0.3), lib, "4D530001", {"60 FPS"}, cache, stop_after=3)
    odd = bytearray(GAME_A)
    odd[0x12340:0x12344] = b"\x60\x00\x00\x00"            # something else changed that instruction
    r, ev, srv = run_engine([(0, DASH, "Aurora.xex")] + boot(odd, 0.3), lib, "4D530001",
                            {"60 FPS", "Brighter"}, cache)
    assert all(a != 0x82012340 for a, _ in srv.writes) and "60 FPS" in r.failed
    # restore puts the data values back while the game runs
    r2, ev2, srv2 = run_engine([(0, resolved(GAME_A))], lib, "4D530001", {"60 FPS", "Brighter"}, cache)
    assert r2.applied == 2
    r3, ev3, srv3 = run_engine([(0, _patched(resolved(GAME_A)))], lib, "4D530001", {"60 FPS", "Brighter"},
                               cache, restore=True, wait=False)
    assert r3.applied == 2, ev3
    restored = dict(srv3.writes)
    assert restored[0x82012340] == GAME_A[0x12340:0x12344]
    assert restored[0x82030010] == GAME_A[0x30010:0x30014]


def _patched(img: bytearray) -> bytearray:
    out = bytearray(img)
    out[0x12340:0x12344] = bytes.fromhex("39600001")
    out[0x30010:0x30014] = struct.pack(">f", 1.5)
    out[0x30020] = 0xFF
    return out


# ------------------------------------------------- scenarios from the independent review
def _msgs(ev):
    return " | ".join(m for k, m in ev if isinstance(m, str))


def test_game_running_at_start_is_identified_after_relaunch(lib, tmp_path):
    """Start pressed while the (never-identified) game runs; the user quits and relaunches as told."""
    cache = BuildCache(tmp_path / "b.json")
    tl = [(0, resolved(GAME_A)), (1.0, DASH, "Aurora.xex")] + boot(GAME_A, 1.5)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=6)
    assert "Identified" in _msgs(ev), _msgs(ev)


def test_relaunch_without_a_dashboard_module_in_between(lib, tmp_path):
    """No title module between runs (stock dashboard): identified, quit, relaunched, patched."""
    cache = BuildCache(tmp_path / "b.json")
    tl = [(0, None)] + boot(GAME_A, 0.3) + [(4.0, None)] + boot(GAME_A, 4.5)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=9)
    assert srv.writes == [(0x82012340, bytes.fromhex("39600001"))], _msgs(ev)


def test_dashboard_is_never_scanned(lib, tmp_path):
    cache = BuildCache(tmp_path / "b.json")
    aurora = r"\Device\Harddisk0\Partition1\Apps\Aurora\default.xex"     # recognised by its folder
    tl = [(0, None), (0.2, DASH, "default.xex", aurora)] + boot(GAME_A, 0.5) + \
         [(4.0, DASH, "default.xex", aurora)] + boot(GAME_A, 8.0)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=12)
    assert _msgs(ev).count("MB of code") == 1, _msgs(ev)
    assert r.applied == 1


def test_god_path_names_another_game(lib, tmp_path):
    cache = BuildCache(tmp_path / "b.json")
    god = r"\Device\Harddisk0\Partition1\Content\0000000000000000\4D530002\00007000\ABCDEF"
    tl = [(0, DASH, "Aurora.xex"), (0.3, resolved(OTHER), "default.xex", god)]
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=4)
    assert "That's Other Game, not Test Game" in _msgs(ev) and "MB of code" not in _msgs(ev)


def test_new_code_patch_after_identification_is_not_written_blind(lib, tmp_path):
    """Build identified with a library lacking a code patch; the library gains it later."""
    cache = BuildCache(tmp_path / "b.json")
    files = lib_files()
    head, rest = files["4D530001 - Test Game.patch.toml"].split("[[patch]]", 1)
    files1 = dict(files)
    files1["4D530001 - Test Game.patch.toml"] = head + "[[patch]]" + rest.split("[[patch]]")[1]
    lib1 = load_library(write_patches(tmp_path / "lib1", files1))
    run_engine([(0, DASH, "Aurora.xex")] + boot(GAME_A, 0.3), lib1, "4D530001", {"Brighter"}, cache, stop_after=3)
    odd = bytearray(GAME_A)
    odd[0x12340:0x12344] = b"\x60\x00\x00\x00"
    r, ev, srv = run_engine([(0, DASH, "Aurora.xex")] + boot(odd, 0.3), lib, "4D530001", {"60 FPS"}, cache,
                            stop_after=4)
    assert all(a != 0x82012340 for a, _ in srv.writes), _msgs(ev)


def test_patch_now_waits_for_the_loader(lib, tmp_path):
    cache = BuildCache(tmp_path / "b.json")
    run_engine([(0, DASH, "Aurora.xex")] + boot(GAME_A, 0.3), lib, "4D530001", {"60 FPS"}, cache, stop_after=3)
    r, ev, srv = run_engine([(0, GAME_A)], lib, "4D530001", {"60 FPS"}, cache, wait=False)   # raw thunks
    assert not srv.writes and "still loading" in _msgs(ev)


def test_losing_the_console_while_watching_is_reported(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(eng_mod, "WATCH_SECONDS", 5)
    cache = BuildCache(tmp_path / "b.json")
    run_engine([(0, DASH, "Aurora.xex")] + boot(GAME_A, 0.3), lib, "4D530001", {"60 FPS"}, cache, stop_after=3)
    srv = FakeXbdm([(0, DASH, "Aurora.xex")] + boot(GAME_A, 0.3), port=0)
    ev = []
    e = LibraryEngine(lambda k, m: ev.append((k, m)), lib, cache)
    game = to_game(lib.by_id["4D530001"])
    mods = [m for m in game.mods if m.name == "60 FPS"]
    xb.XbdmClient.__init__.__defaults__ = (srv.port, 1.0)
    orig_read = xb.XbdmClient.read

    def read(self, a, n):
        if any("Watching" in str(m) for _, m in ev):
            raise OSError("console froze")
        return orig_read(self, a, n)
    monkeypatch.setattr(xb.XbdmClient, "read", read)
    threading.Timer(4, e.stop).start()
    try:
        r = e.run("127.0.0.1", game, "*", mods)
    finally:
        xb.XbdmClient.__init__.__defaults__ = (730, 5.0)
        srv.close()
    assert "(connection lost)" in r.failed and "All done" not in _msgs(ev)


def test_same_build_in_two_files_is_one_version(tmp_path):
    h = xenia_hash(GAME_A)
    body = '[[patch]]\n    name = "{n}"\n    [[patch.be8]]\n        address = 0x82030020\n        value = 1\n'
    lib = load_library(write_patches(tmp_path, {
        "4D530003 - Twin (TU10).patch.toml": f'title_name = "Twin"\ntitle_id = "4D530003"\nhash = "{h}"\n'
                                             + body.format(n="A"),
        "4D530003 - Twin (TU18).patch.toml": f'title_name = "Twin"\ntitle_id = "4D530003"\nhash = "{h}"\n'
                                             + body.format(n="A") + body.format(n="B"),
    }))
    t = lib.by_id["4D530003"]
    assert len(t.builds) == 1 and t.builds[0].label == "TU10; TU18"
    g = to_game(t)
    assert [m.name for m in g.mods] == ["A", "A (2)", "B"] and len(g.versions) == 1


AUR = "\\Device\\Harddisk0\\Partition1\\Aurora\\Aurora.xex"


def test_base_visible_long_before_title_update(lib, tmp_path):
    cache = BuildCache(tmp_path / "b.json")
    tl = [(0, None), (0.3, GAME_A), (2.5, GAME_A_TU), (2.8, resolved(GAME_A_TU)),
          (6.0, None)] + boot(GAME_A_TU, 6.5)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=12)
    assert srv.writes == [(0x82012350, bytes.fromhex("39600001"))], srv.writes

def test_works_without_xbeinfo(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(xb.XbdmClient, "running_path", lambda self: None)
    cache = BuildCache(tmp_path / "b.json")
    tl = [(0, resolved(GAME_A)), (1.0, DASH, "Aurora.xex")] + boot(GAME_A, 1.5) \
         + [(4.5, DASH, "Aurora.xex")] + boot(GAME_A, 5.0)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=10)
    assert dict(srv.writes).get(0x82012340) == bytes.fromhex("39600001")

def test_stale_xbeinfo_path_does_not_hide_the_game(lib, tmp_path):
    """The game module is listed while XBDM still reports the dashboard as the running title."""
    cache = BuildCache(tmp_path / "b.json")
    tl = [(0, DASH, "Aurora.xex", AUR), (0.5, GAME_A, "default.xex", AUR), (0.7, resolved(GAME_A)),
          (4.0, DASH, "Aurora.xex", AUR), (4.5, GAME_A, "default.xex", AUR), (4.7, resolved(GAME_A))]
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=9)
    assert srv.writes, "game misclassified as dashboard for the whole run"

def test_other_library_game_then_selected_one(lib, tmp_path):
    cache = BuildCache(tmp_path / "b.json")
    tl = [(0, DASH, "Aurora.xex", AUR)] + boot(OTHER, 0.3) + [(3.0, DASH, "Aurora.xex", AUR)] + boot(GAME_A, 3.5) \
         + [(6.5, DASH, "Aurora.xex", AUR)] + boot(GAME_A, 7.0)
    r, ev, srv = run_engine(tl, lib, "4D530001", {"60 FPS"}, cache, stop_after=12)
    assert all(a != 0x82011000 for a, _ in srv.writes)
    assert dict(srv.writes).get(0x82012340) == bytes.fromhex("39600001")
