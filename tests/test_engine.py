"""Engine tests against a fake console.

Needs memory images of the game (not shipped - they are copyrighted game code):
    LIVEMODS_BASE_IMAGE=<path to base-game memory image>
    LIVEMODS_TU_IMAGE=<path to title-update memory dump>
Run:  python -m pytest tests -q      (tests are skipped when the images are missing)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_xbdm import FakeXbdm  # noqa: E402
from livemods import engine as eng_mod  # noqa: E402
from livemods.engine import Engine  # noqa: E402
from livemods.games import load_all  # noqa: E402

BASE = os.environ.get("LIVEMODS_BASE_IMAGE")
TU = os.environ.get("LIVEMODS_TU_IMAGE")
need_images = pytest.mark.skipif(not (BASE and TU), reason="game memory images not provided")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(eng_mod, "WATCH_SECONDS", 2)


@pytest.fixture
def fable2():
    games, errors = load_all()
    assert not errors
    return next(g for g in games if g.id == "fable2")


def run(timeline, game, version, mod_ids=None, **kw):
    srv = FakeXbdm(timeline, port=0)
    events = []
    e = Engine(lambda k, m: events.append((k, m)))
    mods = [m for m in game.mods if (m.id in mod_ids if mod_ids else m.default)]
    # point the client at the fake server's port
    import livemods.xbdm as x
    old = x.XBDM_PORT
    x.XbdmClient.__init__.__defaults__ = (srv.port, 5.0)
    try:
        r = e.run("127.0.0.1", game, version, mods, **kw)
    finally:
        x.XbdmClient.__init__.__defaults__ = (old, 5.0)
        srv.close()
    return r, events, srv


def test_definitions_load(fable2):
    assert {v.id for v in fable2.versions} == {"base", "tu"}
    res = next(m for m in fable2.mods if m.id == "res720")
    assert res.available_for("base") and not res.available_for("tu")


@need_images
def test_base_game_all_defaults(fable2):
    base = Path(BASE).read_bytes()
    r, ev, srv = run([(0, None), (0.5, base)], fable2, "base")
    assert not r.failed and r.applied == 6, ev
    assert len(srv.writes) == 11


@need_images
def test_title_update_skips_720p(fable2):
    tu = Path(TU).read_bytes()
    r, ev, srv = run([(0, None), (0.5, tu)], fable2, "tu")
    assert not r.failed and r.applied == 5, ev       # 720p unavailable on the update
    assert len(srv.writes) == 9


@need_images
def test_tu_boot_sequence_never_touches_base_code(fable2):
    base, tu = Path(BASE).read_bytes(), Path(TU).read_bytes()
    r, ev, srv = run([(0, None), (0.5, base), (1.5, tu)], fable2, "tu")
    assert not r.failed and r.applied == 5, ev
    tu_addrs = {p.address for m in fable2.mods for p in m.patches.get("tu", [])}
    assert all(a in tu_addrs for a, _ in srv.writes)


@need_images
def test_wrong_version_selected_is_reported(fable2):
    tu = Path(TU).read_bytes()
    r, ev, srv = run([(0, tu)], fable2, "base")
    assert r.wrong_version == "Title Update"
    assert srv.writes == []


@need_images
def test_restore(fable2):
    base = Path(BASE).read_bytes()
    srv = FakeXbdm([(0, base)], port=0)
    import livemods.xbdm as x
    x.XbdmClient.__init__.__defaults__ = (srv.port, 5.0)
    try:
        mods = [m for m in fable2.mods if m.default]
        e = Engine(lambda k, m: None)
        e.run("127.0.0.1", fable2, "base", mods, wait=False)
        r = e.run("127.0.0.1", fable2, "base", mods, restore=True, wait=False)
        assert not r.failed
        for m in mods:
            for p in m.patches["base"]:
                off = p.address - 0x82000000
                assert bytes(srv._mem[off:off + len(p.original)]) == p.original
    finally:
        x.XbdmClient.__init__.__defaults__ = (730, 5.0)
        srv.close()


def test_every_definition_is_valid():
    """Loads every bundled game file and checks the basics contributors tend to get wrong."""
    games, errors = load_all()
    assert not errors, errors
    for g in games:
        ids = [m.id for m in g.mods]
        assert len(ids) == len(set(ids)), f"{g.id}: duplicate mod ids"
        vids = {v.id for v in g.versions}
        for v in g.versions:
            assert all(len(x) == 4 for x in v.detect_values), f"{g.id}/{v.id}: detect values must be 4 bytes"
        for m in g.mods:
            assert m.status in ("tested", "experimental", "unstable"), f"{g.id}/{m.id}: bad status"
            assert set(m.unavailable) <= vids, f"{g.id}/{m.id}: unavailable lists unknown version"
            assert any(m.available_for(v) for v in vids), f"{g.id}/{m.id}: not available for any version"
