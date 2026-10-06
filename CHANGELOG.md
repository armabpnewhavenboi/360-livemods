# Changelog

## 1.5.0
- Removed the community library. Xenia's emulator patches crashed real consoles across the board
  (about 20 games tried), so 360 LiveMods goes back to hand-made, hardware-tested definitions only.
  The library's downloaded files in `%APPDATA%\360LiveMods\library` are no longer used and can be deleted.
- Kept from 1.4.0: the Fable II extras (ultrawide, website items, Collector's Edition content,
  updated tick rate - all still experimental/unstable), window text that wraps at small sizes, and
  long game names that fit the header.

## 1.4.0
- Community library (removed again in 1.5.0).
- **Community library: 300+ games.** The app downloads the Xenia community patch library
  (github.com/xenia-canary/game-patches) and offers its patches for real consoles. Search by name
  or title ID in the sidebar.
- **Exact version matching.** The first time you play a version of a library game, the app reads its
  code over XBDM and computes the same build hash Xenia uses (about a minute, once). Patches are only
  ever applied to the exact build they were made for; after that, the game is recognised the moment
  it loads and patched before it runs. Title updates loading on top of the base game are handled.
- Every patch site is checked before writing, patches pointing outside the game's memory are
  skipped, and patches that only work around emulator problems are flagged and need confirming.
- Fable II: Unlock website items, Unlock Collector's Edition content, 21:9 and 32:9 ultrawide
  (experimental, base game and title update); the 30 Hz tick rate is updated with the community's
  physics and stairs fixes and now also covers the title update (still marked unstable).
- Long window text now wraps at small window sizes instead of being cut off.
- Command line: `--update-library`, `--library [search]`, and library games by title ID.

## 1.3.0
- Fable II: Performance mode (anti-aliasing off) and the lamp/torch and screen-space shadow mods
  are now marked tested on hardware; clearer descriptions for them and for V-Sync.

## 1.2.0
- Live frame-rate readout: while the game runs with mods active, the status panel shows the
  current FPS plus the session average and low (reads the game's own once-per-frame counter).
- Fable II: new mods for both the base game and the title update, all tested on hardware: Remove
  screen overlay, Fix screen tearing (V-Sync), Performance mode (anti-aliasing off), Turn off lamp
  and torch shadows, Turn off screen-space light shadows.

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
