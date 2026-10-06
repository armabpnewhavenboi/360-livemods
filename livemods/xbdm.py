"""Minimal XBDM (Xbox Debug Monitor) client.

XBDM is a debug plugin that RGH/JTAG consoles commonly load through DashLaunch.
It listens on TCP port 730 and speaks a simple line-based text protocol.
We only need three commands: dbgname (connection test), getmem and setmem.
"""
from __future__ import annotations

import socket

XBDM_PORT = 730


class XbdmError(Exception):
    """Raised when the console answers with an error or the connection drops."""


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

    def write(self, address: int, data: bytes) -> None:
        r = self.command(f"setmem addr=0x{address:08x} data={data.hex()}")
        if not r.startswith("200"):
            raise XbdmError(f"setmem failed: {r}")


def test_connection(host: str, timeout: float = 4.0) -> str:
    """Connect, ask for the console name, disconnect. Raises on failure."""
    with XbdmClient(host, timeout=timeout) as x:
        return x.console_name()
