"""9:16 rendering: tighten dead air, reframe around the action, clean audio, burn captions + credit."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import captions as cap
from .media import FFMPEG, load_audio, loudness_curve, motion_track, probe, run

OUT_W, OUT_H = 1080, 1920

ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high",
          "-r", "30", "-g", "60", "-keyint_min", "30", "-flags", "+cgop", "-maxrate", "14M", "-bufsize", "28M",
          "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart"]

AUDIO_CHAIN = ("highpass=f=70,afftdn=nf=-25,"
               "acompressor=threshold=-18dB:ratio=3:attack=10:release=200,"
               "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000")


@dataclass
class RenderSpec:
    source: Path
    start: float
    end: float
    words: list[dict]                 # word timings relative to `start`
    layout: str = "auto"              # auto|blur|split|crop
    facecam_box: dict | None = None   # {x,y,w,h} fractions of the source frame
    credit: str | None = None
    brand: str | None = None
    demo: bool = False
    tighten: bool = True


def keep_segments(words: list[dict], duration: float, loud_db=None, hop: float = 0.1, max_gap: float = 0.8,
                  pad: float = 0.25, keep_gap: float = 0.3) -> list[tuple[float, float]]:
    """Segments to keep. A stretch is only cut when it has no transcribed words AND is actually quiet,
    so laughter, shouting and crashes the transcriber missed are never removed. Long quiet gaps are
    shortened to `keep_gap`, not removed entirely, so pacing still feels natural."""
    import numpy as np

    n = max(1, int(np.ceil(duration / hop)))
    active = np.zeros(n, dtype=bool)
    for w in words:
        active[max(0, int((w["start"] - pad) / hop)):min(n, int((w["end"] + pad) / hop) + 1)] = True
    if loud_db is not None and len(loud_db):
        lv = np.asarray(loud_db[:n], dtype=float)
        floor = float(np.percentile(lv, 10))
        thresh = max(-50.0, floor + 9.0)
        loud = np.zeros(n, dtype=bool)
        loud[:len(lv)] = lv > thresh
        active |= loud
    else:
        active[:] = True  # no audio info: never cut
    segs: list[list[float]] = []
    i = 0
    while i < n:
        if active[i]:
            j = i
            while j < n and active[j]:
                j += 1
            segs.append([i * hop, min(duration, j * hop)])
            i = j
        else:
            j = i
            while j < n and not active[j]:
                j += 1
            gap = (j - i) * hop
            if gap <= max_gap or i == 0 or j >= n:  # short pause, cold open or tail: keep as is
                segs.append([i * hop, min(duration, j * hop)])
            else:
                mid = i * hop + keep_gap / 2
                segs.append([i * hop, mid])
                segs.append([min(duration, j * hop) - keep_gap / 2, min(duration, j * hop)])
            i = j
    merged: list[list[float]] = []
    for a, b in segs:
        if merged and abs(a - merged[-1][1]) < 1e-6:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a > 0.05]


def remap(t: float, segs: list[tuple[float, float]]) -> float | None:
    acc = 0.0
    for a, b in segs:
        if a <= t <= b:
            return acc + (t - a)
        acc += b - a
    return None


def _cut(spec: RenderSpec, work: Path) -> tuple[Path, list[dict], float]:
    """Stage 1: frame-accurate cut + dead-air removal into a high-quality intermediate."""
    dur = spec.end - spec.start
    info = probe(spec.source)
    if spec.tighten and info["has_audio"]:
        audio = load_audio(spec.source, start=spec.start, duration=dur)
        segs = keep_segments(spec.words, dur, loud_db=loudness_curve(audio, hop=0.1))
    else:
        segs = [(0.0, dur)]
    out = work / "cut.mp4"
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{spec.start:.3f}", "-t", f"{dur:.3f}",
           "-i", str(spec.source)]
    if not info["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    a_in = "0:a" if info["has_audio"] else "1:a"
    parts, labels = [], []
    for i, (a, b) in enumerate(segs):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[{a_in}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(segs)}:v=1:a=1[v][a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le", str(out.with_suffix(".mov"))]
    run(cmd)
    out = out.with_suffix(".mov")
    new_words = []
    for w in spec.words:
        s, e = remap(w["start"], segs), remap(w["end"], segs)
        if s is None:
            continue
        new_words.append({**w, "start": s, "end": e if e is not None else s + 0.25})
    new_dur = sum(b - a for a, b in segs)
    return out, new_words, new_dur


def _crop_bounds(scaled_w: int, facecam: dict | None) -> tuple[int, int]:
    """Allowed [min_x, max_x] for the crop's left edge, keeping the facecam overlay out of the gameplay crop."""
    lo, hi = 0, scaled_w - OUT_W
    if facecam:
        fx0, fx1 = facecam["x"] * scaled_w, (facecam["x"] + facecam["w"]) * scaled_w
        if facecam["x"] + facecam["w"] / 2 >= 0.5:      # facecam on the right: crop must end before it
            hi = min(hi, int(fx0) - OUT_W)
        else:                                            # facecam on the left: crop must start after it
            lo = max(lo, int(fx1))
        if hi < lo:                                      # not enough room: fall back to the centre
            lo = hi = (scaled_w - OUT_W) // 2
    return lo, hi


