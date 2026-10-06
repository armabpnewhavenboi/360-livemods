"""Renders the game header: banner art, fades into the UI, and the game name set over it."""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HEADER_H = 176           # logical pixels
BG = (22, 25, 30)        # matches the window colour
LINE = (50, 56, 67)
TEXT = (238, 240, 243)
MUTED = (200, 205, 214)
FAINT = (160, 168, 180)
AMBER = (242, 179, 61)


def _ellipsize(d: ImageDraw.ImageDraw, text: str, font, width: float) -> str:
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _generated_art(canvas: Image.Image, game, W: int, H: int, scale: float) -> None:
    """Header art for games without a banner: a warm glow and the title ID set large and faint."""
    glow = Image.new("RGB", (W, H), (58, 46, 24))
    canvas.paste(glow, mask=_gradient(W, H, True, [(0, 0), (0.45, 0), (1, 120)]))
    canvas.paste(Image.new("RGB", (W, H), BG), mask=_gradient(W, H, False, [(0, 60), (0.5, 0), (1, 150)]))
    big = _font("ChakraPetch-SemiBold.ttf", round(H * 0.62))
    layer = Image.new("L", (W, H), 0)
    ld = ImageDraw.Draw(layer)
    text = game.title_id
    tw = ld.textlength(text, font=big)
    box = ld.textbbox((0, 0), text, font=big)
    ld.text((W - tw - round(18 * scale), round(H * 0.08) - box[1]), text, font=big, fill=30)
    keep = _gradient(W, H, True, [(0, 0), (0.42, 0), (0.85, 255), (1, 255)])   # clear behind the name
    layer = Image.composite(layer, Image.new("L", (W, H), 0), keep)
    canvas.paste(Image.new("RGB", (W, H), (255, 214, 140)), mask=layer)
    # fine diagonal hatching on the right, fading out towards the text
    hatch = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hatch)
    step = round(14 * scale)
    for x in range(-H, W, step):
        hd.line([(x, H), (x + H, 0)], fill=14, width=max(1, round(scale)))
    fade = _gradient(W, H, True, [(0, 0), (0.5, 0), (1, 255)])
    hatch = Image.composite(hatch, Image.new("L", (W, H), 0), fade)
    canvas.paste(Image.new("RGB", (W, H), (255, 220, 160)), mask=hatch)


def _asset(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath("assets", *parts)


@lru_cache(maxsize=16)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(_asset("fonts", name)), size)
    except OSError:
        return ImageFont.load_default(size)


@lru_cache(maxsize=8)
def _load(path: str) -> Image.Image:
    return Image.open(path).convert("RGB")


def _cover(img: Image.Image, w: int, h: int, focus: tuple[float, float]) -> Image.Image:
    """Scale to cover w x h, cropping around the focus point (0..1, 0..1)."""
    s = max(w / img.width, h / img.height)
    im = img.resize((max(w, round(img.width * s)), max(h, round(img.height * s))), Image.LANCZOS)
    x = round((im.width - w) * min(max(focus[0], 0), 1))
    y = round((im.height - h) * min(max(focus[1], 0), 1))
    return im.crop((x, y, x + w, y + h))


