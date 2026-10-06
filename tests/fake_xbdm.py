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
    def __init__(self, timeline: list[tuple], base: int = 0x82000000,
                 host: str = "127.0.0.1", port: int = 730, getmemex: bool = True):
        """timeline: list of (seconds_after_start, image_or_None[, module_name]).
        None = nothing loaded at the title address. module_name defaults to default.xex."""
        self.timeline = sorted(timeline, key=lambda t: t[0])
        self.base = base
        self.getmemex = getmemex
        self.reads = 0
        self.start = time.time()
        self.writes: list[tuple[int, bytes]] = []
        self._mem: bytearray | None = None
        self._stage = -1
        self._name = "default.xex"
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _memory(self) -> bytearray | None:
        t = time.time() - self.start
        stage = max((i for i, e in enumerate(self.timeline) if e[0] <= t), default=-1)
        if stage != self._stage:
            self._stage = stage
            img = self.timeline[stage][1] if stage >= 0 else None
            self._mem = bytearray(img) if img is not None else None
            self._name = (self.timeline[stage][2] if stage >= 0 and len(self.timeline[stage]) > 2
                          else "default.xex")
        return self._mem

    def _modules(self) -> bytes:
        mem = self._memory()
        lines = ['name="xboxkrnl.exe" base=0x80040000 size=0x00180000 check=0x0 timestamp=0x0',
                 'name="xam.xex" base=0x81500000 size=0x00800000 check=0x0 timestamp=0x0']
        if mem is not None:
            lines.append(f'name="{self._name}" base=0x{self.base:08x} size=0x{len(mem):08x} '
                         f'check=0x00000000 timestamp=0x00000000 pdata=0x0 psize=0x0 thread=0x0 osize=0x0')
        lines.append('name="xbdm.xex" base=0x91f00000 size=0x00080000 check=0x0 timestamp=0x0')
        return ("202- multiline response follows\r\n" + "\r\n".join(lines) + "\r\n.\r\n").encode()

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
                if line == "modules":
                    c.sendall(self._modules())
                    continue
                if line == "xbeinfo running":
                    mem = self._memory()
                    st = self.timeline[self._stage] if self._stage >= 0 else ()
                    path = st[3] if len(st) > 3 else f"\\Device\\Harddisk0\\Partition1\\Games\\{self._name}"
                    c.sendall(("202- multiline response follows\r\ntimestamp=0x00000000 checksum=0x00000000\r\n"
                               f'name="{path}"\r\n.\r\n').encode() if mem is not None else b"402- no title\r\n")
                    continue
                args = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
                addr = int(args["addr"], 16)
                off = addr - self.base
                mem = self._memory()
                if line.startswith("getmemex"):
                    if not self.getmemex:
                        c.sendall(b"407- unknown command\r\n")
                        continue
                    n = int(args["length"], 16)
                    out = [b"203- binary response follows\r\n"]
                    for k in range(0, n, 1024):
                        m = min(1024, n - k)
                        o = off + k
                        if mem is None or o < 0 or o + m > len(mem):
                            out.append((m | 0x8000).to_bytes(2, "little") + bytes(m))
                        else:
                            out.append(m.to_bytes(2, "little") + bytes(mem[o:o + m]))
                    self.reads += 1
                    c.sendall(b"".join(out))
                elif line.startswith("getmem"):
                    n = int(args["length"])
                    if mem is None or off < 0 or off + n > len(mem):
                        data = "??" * n
                    else:
                        data = mem[off:off + n].hex().upper()
                    self.reads += 1
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
