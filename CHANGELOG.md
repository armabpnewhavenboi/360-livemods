# Changelog

## 1.1.0
- Built-in updater: the app checks GitHub for new releases at startup (or via "Check for updates")
  and installs them in one click. Portable copies get a link to the download page instead.
- Game banners: each game can show header art behind its name (Fable II included).
  Drop `<game id>_banner.jpg` or `.png` into `%APPDATA%\360LiveMods\games` to use your own.
- Fixed the mods list not scrolling: only the first few mods were reachable until the window was resized.

## 1.0.0
- First release.
- Fable II: remove bloom, motion blur, depth of field and distance fog; 60 FPS; native 720p
  (base game only); experimental 30 Hz tick rate (base game only).
- Supports Fable II's base game and title update, with automatic detection of the wrong version.
- Fixes the community fog patch crashing the title update.
