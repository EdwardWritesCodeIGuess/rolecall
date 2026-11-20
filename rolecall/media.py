"""FFmpeg/ffprobe helpers: probing, audio features, motion tracking, thumbnails."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-1500:]
        raise MediaError(f"{Path(cmd[0]).name} failed ({proc.returncode}): {tail}")
    return proc


def probe(path: str | Path) -> dict:
    out = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    info = json.loads(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else None
    return {
        "duration": float(info.get("format", {}).get("duration") or 0),
        "width": int(v["width"]) if v else None,
        "height": int(v["height"]) if v else None,
        "fps": fps,
        "has_video": v is not None,
        "has_audio": a is not None,
    }


def sha256_file(path: str | Path, limit_mb: int = 64) -> str:
    """Hash of the first/last chunk + size: fast dedupe for multi-GB VODs."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    chunk = limit_mb * 1024 * 1024 // 2
    with p.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(-chunk, 2)
            h.update(f.read(chunk))
    return h.hexdigest()


def load_audio(path: str | Path, sr: int = 16000, start: float | None = None, duration: float | None = None) -> np.ndarray:
    cmd = [FFMPEG, "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    out = run(cmd).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def loudness_curve(audio: np.ndarray, sr: int = 16000, hop: float = 0.25) -> np.ndarray:
    """RMS in dBFS per hop window."""
    n = int(sr * hop)
    if len(audio) < n:
        return np.array([-90.0])
    frames = len(audio) // n
    a = audio[: frames * n].reshape(frames, n)
    rms = np.sqrt(np.mean(a * a, axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9)


def spectral_flatness_curve(audio: np.ndarray, sr: int = 16000, hop: float = 0.5) -> np.ndarray:
    """Low flatness + steady energy without speech is a (weak) music signal."""
    n = int(sr * hop)
    frames = len(audio) // n
    if frames == 0:
        return np.array([1.0])
    a = audio[: frames * n].reshape(frames, n) * np.hanning(n)
    spec = np.abs(np.fft.rfft(a, axis=1)) + 1e-10
    geo = np.exp(np.mean(np.log(spec), axis=1))
    return geo / np.mean(spec, axis=1)


def motion_track(path: str | Path, start: float, duration: float, src_w: int, src_h: int,
                 exclude: dict | None = None, fps: int = 4) -> list[tuple[float, float]]:
    """Return [(t, center_x_fraction)] following where on-screen motion is concentrated.

    Uses frame differencing on a 160px-wide grayscale proxy. The facecam box (if known) is masked
    out so the crop follows gameplay, not the streamer's face.
    """
    w = 160
    h = max(2, int(round(src_h * w / src_w / 2)) * 2)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{duration:.3f}",
           "-vf", f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"]
    raw = run(cmd).stdout
    frames = np.frombuffer(raw, dtype=np.uint8)
    count = len(frames) // (w * h)
    if count < 2:
        return [(0.0, 0.5)]
    frames = frames[: count * w * h].reshape(count, h, w).astype(np.int16)
    mask = np.ones((h, w), dtype=bool)
    if exclude:
        x0, y0 = int(exclude["x"] * w), int(exclude["y"] * h)
        x1, y1 = int((exclude["x"] + exclude["w"]) * w), int((exclude["y"] + exclude["h"]) * h)
        mask[y0:y1, x0:x1] = False
    # ignore the outer 8% (HUD, minimap, chat overlays sit there)
    mask[: int(h * 0.08), :] = False
    mask[int(h * 0.92):, :] = False
    cols = np.arange(w)
    centers = []
    prev_c = 0.5
    for i in range(1, count):
        diff = np.abs(frames[i] - frames[i - 1]) * mask
        col_energy = diff.sum(axis=0).astype(np.float64)
        total = col_energy.sum()
        if total < 50 * h:  # nearly static: hold position
            c = prev_c
        else:
            c = float((col_energy * cols).sum() / total) / w
            c = 0.5 + (c - 0.5) * 0.8  # bias towards centre: GTA's chase cam keeps the player near it
        centers.append(c)
        prev_c = c
    arr = np.array(centers)
    # Smooth like a camera operator: ~2.5s moving average, then clamp speed.
    k = max(1, int(fps * 2.5))
    if len(arr) >= k:
        arr = np.convolve(np.pad(arr, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")
    out = []
    step = fps  # one keyframe per second
    for i in range(0, len(arr), step):
        out.append((i / fps, float(arr[i])))
    if not out:
        out = [(0.0, 0.5)]
    return out


def thumbnail(video: str | Path, out: str | Path, t: float = 1.0) -> Path:
    run([FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=540:-2", "-q:v", "4", str(out)])
    return Path(out)


def frame_png(video: str | Path, out: str | Path, t: float) -> Path:
    run([FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=960:-2", str(out)])
    return Path(out)
