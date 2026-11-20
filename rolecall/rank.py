"""Find and score standalone moments: immediate hook, enough context, clear payoff.

Signals are free and local: loudness spikes (shouting, crashes, gunfire), laughter, surprise
phrases, RP-specific vocabulary (cops, court, heists), punctuation from the transcript, and
speech coverage. Optionally reranked by an LLM (see copywriter.rerank) when one is configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

HOP = 0.25

FORMATS = {
    # name: (min_s, max_s, target_s)
    "punch": (12.0, 35.0, 24.0),
    "story": (35.0, 60.0, 45.0),
    "long": (61.0, 90.0, 70.0),   # TikTok Creator Rewards only pays on videos over one minute
}

LAUGH = re.compile(r"\b(ha([\s-]?ha)+|he([\s-]?he)+|lol|lmao|lmfao|laugh\w*|dying|crying)\b", re.I)
SCENE_BREAK_S = 10.0  # no speech and no loud audio for this long = a different scene
SURPRISE = re.compile(
    r"\b(what|no way|oh my god|omg|bro+|dude|wait|yo+|holy|wtf|what the|are you serious|no no|"
    r"let'?s go+|oh no|run|go go|get out|shut up|stop|why|how)\b", re.I)
RP = re.compile(
    r"\b(officer|cops?|police|pd|ems|cuff\w*|arrest\w*|jail|prison|judge|court|lawyer|warrant|chase|"
    r"pursuit|heist|bank|robb\w*|hostage|licen[cs]e|ticket|mayor|gang|tow|mechanic|taxi|wanted|"
    r"evidence|interrogat\w*|felony|misdemeanor|plates?|sheriff|trooper|bail|sentence|dispatch)\b", re.I)


@dataclass
class Moment:
    start: float
    end: float
    fmt: str
    score: float
    hook: float
    context: float
    payoff: float
    reasons: list[str] = field(default_factory=list)
    text: str = ""


def _robust_z(x: np.ndarray) -> np.ndarray:
    active = x[x > -60]
    if len(active) < 8:
        active = x
    med = np.median(active)
    mad = np.median(np.abs(active - med)) * 1.4826 + 1e-6
    return (x - med) / mad


def _sig(x: float) -> float:
    return float(1 / (1 + np.exp(-x)))


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(x) < k:
        return x
    return np.convolve(np.pad(x, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")


def _cue_curve(transcript: dict, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-hop arrays: cue intensity, laughter, speech presence."""
    cue = np.zeros(n)
    laugh = np.zeros(n)
    speech = np.zeros(n)
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            i0, i1 = int(w["start"] / HOP), max(int(w["start"] / HOP) + 1, int(w["end"] / HOP))
            speech[i0:min(i1, n)] = 1
            tok = w["word"]
            i = min(i0, n - 1) if n else 0
            if n == 0:
                continue
            if LAUGH.search(tok):
                laugh[i] += 1.0
                cue[i] += 1.2
            if SURPRISE.search(tok):
                cue[i] += 0.6
            if RP.search(tok):
                cue[i] += 0.4
            if "!" in tok:
                cue[i] += 0.7
            if "?" in tok:
                cue[i] += 0.4
        # multi-word phrases are matched at segment level
        txt = seg.get("text", "")
        if n and re.search(r"no way|oh my god|what the|are you serious|let'?s go|oh no|get out", txt, re.I):
            i = min(int(seg["start"] / HOP), n - 1)
            cue[i] += 0.8
    return cue, laugh, speech


