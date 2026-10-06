"""Working out exactly which game build is running on the console.

Xenia names every game build by a hash of its code: XXH3-64 over the executable's code pages, from
the first code page to the last, taken after the loader has pointed the game's calls into the
system (its "import thunks") at Xenia's own stubs. We read the same pages from the console, rewrite
the console's resolved thunks into Xenia's stub form, and hash. When the result equals a library
file's hash, that file was made for exactly this code - which is what makes it safe to apply.

Reading ~20 MB over the network takes a while, so the result is remembered per build (keyed by the
executable's PE header, which differs between builds) in builds.json. After the first time, a game
is recognised the moment it loads.
"""
from __future__ import annotations

import hashlib
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path

import xxhash

from .config import app_dir
from .xbdm import Module, XbdmClient, XbdmError

XENIA_THUNK = bytes.fromhex("440000424E8000206000000060000000")   # sc 2; blr; nop; nop
_THUNK_TAIL = bytes.fromhex("7D6903A64E800420")                      # mtctr r11; bctr
PAGE_SIZES = (0x10000, 0x1000)
SECTION_EXEC = 0x20 | 0x20000000                                     # code / executable
READ_CHUNK = 0x10000


class IdentifyError(Exception):
    pass


# ------------------------------------------------------------------------------ modules
def title_module(mods: list[Module]) -> Module | None:
    """The running game's main executable: the lowest-loaded .xex in the title address range."""
    cands = [m for m in mods if 0x82000000 <= m.base < 0x90000000
             and m.name.lower().endswith((".xex", ".exe"))]
    return min(cands, key=lambda m: m.base) if cands else None


def header_size(head: bytes) -> int:
    """Size of the PE headers at the start of the image (capped to what we read)."""
    try:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe:pe + 4] != b"PE\0\0":
            return min(len(head), 0x400)
        size = struct.unpack_from("<I", head, pe + 24 + 60)[0]
        return max(0x200, min(size, len(head)))
    except struct.error:
        return min(len(head), 0x400)


def fingerprint(module: Module, head: bytes) -> str:
    """Cheap identity for a loaded build: its PE headers (timestamp, section sizes...) + size."""
    h = hashlib.sha1(head[:header_size(head)] + module.size.to_bytes(4, "big"))
    return h.hexdigest()[:24]


def code_sections(head: bytes) -> list[tuple[int, int]]:
    """(start, end) image offsets of the executable sections, from the PE section table."""
    if head[:2] != b"MZ":
        raise IdentifyError("The game's executable header isn't readable yet.")
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if head[pe:pe + 4] != b"PE\0\0":
        raise IdentifyError("The game's executable header isn't valid.")
    count = struct.unpack_from("<H", head, pe + 6)[0]
    opt = struct.unpack_from("<H", head, pe + 20)[0]
    table = pe + 24 + opt
    out = []
    for i in range(count):
        o = table + i * 40
        if o + 40 > len(head):
            break
        vsize, va = struct.unpack_from("<II", head, o + 8)
        ch = struct.unpack_from("<I", head, o + 36)[0]
        if ch & SECTION_EXEC and vsize:
            out.append((va, va + vsize))
    if not out:
        raise IdentifyError("The game's executable has no code sections.")
    return out


def code_range(sections: list[tuple[int, int]], page: int) -> tuple[int, int]:
    start = min(s for s, _ in sections) // page * page
    end = (max(e for _, e in sections) + page - 1) // page * page
    return start, end


def normalize_thunks(buf: bytearray) -> list[int]:
    """Rewrite import thunks into the form Xenia hashes. Returns their offsets in buf.
    On a console a resolved thunk is   lis r11,hi ; addi r11,r11,lo ; mtctr r11 ; bctr
    (hi pointing into the kernel/system, 0x8000xxxx-0x81FFxxxx); before the loader resolves it, it
    is the raw import record  01xxxxxx 02xxxxxx mtctr r11 ; bctr."""
    found = []
    i = buf.find(_THUNK_TAIL, 8)
    while i != -1:
        o = i - 8
        if o % 4 == 0:
            w0, w1 = buf[o:o + 4], buf[o + 4:o + 8]
            resolved = w0[:2] == b"\x3d\x60" and w1[:2] == b"\x39\x6b" and 0x80 <= w0[2] <= 0x81
            raw = w0[0] == 0x01 and w1[0] == 0x02
            if resolved or raw:
                buf[o:o + 16] = XENIA_THUNK
                found.append(o)
        i = buf.find(_THUNK_TAIL, i + 1)
    return found


