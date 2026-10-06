"""Self-updater: checks GitHub Releases for a newer version and installs it.

- Installed copies (from the Setup.exe) download the new installer and run it silently;
  the installer closes nothing important, replaces the files and relaunches the app.
- Portable copies and source checkouts just open the release page.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

REPO = "armabpnewhavenboi/360-livemods"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
SETUP_RE = re.compile(r"^360LiveMods-Setup-[\w.\-]+\.exe$", re.I)
UA = {"User-Agent": f"360LiveMods/{__version__}", "Accept": "application/vnd.github+json"}


@dataclass
class Release:
    version: str
    page_url: str
    setup_url: str | None
    setup_size: int
    notes: str


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def newest(releases: list[dict]) -> Release | None:
    """Pick the highest-versioned, non-draft release."""
    best = None
    for r in releases:
        if r.get("draft"):
            continue
        tag = r.get("tag_name", "")
        if not re.match(r"^v?\d+(\.\d+)*", tag):
            continue
        if best is None or parse_version(tag) > parse_version(best["tag_name"]):
            best = r
    if not best:
        return None
    asset = next((a for a in best.get("assets", []) if SETUP_RE.match(a.get("name", ""))), None)
    return Release(
        version=best["tag_name"].lstrip("v"),
        page_url=best.get("html_url", f"https://github.com/{REPO}/releases"),
        setup_url=asset["browser_download_url"] if asset else None,
        setup_size=int(asset.get("size", 0)) if asset else 0,
        notes=best.get("body") or "",
    )


def check(timeout: float = 8.0) -> Release | None:
    """Return the newest release if it is newer than this copy, else None. Raises on network errors."""
    req = urllib.request.Request(API, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rel = newest(json.load(resp))
    if rel and parse_version(rel.version) > parse_version(__version__):
        return rel
    return None


def is_installed_copy() -> bool:
    """True when running from the Setup.exe install (Inno Setup leaves an uninstaller next to the exe)."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return False
    app_dir = Path(sys.executable).resolve().parent
    return any(app_dir.glob("unins*.exe"))


def download(rel: Release, progress: Callable[[int, int], None]) -> Path:
    if not rel.setup_url or not rel.setup_url.startswith("https://github.com/"):
        raise RuntimeError("This release has no installer to download.")
    dest = Path(tempfile.gettempdir()) / f"360LiveMods-Setup-{rel.version}.exe"
    req = urllib.request.Request(rel.setup_url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or rel.setup_size or 0)
        done = 0
        while chunk := resp.read(256 * 1024):
            out.write(chunk)
            done += len(chunk)
            progress(done, total)
    if rel.setup_size and dest.stat().st_size != rel.setup_size:
        dest.unlink(missing_ok=True)
        raise RuntimeError("The download was incomplete. Please try again.")
    return dest


def launch_installer(path: Path) -> None:
    """Run the installer silently; it replaces the app and starts the new version."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=flags, close_fds=True, cwd=os.path.dirname(path))
