"""360 LiveMods desktop app (CustomTkinter)."""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path

import customtkinter as ctk

from . import APP_NAME, __version__
from .config import Settings
from .engine import Engine, FpsMonitor
from .games import Game, Mod, load_all, user_dir
from . import updater
from .banner import HEADER_H, render_banner
from .xbdm import test_connection

REPO_URL = "https://github.com/armabpnewhavenboi/360-livemods"

# ------------------------------------------------------------------ design tokens
C = {
    "bg": "#16191E",        # graphite window
    "panel": "#1E2228",     # sidebar / cards
    "raised": "#272C34",    # inputs, hover
    "line": "#323843",      # hairlines, borders
    "text": "#E7E9EC",
    "muted": "#8D95A3",
    "faint": "#5E6673",
    "amber": "#F2B33D",     # the status light: primary action + active states
    "amber_hi": "#FFC65C",
    "amber_ink": "#1A1405",
    "ok": "#5FCF9A",
    "warn": "#F2B33D",
    "err": "#FF6F6F",
}
STATUS_TAG = {  # mod.status -> (label, colour)
    "tested": ("Tested", C["ok"]),
    "experimental": ("Experimental", C["warn"]),
    "unstable": ("Unstable", C["err"]),
}


def _asset(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath("assets", *parts)


class Fonts:
    """Loads the bundled typefaces, falling back to system fonts if anything fails."""

    def __init__(self, root: tk.Misc):
        for f in ("ChakraPetch-SemiBold.ttf", "IBMPlexSans-Regular.ttf",
                  "IBMPlexSans-Medium.ttf", "IBMPlexMono-Regular.ttf"):
            try:
                ctk.FontManager.load_font(str(_asset("fonts", f)))
            except Exception:  # noqa: BLE001
                pass
        fams = set(tkfont.families(root))
        ui = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
        self.display = "Chakra Petch" if "Chakra Petch" in fams else ui
        self.body = "IBM Plex Sans" if "IBM Plex Sans" in fams else ui
        self.body_med = next((f for f in ("IBM Plex Sans Medm", "IBM Plex Sans Medium") if f in fams), self.body)
        self.mono = "IBM Plex Mono" if "IBM Plex Mono" in fams else ("Consolas" if sys.platform == "win32" else "DejaVu Sans Mono")

    def d(self, size: int) -> ctk.CTkFont:
        return ctk.CTkFont(family=self.display, size=size)

    def b(self, size: int = 13) -> ctk.CTkFont:
        return ctk.CTkFont(family=self.body, size=size)

    def m(self, size: int = 13) -> ctk.CTkFont:
        return ctk.CTkFont(family=self.body_med, size=size)

    def mono_f(self, size: int = 12) -> ctk.CTkFont:
        return ctk.CTkFont(family=self.mono, size=size)


class Led(tk.Canvas):
    """A small status light. Pulses while 'busy'."""

    def __init__(self, master, size=14, **kw):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0, bg=C["panel"], **kw)
        self.size = size
        self._color = C["faint"]
        self._pulse = False
        self._phase = 0
        self._draw(self._color)

    def _draw(self, fill, glow=None):
        self.delete("all")
        s = self.size
        if glow:
            self.create_oval(0, 0, s, s, fill=glow, outline="")
        self.create_oval(3, 3, s - 3, s - 3, fill=fill, outline="")

    def set(self, color: str, pulse: bool = False):
        self._color, was = color, self._pulse
        self._pulse = pulse
        self._draw(color)
        if pulse and not was:
            self._tick()

    def _tick(self):
        if not self._pulse:
            self._draw(self._color)
            return
        self._phase = (self._phase + 1) % 20
        glow = _mix(self._color, C["panel"], 0.35 + 0.45 * abs(10 - self._phase) / 10)
        self._draw(self._color, glow)
        self.after(60, self._tick)


