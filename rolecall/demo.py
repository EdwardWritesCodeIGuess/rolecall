"""Clearly labeled synthetic demo footage for testing the pipeline before real, authorized GTA clips.

The video is generated (gradient "street", a moving "car", a cartoon "facecam" box and a big
DEMO banner) and the audio is an original scripted RP-style scene spoken by text-to-speech
(macOS `say`, or `espeak-ng` on Linux). Demo sources are flagged and can never be published.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import captions, db
from .media import FFMPEG, run

SR = 22050

# (speaker, line, pause_after_seconds). Speakers map to voices.
SCRIPT = [
    ("officer", "Sir, do you know why I pulled you over?", 0.5),
    ("driver", "Because you were lonely?", 0.6),
    ("officer", "No. You were doing ninety in a school zone.", 0.4),
    ("driver", "It's three in the morning! There are no kids out here!", 0.6),
    ("officer", "License and registration, please.", 1.2),
    ("driver", "Okay, okay. Here is my library card.", 0.8),
    ("officer", "This is a library card.", 0.3),
    ("driver", "It has my picture on it!", 1.5),
    ("officer", "Step out of the vehicle.", 0.4),
    ("driver", "Wait wait wait, I can explain!", 0.2),
    ("SFX", "engine", 1.0),
    ("officer", "He's running! Dispatch, suspect is fleeing. In a tow truck?", 0.4),
    ("driver", "Ha ha ha ha! You will never catch me!", 0.8),
    ("SFX", "crash", 1.2),
    ("driver", "Oh no. Oh no no no. Why is there a wall here?", 0.5),
    ("officer", "Are you serious right now?", 0.6),
    ("driver", "Officer, I would like to report a wall.", 0.3),
    ("laugh", "Ha ha ha ha ha ha!", 0.5),
    ("SFX", "ambient", 28.0),
    ("judge", "Order in the court. Mister Diaz, how do you plead?", 0.7),
    ("driver", "Not guilty, your honor. The wall came out of nowhere.", 0.6),
    ("judge", "That wall has been there for forty years.", 0.5),
    ("driver", "Then it should have moved by now!", 0.4),
    ("laugh", "Ha ha ha ha!", 0.8),
    ("judge", "Thirty days. And you are paying for the wall.", 0.6),
    ("driver", "Can I pay with my library card?", 0.4),
    ("officer", "No way. Bro. No way.", 0.3),
    ("laugh", "Ha ha ha ha ha!", 1.5),
    ("SFX", "ambient", 20.0),
    ("driver", "Anyway, I am going to get some food. Anybody want anything?", 0.8),
    ("officer", "I'm good. I'm going back on patrol.", 6.0),
]

MAC_VOICES = {"officer": "Daniel", "driver": "Reed (English (US))", "judge": "Samantha",
              "laugh": "Reed (English (US))"}
ESPEAK_VOICES = {"officer": "en-gb", "driver": "en-us+m3", "judge": "en-us+m7", "laugh": "en-us+m3"}


def _tts(text: str, speaker: str, out: Path) -> np.ndarray:
    if shutil.which("say"):
        aiff = out.with_suffix(".aiff")
        subprocess.run(["say", "-v", MAC_VOICES[speaker], "-r", "185", "-o", str(aiff), text], check=True)
        src = aiff
    elif shutil.which("espeak-ng"):
        wav = out.with_suffix(".wav")
        subprocess.run(["espeak-ng", "-v", ESPEAK_VOICES[speaker], "-s", "165", "-w", str(wav), text], check=True)
        src = wav
    else:
        raise RuntimeError("Demo needs macOS `say` or `espeak-ng` for speech.")
    raw = run([FFMPEG, "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def _sfx(kind: str, seconds: float, rng: np.random.Generator) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    if kind == "crash":
        env = np.exp(-t * 3.0)
        return (rng.standard_normal(n) * 0.9 * env).astype(np.float32)
    if kind == "engine":
        f = 60 + 90 * t / max(seconds, 0.1)
        return (0.35 * np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * np.minimum(1, t * 4)).astype(np.float32)
    return np.zeros(n, dtype=np.float32)  # ambient gap: room tone is added globally


def build_audio(work: Path) -> np.ndarray:
    rng = np.random.default_rng(7)
    parts = [np.zeros(int(1.0 * SR), dtype=np.float32)]
    for i, (spk, text, pause) in enumerate(SCRIPT):
        if spk == "SFX":
            parts.append(_sfx(text, pause, rng))
            continue
        voice = _tts(text, spk, work / f"line{i:02d}")
        gain = 1.6 if spk == "laugh" else (1.25 if text.endswith("!") else 1.0)
        parts.append(np.clip(voice * gain, -1, 1))
        parts.append(np.zeros(int(pause * SR), dtype=np.float32))
    audio = np.concatenate(parts)
    # game-ish room tone: filtered noise + low engine hum
    noise = rng.standard_normal(len(audio)).astype(np.float32)
    noise = np.convolve(noise, np.ones(40) / 40, mode="same") * 0.05
    hum = 0.01 * np.sin(2 * np.pi * 55 * np.arange(len(audio)) / SR)
    return np.clip(audio * 0.8 + noise + hum, -1, 1).astype(np.float32)


def _facecam_png(path: Path) -> None:
    img = Image.new("RGB", (480, 270), (40, 44, 60))
    d = ImageDraw.Draw(img)
    d.ellipse((170, 40, 310, 190), fill=(230, 190, 150))
    d.ellipse((205, 90, 225, 110), fill=(20, 20, 20))
    d.ellipse((255, 90, 275, 110), fill=(20, 20, 20))
    d.arc((200, 120, 280, 170), 20, 160, fill=(120, 30, 30), width=6)
    d.rectangle((150, 190, 330, 270), fill=(70, 90, 160))
    d.rectangle((0, 0, 479, 269), outline=(255, 214, 10), width=6)
    img.save(path)


def _label_png(path: Path) -> None:
    img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = captions._font(96)
    small = captions._font(44)
    msg = "DEMO FOOTAGE - NOT GTA"
    d.text(((1920 - f.getlength(msg)) / 2, 470), msg, font=f, fill=(255, 255, 255, 200),
           stroke_width=6, stroke_fill=(0, 0, 0, 200))
    sub = "Synthetic test scene for the Rolecall pipeline"
    d.text(((1920 - small.getlength(sub)) / 2, 600), sub, font=small, fill=(255, 214, 10, 220))
    d.rectangle((0, 820, 1920, 1080), fill=(60, 60, 66, 255))  # "road"
    img.save(path)


def make_demo(out: Path) -> Path:
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        audio = build_audio(work)
        wav = work / "demo.f32"
        wav.write_bytes(audio.tobytes())
        dur = len(audio) / SR
        face, label = work / "face.png", work / "label.png"
        _facecam_png(face)
        _label_png(label)
        g = (f"[0:v][1:v]overlay=0:0[a];"
             f"[a][2:v]overlay=x='mod(260*t,2200)-220':y='860+12*sin(3*t)'[b];"
             f"[b][3:v]overlay=1400:40[v]")
        run([FFMPEG, "-v", "error", "-y",
             "-f", "lavfi", "-i", f"gradients=s=1920x1080:c0=0x1d3b53:c1=0x0b0f14:speed=0.02:d={dur:.2f}:r=30",
             "-loop", "1", "-i", str(label),
             "-f", "lavfi", "-i", f"color=c=0xff7a00:s=220x110:d={dur:.2f}:r=30",
             "-loop", "1", "-i", str(face),
             "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(wav),
             "-filter_complex", g, "-map", "[v]", "-map", "4:a", "-t", f"{dur:.2f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", str(out)])
    return out


def ensure_demo_creator() -> int:
    row = db.q1("SELECT id FROM creators WHERE slug='demo-streamer'")
    if row:
        return row["id"]
    # facecam box matches the overlay position above: x=1400,y=40,w=480,h=270 on 1920x1080
    return db.ex("INSERT INTO creators(slug, display_name, primary_url, credit_text, layout, facecam_box, notes)"
                 " VALUES('demo-streamer','DemoStreamer (synthetic)','https://example.com/demo','demo: synthetic test',"
                 "'auto', ?, 'Synthetic demo creator. Never published.')",
                 ('{"x":0.729,"y":0.037,"w":0.25,"h":0.25}',))
