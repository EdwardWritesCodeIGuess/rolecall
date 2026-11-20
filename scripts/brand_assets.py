"""Generate Rolecall profile image (1080x1080, circle-safe) and YouTube banner (2560x1440)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = str(ROOT / "assets" / "fonts" / "ArchivoBlack-Regular.ttf")
OUT = ROOT / "assets" / "brand"
YELLOW, INK, NIGHT = (255, 200, 20), (18, 18, 22), (16, 17, 22)


def font(n):
    return ImageFont.truetype(FONT, n)


def centered(d, y, text, f, fill, W):
    d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=fill)


def profile():
    W = 1080
    img = Image.new("RGB", (W, W), YELLOW)
    d = ImageDraw.Draw(img)
    # clapperboard stripe across the top of the circle-safe area
    for i in range(-2, 12):
        x = i * 120
        d.polygon([(x, 250), (x + 60, 250), (x + 140, 330), (x + 80, 330)], fill=INK)
    d.rectangle((0, 250, W, 254), fill=INK)
    d.rectangle((0, 326, W, 330), fill=INK)
    centered(d, 380, "RC", font(380), INK, W)
    centered(d, 800, "ROLECALL", font(84), INK, W)
    img.save(OUT / "profile_1080.png")


def banner():
    W, H = 2560, 1440
    img = Image.new("RGB", (W, H), NIGHT)
    d = ImageDraw.Draw(img)
    for i in range(0, W, 160):  # subtle stripes
        d.polygon([(i, 0), (i + 60, 0), (i - 340, H), (i - 400, H)], fill=(22, 23, 30))
    # everything important inside the 1546x423 safe area
    d.rounded_rectangle((W / 2 - 700, 560, W / 2 - 560, 700), radius=24, fill=YELLOW)
    d.text((W / 2 - 690, 572), "RC", font=font(88), fill=INK)
    d.text((W / 2 - 520, 560), "ROLECALL", font=font(132), fill=(245, 245, 245))
    centered(d, 735, "GTA RP moments, credited to the streamers who made them", font(46), YELLOW, W)
    img.save(OUT / "banner_2560x1440.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    profile()
    banner()
    print("wrote", sorted(p.name for p in OUT.iterdir()))
