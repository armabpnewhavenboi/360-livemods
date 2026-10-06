"""Small JSON settings store in the user's app-data folder."""
from __future__ import annotations

import json
import os
from pathlib import Path


def app_dir() -> Path:
    root = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    p = Path(root) / "360LiveMods"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Settings:
    def __init__(self):
        self.path = app_dir() / "settings.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass

    # convenience accessors
    @property
    def console_ip(self) -> str:
        return self.data.get("console_ip", "")

    @console_ip.setter
    def console_ip(self, v: str) -> None:
        self.data["console_ip"] = v.strip()

    def game(self, game_id: str) -> dict:
        return self.data.setdefault("games", {}).setdefault(game_id, {})