def _mix(a: str, b: str, t: float) -> str:
    """Blend colour a towards b by t (0..1)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


class ModRow(ctk.CTkFrame):
    def __init__(self, master, app: "App", mod: Mod):
        super().__init__(master, fg_color="transparent")
        self.app, self.mod = app, mod
        self.var = tk.BooleanVar(value=False)
        self.grid_columnconfigure(1, weight=1)
        F = app.F
        self.box = ctk.CTkCheckBox(self, text="", variable=self.var, width=24, checkbox_width=20,
                                   checkbox_height=20, corner_radius=5, border_width=2,
                                   fg_color=C["amber"], hover_color=C["amber_hi"], checkmark_color=C["amber_ink"],
                                   border_color=C["faint"], command=self._toggled)
        self.box.grid(row=0, column=0, rowspan=2, sticky="n", padx=(2, 12), pady=(3, 0))
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=1, sticky="ew")
        self.name = ctk.CTkLabel(head, text=mod.name, font=F.m(14), text_color=C["text"], anchor="w")
        self.name.pack(side="left")
        self.name.bind("<Button-1>", lambda e: self._click())
        label, color = STATUS_TAG.get(mod.status, ("", C["muted"]))
        self.tag = ctk.CTkLabel(head, text=label, font=F.b(11), text_color=color, anchor="w")
        self.tag.pack(side="left", padx=(10, 0))
        self.desc = ctk.CTkLabel(self, text=mod.description, font=F.b(12), text_color=C["muted"],
                                 anchor="w", justify="left", wraplength=560)
        self.desc.grid(row=1, column=1, sticky="ew", pady=(1, 0))
        credit = f"Patch by {mod.credit}" if mod.credit else ""

    def _click(self):
        if self.box.cget("state") != "disabled":
            self.var.set(not self.var.get())
            self._toggled()

    def _toggled(self):
        if self.var.get() and self.mod.status == "unstable":
            ok = ConfirmDialog.ask(self.app, f"Enable “{self.mod.name}”?",
                                   self.mod.warning or "This mod is marked unstable and may crash the game.",
                                   "Enable anyway")
            if not ok:
                self.var.set(False)
        self.app.on_selection_changed()

    def _show_desc(self, text: str, color: str):
        self.desc.configure(text=text, text_color=color)
        if text:
            self.desc.grid()
        else:
            self.desc.grid_remove()

    def set_version(self, version_id: str):
        if self.mod.available_for(version_id):
            label, color = STATUS_TAG.get(self.mod.status, ("", C["muted"]))
            self.tag.configure(text=label, text_color=color)
            self.box.configure(state="normal")
            self.name.configure(text_color=C["text"])
            self._show_desc(self.mod.description, C["muted"])
        else:
            self.var.set(False)
            self.box.configure(state="disabled")
            self.name.configure(text_color=C["faint"])
            others = [v.name for v in self.app.game.versions if self.mod.available_for(v.id)]
            self.tag.configure(text=f"{others[0]} only" if len(others) == 1 else "Not available",
                               text_color=C["faint"])
            reason = self.mod.unavailable.get(version_id, "Not available for this version.")
            self._show_desc(reason, C["faint"])


class SideItem(ctk.CTkFrame):
    """Sidebar entry: game name with its title ID underneath, amber bar when selected."""

    def __init__(self, master, app: "App", name: str, sub: str, on_click, compact: bool = False):
        h = 44 if compact else 54
        super().__init__(master, fg_color="transparent", corner_radius=8, height=h)
        self.app, self.selected, self.enabled = app, False, True
        self.bar = ctk.CTkFrame(self, width=3, height=24 if compact else 30, corner_radius=2, fg_color="transparent")
        self.bar.place(x=0, rely=0.5, anchor="w")
        font = app.F.m(13 if compact else 14)
        self.name = ctk.CTkLabel(self, text=_fit(name, font, 196, self), font=font, text_color=C["text"], anchor="w",
                                 height=18 if compact else 20)
        self.name.place(x=14, y=5 if compact else 8)
        self.tid = ctk.CTkLabel(self, text=sub, font=app.F.mono_f(10 if compact else 11), text_color=C["faint"],
                                anchor="w", height=14)
        self.tid.place(x=14, y=24 if compact else 30)
        for w in (self, self.name, self.tid):
            w.bind("<Button-1>", lambda e: self.enabled and on_click())
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))

    def _hover(self, on: bool):
        if not self.selected and self.enabled:
            self.configure(fg_color=C["raised"] if on else "transparent")

    def set_selected(self, on: bool):
        self.selected = on
        self.configure(fg_color=C["raised"] if on else "transparent")
        self.bar.configure(fg_color=C["amber"] if on else "transparent")

    def configure(self, **kw):
        if "state" in kw:
            self.enabled = kw.pop("state") != "disabled"
            self.name.configure(text_color=C["text"] if self.enabled else C["faint"])
        if kw:
            super().configure(**kw)


def _fit(text: str, font: ctk.CTkFont, width: int, widget) -> str:
    """Shorten text with an ellipsis so it fits `width` logical pixels."""
    try:
        # CTkFont measures in unscaled (logical) pixels; scaling is applied when it's drawn
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text.rstrip(" -:,") + "…"
    except Exception:  # noqa: BLE001 - measuring is cosmetic
        return text


class ConfirmDialog(ctk.CTkToplevel):
    @classmethod
    def ask(cls, app: "App", title: str, body: str, confirm: str) -> bool:
        d = cls(app, title, body, confirm)
        app.wait_window(d)
        return d.result

    def __init__(self, app: "App", title: str, body: str, confirm: str):
        super().__init__(app, fg_color=C["panel"])
        self.result = False
        self.title(APP_NAME)
        self.resizable(False, False)
        self.transient(app)
        F = app.F
        ctk.CTkLabel(self, text=title, font=F.d(18), text_color=C["text"]).pack(anchor="w", padx=24, pady=(22, 6))
        ctk.CTkLabel(self, text=body, font=F.b(13), text_color=C["muted"], wraplength=380,
                     justify="left").pack(anchor="w", padx=24)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=22)
        ctk.CTkButton(row, text="Cancel", width=100, fg_color=C["raised"], hover_color=C["line"],
                      text_color=C["text"], font=F.m(13), command=self.destroy).pack(side="right")
        ctk.CTkButton(row, text=confirm, width=130, fg_color=C["err"], hover_color=_mix(C["err"], "#ffffff", .15),
                      text_color="#1b0b0b", font=F.m(13), command=self._yes).pack(side="right", padx=(0, 8))
        self.after(10, self.grab_set)

    def _yes(self):
        self.result = True
        self.destroy()


class AboutDialog(ctk.CTkToplevel):
    def __init__(self, app: "App"):
        super().__init__(app, fg_color=C["panel"])
        self.title(f"About {APP_NAME}")
        self.geometry("560x520")
        self.transient(app)
        F = app.F
        ctk.CTkLabel(self, text=APP_NAME, font=F.d(26), text_color=C["text"]).pack(anchor="w", padx=28, pady=(24, 0))
        ctk.CTkLabel(self, text=f"Version {__version__}", font=F.b(12), text_color=C["muted"]).pack(anchor="w", padx=28)
        box = ctk.CTkTextbox(self, font=F.b(13), fg_color=C["bg"], text_color=C["text"], wrap="word",
                             border_width=0, corner_radius=8)
        box.pack(fill="both", expand=True, padx=28, pady=16)
        lines = [
            "Live, non-destructive mods for RGH/JTAG Xbox 360 consoles. Nothing on your console "
            "or in your game files is changed: patches are written into memory while the game runs "
            "and disappear when you quit.",
            "",
            "Requirements: an RGH or JTAG console running the XBDM plugin (loaded through "
            "DashLaunch), on the same network as this PC.",
            "",
            "Credits",
        ]
        for g in app.games:
            lines.append(f"\n{g.name}")
            lines += [f"  • {c}" for c in g.credits]
        lines += ["", "Fonts: Chakra Petch and IBM Plex (SIL Open Font License).",
                  "Game artwork belongs to its respective owners and is shown only to identify the game.", "",
                  f"Extra game definitions can be added to:\n{user_dir()}", "",
                  "Not affiliated with Microsoft, Xbox or any game publisher. Use at your own risk; "
                  "back up your saves."]
        box.insert("1.0", "\n".join(lines))
        box.configure(state="disabled")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=28, pady=(0, 22))
        ctk.CTkButton(row, text="Open project page", font=F.m(13), fg_color=C["raised"], hover_color=C["line"],
                      text_color=C["text"], command=lambda: webbrowser.open(REPO_URL)).pack(side="left")
        ctk.CTkButton(row, text="Close", width=90, font=F.m(13), fg_color=C["amber"], hover_color=C["amber_hi"],
                      text_color=C["amber_ink"], command=self.destroy).pack(side="right")


class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=C["bg"])
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self.geometry("1120x760")
        self.minsize(980, 660)
        try:
            if sys.platform == "win32":
                self.iconbitmap(str(_asset("icon.ico")))
            else:
                self.iconphoto(True, tk.PhotoImage(file=str(_asset("icon.png"))))
        except Exception:  # noqa: BLE001
            pass
        self.F = Fonts(self)
        self.settings = Settings()
        self.games, self.load_errors = load_all()
        self.game: Game | None = None
        self.rows: dict[str, ModRow] = {}
        self.events: queue.Queue = queue.Queue()
        self.engine: Engine | None = None
        self.worker: threading.Thread | None = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_events)
        self.after(2500, self.check_updates)
        if self.games:
            last = self.settings.data.get("last_game")
            self.select_game(next((g for g in self.games if g.id == last), self.games[0]), user=False)
        for e in self.load_errors:
            self.log("warn", f"Could not load game definition {e}")

    # ------------------------------------------------------------------ layout
    def _build_sidebar(self):
        F = self.F
        side = ctk.CTkFrame(self, width=256, corner_radius=0, fg_color=C["panel"])
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.grid_rowconfigure(5, weight=1)
        side.grid_columnconfigure(0, weight=1)
        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=20, pady=(22, 20))
        try:
            from PIL import Image
            img = ctk.CTkImage(Image.open(_asset("icon_small.png")), size=(30, 30))
            ctk.CTkLabel(brand, image=img, text="").pack(side="left", padx=(0, 10))
        except Exception:  # noqa: BLE001
            pass
        ctk.CTkLabel(brand, text=APP_NAME, font=F.d(21), text_color=C["text"]).pack(side="left")

        ctk.CTkLabel(side, text="Games", font=F.m(12), text_color=C["faint"], anchor="w").grid(
            row=1, column=0, sticky="ew", padx=22, pady=(0, 4))
        self.game_list = ctk.CTkFrame(side, fg_color="transparent")
        self.game_list.grid(row=2, column=0, sticky="new", padx=12)
        self.game_buttons: dict[str, SideItem] = {}
        for g in self.games:
            b = SideItem(self.game_list, self, g.name, g.title_id, lambda g=g: self.select_game(g))
            b.pack(fill="x", pady=2)
            self.game_buttons[g.id] = b
        if not self.games:
            ctk.CTkLabel(self.game_list, text="No games found", font=F.b(13), text_color=C["muted"]).pack()

        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.grid(row=8, column=0, sticky="sew", padx=14, pady=(10, 16))

        # update card - hidden until a newer release is found
        self.upd_card = ctk.CTkFrame(foot, fg_color=C["raised"], corner_radius=10, border_width=1,
                                     border_color=_mix(C["amber"], C["panel"], 0.45))
        self.upd_title = ctk.CTkLabel(self.upd_card, text="Update available", font=F.d(15),
                                      text_color=C["amber"], anchor="w")
        self.upd_title.pack(fill="x", padx=14, pady=(10, 0))
        self.upd_sub = ctk.CTkLabel(self.upd_card, text="", font=F.b(12), text_color=C["muted"], anchor="w",
                                    justify="left", wraplength=170)
        self.upd_sub.pack(fill="x", padx=14)
        self.upd_btn = ctk.CTkButton(self.upd_card, text="Update now", height=32, font=F.m(13),
                                     fg_color=C["amber"], hover_color=C["amber_hi"], text_color=C["amber_ink"],
                                     command=self.install_update)
        self.upd_btn.pack(fill="x", padx=14, pady=(8, 12))
        self._update = None

        self.about_btn = ctk.CTkButton(foot, text="About & credits", font=F.b(13), anchor="w",
                                       fg_color="transparent", hover_color=C["raised"], text_color=C["muted"],
                                       height=30, command=lambda: AboutDialog(self))
        self.about_btn.pack(fill="x", padx=6)
        vrow = ctk.CTkFrame(foot, fg_color="transparent")
        vrow.pack(fill="x", padx=14, pady=(4, 0))
        self.ver_lbl = ctk.CTkLabel(vrow, text=f"v{__version__}", font=F.b(11), text_color=C["faint"], anchor="w")
        self.ver_lbl.pack(side="left")
        self.check_btn = ctk.CTkButton(vrow, text="Check for updates", width=10, height=22, font=F.b(11),
                                       fg_color="transparent", hover_color=C["raised"], text_color=C["faint"],
                                       command=lambda: self.check_updates(manual=True))
        self.check_btn.pack(side="right")

    def _card(self, master, **kw) -> ctk.CTkFrame:
        return ctk.CTkFrame(master, fg_color=C["panel"], corner_radius=10, border_width=1,
                            border_color=C["line"], **kw)

    def _build_main(self):
        F = self.F
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=(28, 28), pady=(22, 20))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(3, weight=1)

        # header: game banner with the name drawn over it (rendered with Pillow, see banner.py)
        self.header = ctk.CTkLabel(main, text="", fg_color="transparent")
        self.header.grid(row=0, column=0, sticky="ew")
        self._header_w = 0
        self._header_job = None
        main.bind("<Configure>", self._schedule_header, add="+")

        # setup row: console + version
        setup = ctk.CTkFrame(main, fg_color="transparent")
        setup.grid(row=1, column=0, sticky="ew", pady=(18, 14))
        setup.grid_columnconfigure((0, 1), weight=1, uniform="setup")

        con = self._card(setup)
        con.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ctk.CTkLabel(con, text="Console", font=F.m(13), text_color=C["muted"], anchor="w").pack(
            fill="x", padx=16, pady=(12, 6))
        r = ctk.CTkFrame(con, fg_color="transparent")
        r.pack(fill="x", padx=16)
        self.ip = ctk.CTkEntry(r, placeholder_text="Console IP, e.g. 192.168.1.50", font=F.b(14), height=36,
                               fg_color=C["raised"], border_color=C["line"], text_color=C["text"])
        self.ip.pack(side="left", fill="x", expand=True)
        if self.settings.console_ip:
            self.ip.insert(0, self.settings.console_ip)
        self.test_btn = ctk.CTkButton(r, text="Test", width=70, height=36, font=F.m(13), fg_color=C["raised"],
                                      hover_color=C["line"], text_color=C["text"], border_width=1,
                                      border_color=C["line"], command=self.test_console)
        self.test_btn.pack(side="left", padx=(8, 0))
        self.con_msg = ctk.CTkLabel(con, text="Shown on Aurora's main screen. Needs XBDM loaded via DashLaunch.",
                                    font=F.b(12), text_color=C["faint"], anchor="w", wraplength=360, justify="left")
        self.con_msg.pack(fill="x", padx=16, pady=(6, 12))

        ver = self._card(setup)
        ver.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ctk.CTkLabel(ver, text="Game version", font=F.m(13), text_color=C["muted"], anchor="w").pack(
            fill="x", padx=16, pady=(12, 6))
        self.version_seg = ctk.CTkSegmentedButton(
            ver, values=["-"], height=36, font=F.m(13), fg_color=C["raised"], unselected_color=C["raised"],
            unselected_hover_color=C["line"], selected_color=C["amber"], selected_hover_color=C["amber_hi"],
            text_color=C["text"], command=self._version_changed)
        self.version_seg.pack(fill="x", padx=16)
        self.ver_msg = ctk.CTkLabel(ver, text="", font=F.b(12), text_color=C["faint"], anchor="w",
                                    wraplength=360, justify="left")
        self.ver_msg.pack(fill="x", padx=16, pady=(6, 12))
        # keep the small print inside its card at any window size
        con.bind("<Configure>", lambda e: self._wrap_to(self.con_msg, e.width), add="+")
        ver.bind("<Configure>", lambda e: self._wrap_to(self.ver_msg, e.width), add="+")

        # mods
        mh = ctk.CTkFrame(main, fg_color="transparent")
        mh.grid(row=2, column=0, sticky="ew", pady=(4, 6))
        ctk.CTkLabel(mh, text="Mods", font=F.d(20), text_color=C["text"]).pack(side="left")
        self.sel_count = ctk.CTkLabel(mh, text="", font=F.b(12), text_color=C["muted"])
        self.sel_count.pack(side="left", padx=(12, 0), pady=(4, 0))
        self.mod_buttons = {}
        for txt, cmd in (("Clear", self.select_none), ("Recommended", self.select_defaults)):
            b = ctk.CTkButton(mh, text=txt, width=10, height=26, font=F.b(12), fg_color="transparent",
                              hover_color=C["raised"], text_color=C["muted"], command=cmd)
            b.pack(side="right", padx=(4, 0))
            self.mod_buttons[txt] = b
        self.mod_frame = ctk.CTkScrollableFrame(main, fg_color=C["panel"], corner_radius=10, border_width=1,
                                                border_color=C["line"], scrollbar_button_color=C["line"],
                                                scrollbar_button_hover_color=C["faint"])
        self.mod_frame.grid(row=3, column=0, sticky="nsew")
        self.mod_frame.grid_columnconfigure(0, weight=1)
        # Listen on the outer canvas, and add to (not replace) its bindings: the scrollable frame's
        # own <Configure> handler is what keeps the scrollbar's range up to date.
        self.mod_frame._parent_canvas.bind("<Configure>", self._rewrap, add="+")

        # action / status panel (the one loud element)
        act = ctk.CTkFrame(main, fg_color=C["panel"], corner_radius=10, border_width=1, border_color=C["line"])
        act.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        act.grid_columnconfigure(1, weight=1)
        self.act = act
        self.led = Led(act, size=22)
        self.led.grid(row=0, column=0, padx=(18, 12), pady=18)
        st = ctk.CTkFrame(act, fg_color=C["panel"], corner_radius=0)
        st.grid(row=0, column=1, sticky="ew", pady=2)
        st.bind("<Configure>", lambda e: (self._wrap_to(self.status_sub, e.width, 0),
                                          self._wrap_to(self.status, e.width, 0)), add="+")
        self.status = ctk.CTkLabel(st, text="Ready", font=F.d(20), text_color=C["text"], anchor="w", justify="left")
        self.status.pack(fill="x")
        self.status_sub = ctk.CTkLabel(st, text="Press Start, then launch the game on your console.",
                                       font=F.b(12), text_color=C["muted"], anchor="w", justify="left")
        self.status_sub.pack(fill="x")
        # live frame rate (shown while the game runs with mods active)
        self.fps_box = ctk.CTkFrame(act, fg_color="transparent")
        self.fps_val = ctk.CTkLabel(self.fps_box, text="--", font=F.d(34), text_color=C["text"], anchor="e")
        self.fps_val.pack(side="left")
        ctk.CTkLabel(self.fps_box, text="FPS", font=F.m(12), text_color=C["muted"]).pack(
            side="left", padx=(6, 0), pady=(14, 0))
        self.fps_monitor = None
        self.restore_btn = ctk.CTkButton(act, text="Restore originals", height=42, width=150, font=F.m(13),
                                         fg_color=C["raised"], hover_color=C["line"], text_color=C["text"],
                                         command=self.restore)
        self.restore_btn.grid(row=0, column=2, padx=(0, 10))
        self.start_btn = ctk.CTkButton(act, text="Start", height=42, width=170, font=F.d(17),
                                       fg_color=C["amber"], hover_color=C["amber_hi"], text_color=C["amber_ink"],
                                       command=self.start_or_stop)
        self.start_btn.grid(row=0, column=3, padx=(0, 16))

        # log
        lh = ctk.CTkFrame(main, fg_color="transparent")
        lh.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.log_toggle = ctk.CTkButton(lh, text="Show activity log", width=10, height=24, font=F.b(12),
                                        fg_color="transparent", hover_color=C["raised"], text_color=C["muted"],
                                        command=self._toggle_log)
        self.log_toggle.pack(side="left")
        self.logbox = ctk.CTkTextbox(main, height=130, font=self.F.mono_f(12), fg_color=C["panel"],
                                     text_color=C["text"], border_width=1, border_color=C["line"],
                                     corner_radius=10, wrap="word")
        for k, col in (("ok", C["ok"]), ("warn", C["warn"]), ("error", C["err"]), ("info", C["muted"])):
            self.logbox.tag_config(k, foreground=col)
        self.logbox.configure(state="disabled")
        self._log_visible = False
        self._main = main

    # ------------------------------------------------------------- game state
    def select_game(self, game: Game, user: bool = True):
        if self.busy:
            return
        self.game = game
        self.settings.data["last_game"] = game.id
        for gid, b in self.game_buttons.items():
            b.set_selected(gid == game.id)
        self._header_w = 0
        self._render_header()
        gs = self.settings.game(game.id)
        names = [v.name for v in game.versions]
        self.version_seg.configure(values=names)
        vid = gs.get("version", game.versions[0].id)
        self.version_seg.set(game.version(vid).name if vid in {v.id for v in game.versions} else names[0])

        for w in self.mod_frame.winfo_children():
            w.destroy()
        self.rows.clear()
        r = 0
        cats: dict[str, list[Mod]] = {}
        for m in game.mods:
            cats.setdefault(m.category, []).append(m)
        for ci, (cat, mods) in enumerate(cats.items()):
            ctk.CTkLabel(self.mod_frame, text=cat, font=self.F.d(15), text_color=C["amber"], anchor="w").grid(
                row=r, column=0, sticky="ew", padx=18, pady=(16 if ci == 0 else 22, 6))
            r += 1
            for m in mods:
                row = ModRow(self.mod_frame, self, m)
                row.grid(row=r, column=0, sticky="ew", padx=18, pady=6)
                self.rows[m.id] = row
                r += 1
        if not game.mods:
            ctk.CTkLabel(self.mod_frame, text="No patches for this game.", font=self.F.b(13),
                         text_color=C["muted"]).grid(row=r, column=0, pady=30)
        self.mod_frame._parent_canvas.yview_moveto(0)
        self._apply_version_to_rows()
        self.after(50, self._rewrap)
        saved = gs.get("mods")
        for mid, row in self.rows.items():
            want = (mid in saved) if saved is not None else row.mod.default
            if want and row.mod.available_for(self.version_id) and row.mod.status != "unstable":
                row.var.set(True)
        self.on_selection_changed()

    def _schedule_header(self, event=None):
        if self._header_job:
            self.after_cancel(self._header_job)
        self._header_job = self.after(60, self._render_header)

    def _render_header(self):
        self._header_job = None
        if not self.game:
            return
        w = self.header.master.winfo_width()
        if w < 200 or w == self._header_w:
            return
        self._header_w = w
        scale = ctk.ScalingTracker.get_window_scaling(self)
        logical_w = max(200, round(w / scale))            # winfo_width is in physical pixels
        img = render_banner(self.game, logical_w, HEADER_H, scale * 2)
        self._header_img = ctk.CTkImage(img, size=(logical_w, HEADER_H))
        self.header.configure(image=self._header_img)

    def _wrap_to(self, label, width_px: int, pad: int = 32):
        scale = ctk.ScalingTracker.get_widget_scaling(label)
        label.configure(wraplength=max(120, round(width_px / scale) - pad))

    def _rewrap(self, event=None):
        width = max(320, self.mod_frame._parent_canvas.winfo_width() - 110)
        for row in self.rows.values():
            row.desc.configure(wraplength=width)

    @property
    def version_id(self) -> str:
        name = self.version_seg.get()
        return next(v.id for v in self.game.versions if v.name == name)

    def _version_changed(self, _value=None):
        self._apply_version_to_rows()
        # re-tick defaults that became available again
        for row in self.rows.values():
            if row.mod.available_for(self.version_id) and row.mod.default and row.mod.id in self._remembered:
                row.var.set(True)
        self.on_selection_changed()

    def _apply_version_to_rows(self):
        v = self.game.version(self.version_id)
        self.ver_msg.configure(text=f"{v.hint}. Must match the title update setting for "
                                    f"{self.game.name} in Aurora/FSD.")
        for row in self.rows.values():
            row.set_version(self.version_id)

    @property
    def _remembered(self) -> set[str]:
        return set(self.settings.game(self.game.id).get("mods") or [m.id for m in self.game.mods if m.default])

    def selected_mods(self) -> list[Mod]:
        return [r.mod for r in self.rows.values() if r.var.get()]

    def on_selection_changed(self):
        n = len(self.selected_mods())
        avail = sum(1 for r in self.rows.values() if r.mod.available_for(self.version_id))
        self.sel_count.configure(text=f"{n} of {avail} selected")
        gs = self.settings.game(self.game.id)
        gs["version"] = self.version_id
        # remember choices, keeping ones that are only hidden by the current version
        hidden = {mid for mid in (gs.get("mods") or [])
                  if mid in self.rows and not self.rows[mid].mod.available_for(self.version_id)}
        gs["mods"] = sorted({m.id for m in self.selected_mods()} | hidden)
        self.settings.save()

    def select_defaults(self):
        for r in self.rows.values():
            r.var.set(r.mod.default and r.mod.available_for(self.version_id) and r.mod.status != "unstable")
        self.on_selection_changed()

    def select_none(self):
        for r in self.rows.values():
            r.var.set(False)
        self.on_selection_changed()

    # ------------------------------------------------------------------ actions
    @property
    def busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _ip(self) -> str | None:
        ip = self.ip.get().strip()
        if not ip:
            self.con_msg.configure(text="Enter your console's IP address first.", text_color=C["err"])
            self.ip.focus_set()
            return None
        self.settings.console_ip = ip
        self.settings.save()
        return ip

    def test_console(self):
        ip = self._ip()
        if not ip:
            return
        self.test_btn.configure(state="disabled", text="…")
        self.con_msg.configure(text=f"Connecting to {ip}…", text_color=C["muted"])

        def work():
            try:
                name = test_connection(ip)
                self.events.put(("conn_ok", f"Connected to {name}. XBDM is running."))
            except Exception as e:  # noqa: BLE001
                hint = ("Check the IP on Aurora's main screen and that XBDM is in DashLaunch's plugins."
                        if isinstance(e, OSError) else str(e))
                self.events.put(("conn_err", f"No answer from {ip}. {hint}"))
        threading.Thread(target=work, daemon=True).start()

    def start_or_stop(self):
        if self.busy:
            if self.engine:
                self.engine.stop()
            self._set_status("Stopping…", C["muted"], False)
            return
        mods = self.selected_mods()
        if not mods:
            self._set_status("Nothing selected", C["warn"], False, "Tick at least one mod.")
            return
        self._run(mods, restore=False, wait=True)

    def restore(self):
        if self.busy:
            return
        mods = [r.mod for r in self.rows.values() if r.mod.available_for(self.version_id)]
        self._run(mods, restore=True, wait=False)

    def _run(self, mods: list[Mod], restore: bool, wait: bool):
        ip = self._ip()
        if not ip:
            return
        game, vid = self.game, self.version_id
        self._stop_fps()
        self._fps_target = (ip, game.version(vid))
        if not self._log_visible:
            self._toggle_log()
        self.log("info", "─" * 46)
        self.log("info", f"{'Restoring' if restore else 'Starting'}: {game.name} · "
                         f"{game.version(vid).name} · "
                         f"{', '.join(m.name for m in mods)}")
        self.engine = Engine(lambda k, m: self.events.put((k, m)))
        eng = self.engine

        def work():
            try:
                eng.run(ip, game, vid, mods, restore=restore, wait=wait)
            except Exception as e:  # noqa: BLE001
                self.events.put(("error", f"{e.__class__.__name__}: {e}"))
                self.events.put(("status", "Couldn't reach the game"))
            self.events.put(("done", ""))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._lock(True)
        if wait:
            self._set_status(f"Waiting for {game.name}", C["amber"], True,
                             "Launch the game on your console now. Patches go in the moment it loads.")
        else:
            self._set_status("Connecting…", C["amber"], True, "")

    def _lock(self, on: bool):
        state = "disabled" if on else "normal"
        for w in (self.version_seg, self.ip, self.test_btn, self.restore_btn,
                  *self.mod_buttons.values()):
            w.configure(state=state)
        for b in self.game_buttons.values():
            b.configure(state=state)
        for r in self.rows.values():
            if r.mod.available_for(self.version_id):
                r.box.configure(state=state)
        self.start_btn.configure(text="Stop" if on else "Start",
                                 fg_color=C["raised"] if on else C["amber"],
                                 hover_color=C["line"] if on else C["amber_hi"],
                                 text_color=C["text"] if on else C["amber_ink"])

    def _set_status(self, text: str, color: str, pulse: bool, sub: str | None = None):
        self.status.configure(text=text)
        self.led.set(color, pulse)
        self.act.configure(border_color=_mix(color, C["panel"], 0.35) if pulse else C["line"])
        if sub is not None:
            self.status_sub.configure(text=sub)

    # ---------------------------------------------------------------- events
    def _drain_events(self):
        try:
            while True:
                kind, msg = self.events.get_nowait()
                self._handle(kind, msg)
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _handle(self, kind: str, msg: str):
        if kind == "conn_ok":
            self.test_btn.configure(state="normal", text="Test")
            self.con_msg.configure(text=msg, text_color=C["ok"])
        elif kind == "conn_err":
            self.test_btn.configure(state="normal", text="Test")
            self.con_msg.configure(text=msg, text_color=C["err"])
        elif kind == "status":
            low = msg.lower()
            if low in ("mods active", "restored"):
                self._set_status(msg, C["ok"], False, "Patches last until you quit the game.")
                if low == "mods active":
                    self._start_fps()
            # match fixed prefixes only: game names can contain any word ("XCOM: Enemy Unknown")
            elif low.startswith(("wrong game version", "game not running", "couldn't reach the game")):
                self._set_status(msg, C["err"], False, "See the activity log for details.")
            elif low.startswith("finished with warnings"):
                self._set_status(msg, C["warn"], False, "See the activity log for details.")
            elif low == "stopped":
                self._set_status("Stopped", C["faint"], False, "Press Start, then launch the game on your console.")
            else:
                self._set_status(msg, C["amber"], True)
        elif kind.startswith("update_"):
            self._handle_update(kind, msg)
        elif kind == "fps":
            fps, avg, low_ = msg
            self.fps_val.configure(text=f"{fps:.0f}",
                                   text_color=C["ok"] if fps >= 55 else C["amber"] if fps >= 40 else C["text"])
            self.status_sub.configure(text=f"Average {avg:.0f} FPS, low {low_:.0f}. Patches last until you quit.")
        elif kind == "fps_end":
            self._stop_fps()
            if msg:
                self._set_status("Game closed" if "closed" in msg else "Disconnected", C["faint"], False,
                                 msg + " Press Start before launching it again.")
        elif kind == "done":
            self._lock(False)
            if self.status.cget("text").startswith(("Waiting", "Connecting", "Stopping")):
                self._set_status("Ready", C["faint"], False, "Press Start, then launch the game on your console.")
        else:
            self.log(kind, msg)

    def log(self, kind: str, msg: str):
        prefix = {"ok": "✓ ", "warn": "! ", "error": "✗ "}.get(kind, "  ")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", prefix + msg + "\n", kind if kind in ("ok", "warn", "error", "info") else None)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _toggle_log(self):
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.logbox.grid(row=6, column=0, sticky="ew", pady=(6, 0))
            self.log_toggle.configure(text="Hide activity log")
        else:
            self.logbox.grid_remove()
            self.log_toggle.configure(text="Show activity log")

    # ---------------------------------------------------------------- updates
    def check_updates(self, manual: bool = False):
        if manual:
            self.check_btn.configure(text="Checking…", state="disabled")

        def work():
            try:
                rel = updater.check()
                self.events.put(("update_available", rel) if rel else ("update_none", manual))
            except Exception:  # noqa: BLE001 - offline etc.; stay quiet unless asked
                self.events.put(("update_error", manual))
        threading.Thread(target=work, daemon=True).start()

    def install_update(self):
        rel = self._update
        if not rel:
            return
        if not (updater.is_installed_copy() and rel.setup_url):
            webbrowser.open(rel.page_url)
            return
        if self.busy:
            self.upd_sub.configure(text="Stop the current run first.")
            return
        self.upd_btn.configure(state="disabled", text="Downloading…")

        def work():
            try:
                path = updater.download(rel, lambda d, t: self.events.put(("update_progress", (d, t))))
                self.events.put(("update_ready", path))
            except Exception as e:  # noqa: BLE001
                self.events.put(("update_failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _handle_update(self, kind: str, data):
        if kind == "update_available":
            self._update = data
            self.check_btn.configure(text="Check for updates", state="normal")
            installable = updater.is_installed_copy() and data.setup_url
            self.upd_sub.configure(text=f"Version {data.version} is ready to install." if installable
                                   else f"Version {data.version} is out.")
            self.upd_btn.configure(text="Update now" if installable else "Get it on GitHub", state="normal")
            if not self.upd_card.winfo_ismapped():
                self.upd_card.pack(fill="x", pady=(0, 10), before=self.about_btn)
        elif kind in ("update_none", "update_error"):
            self.check_btn.configure(state="normal",
                                     text=("You're up to date" if kind == "update_none" else "Couldn't check")
                                     if data else "Check for updates")
            if data:
                self.after(4000, lambda: self.check_btn.configure(text="Check for updates"))
        elif kind == "update_progress":
            done, total = data
            pct = f" {done * 100 // total}%" if total else ""
            self.upd_btn.configure(text=f"Downloading{pct}")
        elif kind == "update_failed":
            self.upd_sub.configure(text=data)
            self.upd_btn.configure(text="Try again", state="normal")
        elif kind == "update_ready":
            self.upd_btn.configure(text="Installing…")
            self.upd_sub.configure(text="The app will close and reopen on the new version.")
            try:
                updater.launch_installer(data)
            except OSError as e:
                self._handle_update("update_failed", f"Couldn't start the installer: {e}")
                return
            self.after(900, self._on_close)

    # -------------------------------------------------------------- frame rate
    def _start_fps(self):
        ip, version = getattr(self, "_fps_target", (None, None))
        if not ip or version is None or version.fps_pointer is None:
            return
        self._stop_fps()
        self.fps_val.configure(text="--", text_color=C["text"])
        self.fps_box.grid(row=0, column=2, padx=(0, 18))
        self.restore_btn.grid_configure(column=3)
        self.start_btn.grid_configure(column=4)
        self.fps_monitor = FpsMonitor(ip, version,
                                      lambda f, a, l: self.events.put(("fps", (f, a, l))),
                                      lambda reason: self.events.put(("fps_end", reason)))
        threading.Thread(target=self.fps_monitor.run, daemon=True).start()

    def _stop_fps(self):
        if self.fps_monitor:
            self.fps_monitor.stop()
            self.fps_monitor = None
        if self.fps_box.winfo_ismapped():
            self.fps_box.grid_remove()
            self.restore_btn.grid_configure(column=2)
            self.start_btn.grid_configure(column=3)

    def _on_close(self):
        self._stop_fps()
        if self.engine:
            self.engine.stop()
        self.settings.save()
        self.destroy()


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LiveMods.360LiveMods")
        except Exception:  # noqa: BLE001
            pass
    App().mainloop()
    return 0