def _x_expr(track: list[tuple[float, float]], scaled_w: int, bounds: tuple[int, int] | None = None) -> str:
    """Piecewise-linear crop x(t) expression from (t, centre-fraction) keyframes."""
    lo, hi = bounds or (0, scaled_w - OUT_W)

    def px(c: float) -> int:
        return int(min(hi, max(lo, c * scaled_w - OUT_W / 2)))

    max_x = hi

    pts = [(t, px(c)) for t, c in track[::2]] or [(0.0, px(0.5) if max_x >= 0 else 0)]  # keyframe every ~2s
    if len(pts) == 1:
        return str(pts[0][1])
    expr = str(pts[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(pts[:-1], pts[1:]))):
        expr = f"if(lt(t,{t1:.2f}),{x0}+({x1 - x0})*(t-{t0:.2f})/{max(0.01, t1 - t0):.2f},{expr})"
    return expr


def _layout_graph(layout: str, src_w: int, src_h: int, facecam: dict | None, track) -> tuple[str, int]:
    """Return (filter for [0:v] -> [base], caption top y)."""
    if layout == "split" and facecam:
        fx, fy = int(facecam["x"] * src_w), int(facecam["y"] * src_h)
        fw, fh = int(facecam["w"] * src_w), int(facecam["h"] * src_h)
        top_h, bot_h = 680, OUT_H - 680
        scaled_w = int(round(src_w * bot_h / src_h / 2) * 2)
        bounds = _crop_bounds(scaled_w, facecam)
        x = _x_expr(track or [(0.0, 0.5)], scaled_w, bounds)
        g = (f"[0:v]split[fc][gp];"
             f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={OUT_W}:{top_h}:force_original_aspect_ratio=increase,"
             f"crop={OUT_W}:{top_h}[top];"
             f"[gp]scale={scaled_w}:{bot_h},crop={OUT_W}:{bot_h}:x='{x}':y=0[bot];"
             f"[top][bot]vstack[base]")
        return g, top_h - cap.CAP_H // 2
    if layout == "crop":
        scaled_w = int(round(src_w * OUT_H / src_h / 2) * 2)
        x = _x_expr(track or [(0.0, 0.5)], scaled_w, _crop_bounds(scaled_w, facecam))
        return f"[0:v]scale={scaled_w}:{OUT_H},crop={OUT_W}:{OUT_H}:x='{x}':y=0[base]", 1060
    # blur: full action visible, zoomed 1.3x, over a blurred fill
    zoom_w = int(OUT_W * 1.3 // 2 * 2)
    fg_h = int(round(zoom_w * src_h / src_w / 2) * 2)
    y = 430
    g = (f"[0:v]split[bgs][fgs];"
         f"[bgs]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
         f"gblur=sigma=28,eq=brightness=-0.15:saturation=1.2[bg];"
         f"[fgs]scale={zoom_w}:{fg_h},crop={OUT_W}:{fg_h}[fg];"
         f"[bg][fg]overlay=0:{y}[base]")
    return g, min(OUT_H - cap.CAP_H - 380, y + fg_h - cap.CAP_H // 2)


def render(spec: RenderSpec, out: Path, work: Path, delivery_out: Path | None = None) -> dict:
    """Render the branded clip (and an unbranded client copy if delivery_out is set)."""
    t0 = time.time()
    work.mkdir(parents=True, exist_ok=True)
    cut, words, dur = _cut(spec, work)
    info = probe(cut)
    src_w, src_h = info["width"], info["height"]
    layout = spec.layout
    if layout == "auto":
        layout = "split" if spec.facecam_box else "blur"
    track = None
    if layout in {"split", "crop"}:
        track = motion_track(cut, 0, dur, src_w, src_h, exclude=spec.facecam_box)
    graph, cap_y = _layout_graph(layout, src_w, src_h, spec.facecam_box, track)
    cap_list = cap.build_caption_track(words, dur, work / "caps")

    def encode(target: Path, credit: str | None, brand: str | None) -> None:
        ovl = cap.render_overlay(credit, brand, spec.demo, work / f"overlay_{target.stem}.png")
        cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(cut), "-loop", "1", "-i", str(ovl)]
        g = graph + ";[base][1:v]overlay=0:0:shortest=1[v1]"
        if cap_list:
            cmd += ["-f", "concat", "-safe", "0", "-i", str(cap_list)]
            g += f";[2:v]fps=30,format=rgba[cap];[v1][cap]overlay=0:{cap_y}:eof_action=pass[v2]"
            vout = "[v2]"
        else:
            vout = "[v1]"
        g += f";{vout}fps=30,format=yuv420p[vout];[0:a]{AUDIO_CHAIN}[aout]"
        cmd += ["-filter_complex", g, "-map", "[vout]", "-map", "[aout]", "-t", f"{dur:.3f}", *ENCODE, str(target)]
        run(cmd)

    out.parent.mkdir(parents=True, exist_ok=True)
    encode(out, spec.credit, spec.brand)
    if delivery_out:
        delivery_out.parent.mkdir(parents=True, exist_ok=True)
        encode(delivery_out, None, None)
    shutil.rmtree(work, ignore_errors=True)
    return {"duration": round(dur, 2), "layout": layout, "render_seconds": round(time.time() - t0, 2),
            "words": words}
