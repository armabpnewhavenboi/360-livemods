"""A tiny fake XBDM server for testing without a console.

It serves a memory image (a raw dump starting at a base address) and can switch images on a
timeline, which lets tests reproduce a boot sequence such as "nothing -> base game -> title update".

    python tests/fake_xbdm.py image.bin            # serve one image on 127.0.0.1:730
"""
from __future__ import annotations

import socket
import sys
import threading
import time


class FakeXbdm:
    def __init__(self, timeline: list[tuple[float, bytes | None]], base: int = 0x82000000,
                 host: str = "127.0.0.1", port: int = 730):
        """timeline: list of (seconds_after_start, image_or_None). None = game not loaded."""
        self.timeline = sorted(timeline, key=lambda t: t[0])
        self.base = base
        self.start = time.time()
        self.writes: list[tuple[int, bytes]] = []
        self._mem: bytearray | None = None
        self._stage = -1
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _memory(self) -> bytearray | None:
        t = time.time() - self.start
        stage = max((i for i, (at, _) in enumerate(self.timeline) if at <= t), default=-1)
        if stage != self._stage:
            self._stage = stage
            img = self.timeline[stage][1] if stage >= 0 else None
            self._mem = bytearray(img) if img is not None else None
        return self._mem

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket):
        f = c.makefile("rb")
        c.sendall(b"201- connected\r\n")
        try:
            for raw in f:
                line = raw.decode().strip()
                if line == "dbgname":
                    c.sendall(b"200- FakeConsole\r\n")
                    continue
                args = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
                addr = int(args["addr"], 16)
                off = addr - self.base
                mem = self._memory()
                if line.startswith("getmem"):
                    n = int(args["length"])
                    if mem is None or off < 0 or off + n > len(mem):
                        data = "??" * n
                    else:
                        data = mem[off:off + n].hex().upper()
                    c.sendall(b"202- memory data follows\r\n" + data.encode() + b"\r\n.\r\n")
                elif line.startswith("setmem"):
                    val = bytes.fromhex(args["data"])
                    if mem is not None:
                        mem[off:off + len(val)] = val
                    self.writes.append((addr, val))
                    c.sendall(f"200- set {len(val)} bytes\r\n".encode())
        except (OSError, ValueError, KeyError):
            pass
        finally:
            c.close()

    def close(self):
        self.sock.close()


if __name__ == "__main__":
    srv = FakeXbdm([(0, open(sys.argv[1], "rb").read())])
    print("fake XBDM on 127.0.0.1:730 - Ctrl+C to stop")
    while True:
        time.sleep(1)
