"""Tray icon rendering: a neon ring that fills with the highest 5h usage across accounts."""

from __future__ import annotations

from PIL import Image, ImageDraw

BG = (4, 7, 10, 255)
CYAN = (0, 240, 255, 255)
MAGENTA = (255, 43, 214, 255)
TRACK = (22, 40, 46, 255)


def level_color(pct: float | None, fault: bool = False) -> tuple[int, int, int, int]:
    if pct is None:
        return MAGENTA if fault else CYAN
    if pct >= 90:
        return (255, 59, 92, 255)
    if pct >= 70:
        return (255, 176, 0, 255)
    return (57, 255, 136, 255)


def render_icon(pct: float | None, fault: bool = False, size: int = 64) -> Image.Image:
    """Draw at 4x and downsample for clean anti-aliasing at 16-32px tray sizes."""
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, s - 4, s - 4), fill=BG)

    color = level_color(pct, fault)
    box = (10, 10, s - 10, s - 10)
    d.arc(box, 0, 360, fill=TRACK, width=46)
    sweep = 360 if pct is None else max(8.0, min(100.0, pct) * 3.6)
    d.arc(box, -90, -90 + sweep, fill=color, width=46)

    # ">_" prompt glyph in the middle
    c = s // 2
    d.line([(c - 50, c - 38), (c - 8, c), (c - 50, c + 38)], fill=CYAN, width=24, joint="curve")
    d.line([(c + 6, c + 38), (c + 54, c + 38)], fill=MAGENTA if fault else CYAN, width=22)
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path: str) -> None:
    render_icon(None, size=256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