def _gradient(w: int, h: int, horizontal: bool, stops: list[tuple[float, int]]) -> Image.Image:
    """Alpha mask from (position 0..1, alpha 0..255) stops."""
    n = w if horizontal else h
    line = Image.new("L", (n, 1))
    px = line.load()
    for i in range(n):
        t = i / max(1, n - 1)
        for (p0, a0), (p1, a1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                k = (t - p0) / max(1e-6, p1 - p0)
                k = k * k * (3 - 2 * k)  # smoothstep
                px[i, 0] = round(a0 + (a1 - a0) * k)
                break
    return line.resize((w, h)) if horizontal else line.rotate(-90, expand=True).resize((w, h))


def render_banner(game, width: int, height: int = HEADER_H, scale: float = 2.0) -> Image.Image:
    W, H = max(1, round(width * scale)), max(1, round(height * scale))
    canvas = Image.new("RGB", (W, H), BG)
    if game.banner:
        try:
            src = _load(str(game.banner))
            # Fit the art to the header height (slightly zoomed) and anchor it to the right, so the
            # subject stays whole and the left side is free for the title.
            zoom = 1.25
            aw = round(src.width * H * zoom / src.height)
            if aw < W * 0.55:
                art = _cover(src, W, H, game.banner_focus)
                x0 = 0
            else:
                ah = round(H * zoom)
                art = src.resize((aw, ah), Image.LANCZOS)
                y0 = round((ah - H) * min(max(game.banner_focus[1], 0), 1))
                art = art.crop((0, y0, aw, y0 + H))
                x0 = W - aw
                if x0 < 0:            # narrow window: crop the art's left side
                    art = art.crop((-x0, 0, aw, H))
                    x0 = 0
            art = Image.blend(art, Image.new("RGB", art.size, BG), 0.12)
            fade = _gradient(art.width, H, True, [(0, 0), (0.32, 255), (1, 255)])
            canvas.paste(art, (x0, 0), mask=fade)
            shade = Image.new("RGB", (W, H), BG)
            canvas.paste(shade, mask=_gradient(W, H, True, [(0, 150), (0.45, 60), (0.7, 0), (1, 0)]))
            canvas.paste(shade, mask=_gradient(W, H, False, [(0, 0), (0.55, 0), (1, 140)]))
        except OSError:
            pass
    else:
        _generated_art(canvas, game, W, H, scale)

    d = ImageDraw.Draw(canvas)
    pad = round(30 * scale)
    f_tid = _font("IBMPlexMono-Regular.ttf", round(12 * scale))
    f_desc = _font("IBMPlexSans-Regular.ttf", round(15 * scale))
    f_kick = _font("IBMPlexSans-Medium.ttf", round(11 * scale))
    tid_text = f"Title ID {game.title_id}"
    tid_w = d.textlength(tid_text, font=f_tid)
    room = W - 2 * pad

    # the name: shrink to fit (long library titles), then ellipsize as a last resort
    size = 44
    while True:
        f_name = _font("ChakraPetch-SemiBold.ttf", round(size * scale))
        name = game.name
        if d.textlength(name, font=f_name) <= room or size <= 26:
            break
        size -= 2
    name = _ellipsize(d, name, f_name, room)
    tid_inline = d.textlength(name, font=f_name) + round(16 * scale) + tid_w <= room
    desc = _ellipsize(d, game.description, f_desc, room)
    kicker = "COMMUNITY LIBRARY" if getattr(game, "library", None) else ""
    if kicker and not tid_inline:
        kicker += f"   ·   {tid_text.upper()}"

    # layout from the bottom up
    desc_h = f_desc.getbbox("Ag")[3]
    y_desc = H - pad - desc_h
    name_box = d.textbbox((0, 0), name, font=f_name)
    name_h = name_box[3] - name_box[1]
    y_name = y_desc - round(10 * scale) - name_h - name_box[1]
    y_kick = y_name + name_box[1] - round(12 * scale) - f_kick.getbbox("A")[3]

    # soft shadow behind the text for legibility over bright art
    shadow = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(shadow)
    sd.text((pad, y_name), name, font=f_name, fill=200)
    sd.text((pad, y_desc), desc, font=f_desc, fill=160)
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(6 * scale)))
    canvas.paste(Image.new("RGB", (W, H), (8, 9, 11)), mask=shadow)

    if kicker:
        x = pad
        for ch in kicker:                       # letter-spaced small caps
            d.text((x, y_kick), ch, font=f_kick, fill=AMBER)
            x += d.textlength(ch, font=f_kick) + round(1.6 * scale)
    d.text((pad, y_name), name, font=f_name, fill=TEXT)
    if tid_inline:
        tid_x = pad + d.textlength(name, font=f_name) + round(16 * scale)
        d.text((tid_x, y_name + name_box[3] - f_tid.getbbox("A")[3] - round(4 * scale)),
               tid_text, font=f_tid, fill=FAINT)
    d.text((pad, y_desc), desc, font=f_desc, fill=MUTED)

    # rounded corners + hairline border, matching the cards below
    r = round(12 * scale)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=r, fill=255)
    out = Image.new("RGB", (W, H), BG)
    out.paste(canvas, mask=mask)
    ImageDraw.Draw(out).rounded_rectangle([0, 0, W - 1, H - 1], radius=r, outline=LINE, width=max(1, round(scale)))
    return out
