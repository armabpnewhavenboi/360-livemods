"""Minimal XBDM (Xbox Debug Monitor) client.

XBDM is a debug plugin that RGH/JTAG consoles commonly load through DashLaunch.
It listens on TCP port 730 and speaks a simple line-based text protocol.
Commands used: dbgname (connection test), getmem / getmemex (read), setmem (write) and modules
(which executables are loaded).
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass

XBDM_PORT = 730


class XbdmError(Exception):
    """Raised when the console answers with an error or the connection drops."""


@dataclass(frozen=True)
class Module:
    name: str
    base: int
    size: int
    timestamp: int
    checksum: int


_KV = re.compile(r'(\w+)=("[^"]*"|\S+)')


class XbdmClient:
    def __init__(self, host: str, port: int = XBDM_PORT, timeout: float = 5.0):
        self.host = host
        self.port = port
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._file = self._sock.makefile("rb")
        hello = self._readline()
        if not hello.startswith("201"):
            self.close()
            raise XbdmError(f"Unexpected greeting from console: {hello!r}")

    # ------------------------------------------------------------------ basics
    def _readline(self) -> str:
        line = self._file.readline()
        if not line:
            raise XbdmError("Console closed the connection")
        return line.decode("latin-1").rstrip("\r\n")

    def command(self, cmd: str) -> str:
        self._sock.sendall((cmd + "\r\n").encode("latin-1"))
        return self._readline()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- commands
    def console_name(self) -> str:
        r = self.command("dbgname")
        if r.startswith("200"):
            return r.split("-", 1)[-1].strip() or "Xbox 360"
        return "Xbox 360"

    def read(self, address: int, length: int) -> bytes | None:
        """Read `length` bytes. Returns None if any byte is unreadable (memory not mapped yet)."""
        r = self.command(f"getmem addr=0x{address:08x} length={length}")
        if not (r.startswith("202") or r.startswith("211")):
            raise XbdmError(f"getmem failed: {r}")
        data = []
        while True:
            line = self._readline()
            if line == ".":
                break
            data.append(line.strip())
        hexdata = "".join(data)[: length * 2]
        if len(hexdata) < length * 2 or "?" in hexdata:
            return None
        return bytes.fromhex(hexdata)

    def read_fast(self, address: int, length: int) -> bytes | None:
        """Binary read (getmemex): several times faster than getmem for large blocks.
        The data comes in chunks of up to 1024 bytes, each preceded by a 2-byte little-endian
        header holding the chunk length, with the top bit set when that memory isn't readable.
        Callers should check this against read() once per console (see identify.FastReader)."""
        r = self.command(f"getmemex addr=0x{address:08x} length=0x{length:x}")
        if not r.startswith("203"):
            raise XbdmError(f"getmemex failed: {r}")
        out = bytearray()
        readable = True
        while len(out) < length:
            hdr = self._file.read(2)
            if len(hdr) != 2:
                raise XbdmError("Console closed the connection")
            h = hdr[0] | (hdr[1] << 8)
            n = h & 0x7FFF
            if n == 0 or n > 1024 or n > length - len(out):
                raise XbdmError(f"getmemex: unexpected chunk header {h:#06x}")
            chunk = self._file.read(n)
            if len(chunk) != n:
                raise XbdmError("Console closed the connection")
            if h & 0x8000:
                readable = False
            out += chunk
        return bytes(out) if readable else None

    def modules(self) -> list[Module]:
        """Executables and DLLs currently loaded (kernel, dashboard or game, plugins)."""
        r = self.command("modules")
        if not r.startswith("202"):
            raise XbdmError(f"modules failed: {r}")
        mods = []
        while True:
            line = self._readline()
            if line == ".":
                break
            kv = {k.lower(): v.strip('"') for k, v in _KV.findall(line)}
            try:
                mods.append(Module(kv.get("name", ""), int(kv["base"], 16), int(kv["size"], 16),
                                   int(kv.get("timestamp", "0"), 16), int(kv.get("check", "0"), 16)))
            except (KeyError, ValueError):
                continue
        return mods

    def running_path(self) -> str | None:
        """Path of the running title's executable (from `xbeinfo running`), or None if this XBDM
        doesn't say. Used only as a hint, e.g. to recognise the dashboard."""
        r = self.command("xbeinfo running")
        text = r
        if r.startswith("202"):
            while True:
                line = self._readline()
                if line == ".":
                    break
                text += " " + line
        elif not r.startswith("200"):
            return None
        m = re.search(r'name="([^"]*)"', text)
        return m.group(1) if m else None

    def write(self, address: int, data: bytes) -> None:
        r = self.command(f"setmem addr=0x{address:08x} data={data.hex()}")
        if not r.startswith("200"):
            raise XbdmError(f"setmem failed: {r}")


def test_connection(host: str, timeout: float = 4.0) -> str:
    """Connect, ask for the console name, disconnect. Raises on failure."""
    with XbdmClient(host, timeout=timeout) as x:
        return x.console_name()