def thunk_state(data: bytes | None) -> str:
    """'resolved', 'raw' or 'unknown' for the 16 bytes of a thunk read from the console."""
    if not data or len(data) < 16 or data[8:16] != _THUNK_TAIL:
        return "unknown"
    if data[:2] == b"\x3d\x60" and data[4:6] == b"\x39\x6b":
        return "resolved"
    if data[0] == 0x01 and data[4] == 0x02:
        return "raw"
    return "unknown"


@dataclass
class Scan:
    """Result of reading and hashing a build's code."""
    hashes: dict[int, int]              # page size -> Xenia hash
    code: dict[int, tuple[int, int]]    # page size -> (start, end) addresses that were hashed
    image: bytes                        # the code as read (before thunk rewriting), start = code[0x10000][0]
    image_start: int
    thunks: list[int]                   # thunk addresses

    def original(self, address: int, length: int) -> bytes | None:
        o = address - self.image_start
        if o < 0 or o + length > len(self.image):
            return None
        return self.image[o:o + length]


def hash_image(module_base: int, head: bytes, read_range) -> Scan:
    """read_range(addr, length) -> bytes for the whole span. Computes both page-size variants."""
    secs = code_sections(head)
    ranges = {p: code_range(secs, p) for p in PAGE_SIZES}
    lo = min(r[0] for r in ranges.values())
    hi = max(r[1] for r in ranges.values())
    raw = read_range(module_base + lo, hi - lo)
    buf = bytearray(raw)
    thunks = normalize_thunks(buf)
    hashes = {p: xxhash.xxh3_64_intdigest(bytes(buf[s - lo:e - lo])) for p, (s, e) in ranges.items()}
    return Scan(hashes, {p: (module_base + s, module_base + e) for p, (s, e) in ranges.items()},
                bytes(raw), module_base + lo, [module_base + lo + t for t in thunks])


# --------------------------------------------------------------------------- fast reads
class FastReader:
    """Reads big blocks, using the binary getmemex when this console's XBDM supports it (checked
    against the plain text read once), otherwise getmem."""

    def __init__(self, host: str):
        self.host = host
        self.fast: bool | None = None

    def _probe(self, x: XbdmClient, addr: int) -> bool:
        try:
            with XbdmClient(self.host) as t:
                fast = t.read_fast(addr, 0x1000)
            return fast is not None and fast == x.read(addr, 0x1000)
        except (OSError, XbdmError):
            return False

    def read(self, x: XbdmClient, addr: int, length: int, progress=None, stop=None) -> bytes:
        if self.fast is None:
            self.fast = self._probe(x, addr)
        out = bytearray()
        conn = XbdmClient(self.host) if self.fast else None
        try:
            pos = addr
            while pos < addr + length:
                if stop and stop():
                    raise IdentifyError("Stopped.")
                n = min(READ_CHUNK, addr + length - pos)
                data = None
                if conn is not None:
                    try:
                        data = conn.read_fast(pos, n)
                    except (OSError, XbdmError):
                        conn.close()
                        conn = None
                        self.fast = False
                if data is None:
                    data = b"".join(_slow(x, pos + k, min(0x4000, n - k)) for k in range(0, n, 0x4000))
                out += data
                pos += n
                if progress:
                    progress(len(out) / length)
        finally:
            if conn is not None:
                conn.close()
        return bytes(out)


def _slow(x: XbdmClient, addr: int, n: int) -> bytes:
    data = x.read(addr, n)
    if data is None:
        raise IdentifyError(f"Memory at 0x{addr:08X} isn't readable.")
    return data


# ------------------------------------------------------------------------------- cache
class BuildCache:
    """Remembers identified builds: fingerprint -> hashes, code range, a thunk to check the
    loader with, and the original bytes at every patch address in the code."""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "builds.json")
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data.get("builds"), dict):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            self.data = {"builds": {}}

    def get(self, fp: str) -> dict | None:
        return self.data["builds"].get(fp)

    def put(self, fp: str, record: dict) -> None:
        record["seen"] = time.time()
        self.data["builds"][fp] = record
        self.save()

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


def record_from_scan(scan: Scan, module: Module) -> dict:
    return {
        "hashes": {str(p): f"{h:016X}" for p, h in scan.hashes.items()},
        "code": [min(s for s, _ in scan.code.values()), max(e for _, e in scan.code.values())],
        "thunk": scan.thunks[0] if scan.thunks else None,
        "module": [module.name, module.base, module.size],
        "originals": {},
        "restore": {},
    }
