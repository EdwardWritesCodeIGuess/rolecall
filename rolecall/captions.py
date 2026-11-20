"""Caption + overlay rendering as transparent PNGs (no libass/drawtext dependency).

Captions are word-synced: 1-3 word groups, active word highlighted, thick outline for
readability on busy GTA footage. The PNG sequence is fed to ffmpeg via the concat demuxer.
"""

from __future__ import annotations

import glob
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import get_config

W = 1080
CAP_H = 380
ASSETS = Path(__file__).resolve().parent.parent / "assets"


@lru_cache(maxsize=1)
def font_path() -> str:
    cfg = get_config()
    candidates = [cfg.caption_font] if cfg.caption_font else []
    candidates += sorted(glob.glob(str(ASSETS / "fonts" / "*.ttf")))
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Supplemental/Impact.ttf",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    raise RuntimeError("No caption font found. Put a .ttf in assets/fonts or set CAPTION_FONT.")


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(), size)


def group_words(words: list[dict], max_words: int = 3, max_chars: int = 18) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        token = w["word"].strip()
        if not token:
            continue
        w = {**w, "word": token}
        gap = (w["start"] - cur[-1]["end"]) if cur else 0
        chars = sum(len(x["word"]) + 1 for x in cur) + len(token)
        if cur and (len(cur) >= max_words or chars > max_chars or gap > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
        if token[-1] in ".!?":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _layout_lines(tokens: list[str], font, max_w: int) -> list[list[int]]:
    lines: list[list[int]] = [[]]
    for i, t in enumerate(tokens):
        trial = " ".join(tokens[j] for j in lines[-1] + [i])
        if lines[-1] and font.getlength(trial) > max_w:
            lines.append([i])
        else:
            lines[-1].append(i)
    return lines


def render_caption(tokens: list[str], active: int | None, size: int = 84) -> Image.Image:
    img = Image.new("RGBA", (W, CAP_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(size)
    while size > 48:
        lines = _layout_lines(tokens, font, W - 120)
        if len(lines) <= 2:
            break
        size -= 8
        font = _font(size)
    lines = _layout_lines(tokens, font, W - 120)
    line_h = int(size * 1.18)
    y = (CAP_H - line_h * len(lines)) // 2
    stroke = max(5, size // 11)
    space = font.getlength(" ")
    for line in lines:
        widths = [font.getlength(tokens[i]) for i in line]
        x = (W - (sum(widths) + space * (len(line) - 1))) / 2
        for i, wdt in zip(line, widths):
            fill = (255, 214, 10, 255) if i == active else (255, 255, 255, 255)
            # drop shadow, then outlined text
            d.text((x + 4, y + 6), tokens[i], font=font, fill=(0, 0, 0, 140), stroke_width=stroke,
                   stroke_fill=(0, 0, 0, 140))
            d.text((x, y), tokens[i], font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            x += wdt + space
        y += line_h
    return img


def build_caption_track(words: list[dict], duration: float, workdir: Path) -> Path | None:
    """Write PNGs + an ffconcat file covering [0, duration]. Returns the concat path."""
    groups = group_words(words)
    if not groups:
        return None
    workdir.mkdir(parents=True, exist_ok=True)
    blank = workdir / "blank.png"
    Image.new("RGBA", (W, CAP_H), (0, 0, 0, 0)).save(blank)
    entries: list[tuple[Path, float]] = []
    t = 0.0
    n = 0
    for gi, g in enumerate(groups):
        g_start = max(t, g[0]["start"])
        nxt = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else duration
        g_end = min(duration, max(g[-1]["end"] + 0.15, g[-1]["end"]))
        if nxt - g_end < 0.35:  # avoid flicker between close groups
            g_end = nxt
        if g_start - t > 0.01:
            entries.append((blank, g_start - t))
        tokens = [w["word"].upper() for w in g]
        for wi, w in enumerate(g):
            s = max(g_start, w["start"]) if wi else g_start
            e = g[wi + 1]["start"] if wi + 1 < len(g) else g_end
            if e - s <= 0.02:
                continue
            p = workdir / f"cap_{n:05d}.png"
            render_caption(tokens, wi).save(p, optimize=False, compress_level=1)
            entries.append((p, e - s))
            n += 1
        t = max(t, g_end)
        if t >= duration:
            break
    if duration - t > 0.01:
        entries.append((blank, duration - t))
    lst = workdir / "captions.ffconcat"
    with lst.open("w") as f:
        f.write("ffconcat version 1.0\n")
        for p, dur in entries:
            f.write(f"file '{p.name}'\nduration {dur:.3f}\n")
        f.write(f"file '{entries[-1][0].name}'\n")  # concat demuxer quirk: repeat the last frame
    return lst


def render_pill(text: str, size: int = 40, label: str | None = None) -> Image.Image:
    font = _font(size)
    small = _font(int(size * 0.62))
    tw = font.getlength(text)
    lw = small.getlength(label) + 24 if label else 0
    pad = 28
    h = int(size * 1.7)
    w = int(tw + lw + pad * 2)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(10, 10, 14, 185))
    x = pad
    if label:
        d.text((x, (h - small.size) / 2 - 2), label, font=small, fill=(255, 214, 10, 255))
        x += lw
    d.text((x, (h - size) / 2 - 3), text, font=font, fill=(255, 255, 255, 255))
    return img


def render_overlay(credit: str | None, brand: str | None, demo: bool, out: Path, height: int = 1920) -> Path:
    """Full-frame static overlay: streamer credit (top), small brand tag, DEMO banner if needed."""
    img = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    # top-left, below the platforms' top bar: keeps the streamer's (usually centred) face clear
    y = 190
    if credit:
        pill = render_pill(credit, 36, "CLIP FROM")
        if pill.width > W - 80:
            pill = pill.resize((W - 80, int(pill.height * (W - 80) / pill.width)))
        img.alpha_composite(pill, (40, y))
        y += pill.height + 10
    if brand:
        tag = render_pill(brand, 24)
        img.alpha_composite(tag, (40, y))
    if demo:
        d = ImageDraw.Draw(img)
        d.rectangle((0, 40, W, 130), fill=(200, 20, 40, 230))
        f = _font(44)
        msg = "DEMO FOOTAGE - NOT FOR PUBLISHING"
        d.text(((W - f.getlength(msg)) / 2, 62), msg, font=f, fill=(255, 255, 255, 255))
    img.save(out)
    return out
