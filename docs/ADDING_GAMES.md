# Adding a game

A game is one JSON file in `livemods/games/`. The app picks it up automatically: no code changes.
To test a definition without rebuilding, put it in `%APPDATA%\360LiveMods\games\`. A file there with
the same `id` overrides the built-in one.

## File format

```jsonc
{
  "schema": 1,
  "id": "fable2",                       // short unique id, used on the command line
  "name": "Fable II",
  "title_id": "4D5307F1",               // the game's Xbox 360 title ID
  "description": "One line shown under the game name.",
  "notes": ["Optional extra notes."],
  "credits": ["Who made which patch."],
  "banner": { "file": "fable2_banner.jpg", "focus": [0.5, 0.12] },  // optional header art next to this file;
                                        // focus = which part to keep when cropping (x, y from 0 to 1)

  "versions": [                         // one entry per executable the game can run as
    {
      "id": "base",
      "name": "Base game",
      "hint": "Title update disabled",
      "detect": { "address": "0x83282140", "values": ["388755A4", "388755B4"] },
      "fps_counter": { "pointer": "0x8336019C", "offset": "0x40B0" }   // optional live FPS readout:
                                        // a global holding a pointer, plus the offset of a counter
                                        // that goes up once per presented frame
    }
  ],

  "mods": [
    {
      "id": "bloom",
      "name": "Remove bloom",
      "category": "Visual",             // mods are grouped by category in the app
      "description": "What the player will notice.",
      "default": true,                  // ticked by "Recommended"
      "status": "tested",               // tested | experimental | unstable (unstable asks first)
      "credit": "punchbrotv",
      "warning": "Shown before enabling an unstable mod.",
      "patches": {
        "base": [["0x83282140", "388755A4", "388755B4"]]   // [address, original bytes, new bytes]
      },
      "unavailable": { "tu": "Reason shown when this version is selected." }
    }
  ]
}
```

### How patches are applied

- `detect`: the app polls this address while the game boots. When it holds one of `values`
  (the original bytes, or the patched bytes), that version is running and patching starts. Use a
  4-byte location that is unique to that version and that one of your patches touches.
- Each patch is checked before it's written: the address must hold exactly the `original` bytes.
  If it holds anything else, that mod is skipped and nothing is written for it. This is what keeps
  a wrong address from crashing someone's game.
- Original and new bytes must be the same length. Any length works.
- Patches are written as soon as the game's code is in memory, before the game runs its startup
  code. Settings the game reads only at boot are caught too.

### Porting Xenia patches

Xenia's `.patch.toml` files already contain addresses and values for a specific executable hash
(`be8`/`be16`/`be32` entries). The original bytes come from the game's decrypted `default.xex`. Be
aware that a title update usually moves the code, so it needs its own `versions` entry and its own
addresses. Find them by matching the surrounding instructions in a memory dump of the updated game,
and test on real hardware: some emulator patches don't survive the trip (see Fable II's 720p).

### Checklist before opening a pull request

- [ ] Tested on a real console, for every version you list
- [ ] `status` is honest (`experimental` until it has been tested)
- [ ] Credits name the original patch authors
- [ ] `python -m pytest tests -q` passes (it validates every definition)