def find_moments(transcript: dict, loud_db: np.ndarray, duration: float,
                 formats: list[str] | None = None, max_per_format: int = 6) -> list[Moment]:
    formats = formats or ["punch", "story"]
    n = len(loud_db)
    if n < 8:
        return []
    zl = _smooth(_robust_z(loud_db), 3)
    cue, laugh, speech = _cue_curve(transcript, n)
    cue_s = _smooth(cue, 8)  # ~2s window
    intensity = 0.6 * np.clip(zl, -3, 6) + 1.2 * cue_s
    segs = [s for s in transcript.get("segments", []) if s.get("words")]
    seg_starts = np.array([s["start"] for s in segs]) if segs else np.array([])
    seg_ends = np.array([s["end"] for s in segs]) if segs else np.array([])

    # payoff candidates: local maxima of intensity, suppressed within 8s
    order = np.argsort(-intensity)
    peaks: list[int] = []
    for i in order:
        if intensity[i] < 0.8 or len(peaks) >= 60:
            break
        if all(abs(i - p) * HOP > 8 for p in peaks):
            peaks.append(int(i))

    def window_scores(s: float, e: float, peak_t: float) -> tuple[float, float, float, list[str]]:
        i0, i1 = int(s / HOP), max(int(s / HOP) + 1, int(e / HOP))
        reasons = []
        hook_slice = intensity[i0:i0 + int(3 / HOP)]
        first_word = next((float(w) for w in seg_starts if w >= s - 0.05), e)
        silent_open = first_word - s
        hook_raw = float(np.mean(hook_slice)) if len(hook_slice) else 0.0
        hook_raw += 0.8 * float(cue[i0:i0 + int(3 / HOP)].sum() > 0)
        hook_raw -= 1.5 * max(0.0, silent_open - 0.8)
        if silent_open > 1.5:
            reasons.append("slow open")
        if cue[i0:i0 + int(3 / HOP)].sum() > 0:
            reasons.append("hook line in first 3s")
        cover = float(speech[i0:i1].mean()) if i1 > i0 else 0.0
        # longest dead-air gap: no words and no loud audio
        lively = (speech[i0:i1] > 0) | (zl[i0:i1] > 1.0)
        gaps = np.diff(np.flatnonzero(np.r_[1, lively, 1]))
        dead = float(gaps.max() * HOP) if len(gaps) else 0.0
        starts_clean = bool(len(seg_starts) and np.min(np.abs(seg_starts - s)) < 0.15)
        context_raw = 3.0 * (cover - 0.45) + (0.6 if starts_clean else -0.4) - 0.5 * max(0.0, dead - 3.0)
        if dead > 4:
            reasons.append(f"{dead:.0f}s dead air")
        if starts_clean:
            reasons.append("starts on a full sentence")
        tail0 = i0 + int((i1 - i0) * 0.6)
        tail = intensity[tail0:i1]
        payoff_raw = float(tail.max()) if len(tail) else 0.0
        if laugh[tail0:min(i1 + int(3 / HOP), n)].sum() > 0:
            payoff_raw += 1.0
            reasons.append("laughter at the end")
        if RP.search(" ".join(s2["text"] for s2 in segs if s <= s2["start"] < e)):
            reasons.append("RP scenario")
        if peak_t - s < (e - s) * 0.45:
            payoff_raw -= 0.8  # peak too early: the clip drags after the punchline
        # ending: reward landing on a laugh or a natural pause, punish cutting someone off
        nxt = seg_starts[seg_starts > e - 0.05] if len(seg_starts) else np.array([])
        gap_after = float(nxt[0] - e) if len(nxt) else 5.0
        end_seg = next((s2 for s2 in reversed(segs) if s2["end"] <= e + 0.05), None)
        if end_seg and LAUGH.search(end_seg["text"]):
            payoff_raw += 0.6
        if gap_after < 0.3:
            payoff_raw -= 0.7
            reasons.append("cuts into the next line")
        elif gap_after > 0.8:
            payoff_raw += 0.3
        return _sig(hook_raw - 0.5), _sig(context_raw), _sig(payoff_raw - 1.0), reasons, dead

    results: list[Moment] = []
    for fmt in formats:
        lo, hi, target = FORMATS[fmt]
        if duration < lo:
            continue
        cands: list[Moment] = []
        for p in peaks:
            pt = p * HOP
            # end options: sentence ends 1-14s after the peak, so the reaction and punchline land
            ends = [min(duration, pt + 2.0)]
            if len(seg_ends):
                ends = [min(duration, float(x) + 0.35) for x in seg_ends if pt + 1.0 <= x <= pt + 14.0][:6] or ends
            best = None
            for e in ends:
                # start options: sentence starts within the format range
                options = [float(x) - 0.1 for x in seg_starts if lo <= e - x <= hi] if len(seg_starts) else []
                if not options:
                    options = [max(0.0, e - target)]
                for s in options:
                    s = max(0.0, s)
                    if e - s < lo * 0.9:
                        continue
                    h, c, pay, why, dead = window_scores(s, e, pt)
                    if dead >= SCENE_BREAK_S:
                        continue  # spans two scenes: never a standalone clip
                    closeness = 1 - min(1.0, abs((e - s) - target) / target)
                    total = 0.35 * h + 0.25 * c + 0.40 * pay + 0.05 * closeness
                    if dead > 5:
                        total *= 0.8
                    if best is None or total > best.score:
                        best = Moment(s, e, fmt, round(total, 4), round(h, 3), round(c, 3), round(pay, 3), why)
            if best:
                cands.append(best)
        cands.sort(key=lambda m: -m.score)
        kept: list[Moment] = []
        for m in cands:
            if all(_iou(m, k) < 0.3 for k in kept):
                kept.append(m)
            if len(kept) >= max_per_format:
                break
        results.extend(kept)
    # cross-format: drop near-duplicates (a punch cut that sits inside a story cut)
    results.sort(key=lambda m: -m.score)
    final: list[Moment] = []
    for m in results:
        if all(_iou(m, k) < 0.5 for k in final):
            final.append(m)
    return final


def _iou(a: Moment, b: Moment) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    union = max(a.end, b.end) - min(a.start, b.start)
    return inter / union if union > 0 else 0.0
