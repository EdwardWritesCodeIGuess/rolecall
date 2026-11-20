"""Pre-publish checks. Every clip gets flags; blocking flags force human review.

Severity:
  block  - never publishes until a human resolves it (rights, demo footage, PII, slurs)
  review - needs a human look unless the creator is trusted (music, low caption confidence)
  info   - advisory only (monetization risk notes)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import numpy as np

from . import db

PII_PATTERNS = {
    "phone number": re.compile(r"\b(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    "email address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "street address": re.compile(
        r"\b\d{2,5}\s+\w+(\s\w+)?\s+(street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct)\b", re.I),
    "IP address": re.compile(r"\b(\d{1,3}\.){3}\d{1,3}\b"),
    "real-name disclosure": re.compile(r"\bmy (real|actual|irl) name\b|\bwhere i live\b|\bmy home address\b", re.I),
    "payment card / SSN": re.compile(r"\b(\d{4}[\s-]?){3}\d{4}\b|\b\d{3}-\d{2}-\d{4}\b"),
}
SLURS = re.compile(r"\bn[i1!]gg(a|er|uh)s?\b|\bf[a@]gg?(ot|it)?s?\b|\br[e3]t[a@]rd(ed|s)?\b|\btr[a@]nn(y|ies)\b|"
                   r"\bk[i1]ke\b|\bch[i1]nk\b|\bsp[i1]c\b", re.I)
EARLY_VIOLENCE = re.compile(r"\b(shoot|shot|shots|kill|killed|dead|blood|stab|gun|headshot|execute)\b", re.I)


def flag(code: str, severity: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "detail": detail}


def permission_for(creator_id: int) -> dict | None:
    row = db.q1("SELECT * FROM permissions WHERE creator_id=? ORDER BY "
                "CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC LIMIT 1", (creator_id,))
    return dict(row) if row else None


def permission_flags(creator_id: int, platforms: list[str]) -> tuple[list[dict], list[str]]:
    """Return (flags, platforms_allowed)."""
    p = permission_for(creator_id)
    if not p:
        return [flag("no_permission", "block", "No permission record for this creator.")], []
    flags: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    if p["status"] != "active":
        flags.append(flag("permission_inactive", "block", f"Permission is '{p['status']}'."))
    if not p["verified"]:
        flags.append(flag("permission_unverified", "block", "Grantor not yet verified as the channel owner."))
    if p["expires_at"] and p["expires_at"] < now:
        flags.append(flag("permission_expired", "block", f"Expired {p['expires_at']}."))
    for need, label in (("allow_edit", "editing"), ("allow_repost", "reposting"), ("allow_monetize", "monetizing")):
        if not p[need]:
            flags.append(flag(f"no_{need[6:]}", "block", f"Permission does not cover {label}."))
    allowed = db.jload(p["platforms"], [])
    missing = [x for x in platforms if x not in allowed]
    if missing:
        flags.append(flag("platform_not_covered", "info", f"Not covered, will be skipped: {', '.join(missing)}."))
    return flags, [x for x in platforms if x in allowed]


def content_flags(text: str, words: list[dict], duration: float, loud_db: np.ndarray | None,
                  flatness: np.ndarray | None, transcript_segments: list[dict],
                  rendered_duration: float | None = None) -> list[dict]:
    """`words`/`duration` describe the source window; `rendered_duration` is after dead-air removal."""
    flags: list[dict] = []
    for label, rx in PII_PATTERNS.items():
        m = rx.search(text)
        if m:
            flags.append(flag("pii_suspected", "block", f"Possible {label} in speech: \"{m.group(0)[:40]}\"."))
    if SLURS.search(text):
        flags.append(flag("slur", "block", "Slur detected in speech: brand-safety and hate-speech risk."))
    if not words:
        flags.append(flag("no_speech", "review", "No speech detected: captions are empty, check the clip still lands."))
    else:
        probs = [w.get("prob", 1.0) for w in words]
        if float(np.mean(probs)) < 0.6:
            flags.append(flag("low_caption_confidence", "review",
                              f"Mean word confidence {np.mean(probs):.2f}: captions may be wrong."))
    music_marks = sum(1 for s in transcript_segments if re.search(r"♪|\[music\]|\(music\)|♫", s.get("text", ""), re.I))
    if music_marks:
        flags.append(flag("music_suspected", "review", "Transcriber marked music. Copyrighted music can get the post muted, claimed or removed."))
    elif loud_db is not None and flatness is not None:
        # energetic, tonal audio with no speech for >4s is a weak music signal
        speech = np.zeros(int(duration / 0.5) + 1)
        for w in words:
            speech[int(w["start"] / 0.5):int(w["end"] / 0.5) + 1] = 1
        n = min(len(flatness), len(speech))
        tonal = (flatness[:n] < 0.08) & (speech[:n] == 0)
        run_len, best = 0, 0
        for v in tonal:
            run_len = run_len + 1 if v else 0
            best = max(best, run_len)
        if best * 0.5 >= 4:
            flags.append(flag("music_suspected", "review", f"{best * 0.5:.0f}s of tonal audio with no speech: possible background music."))
    early = " ".join(w["word"] for w in words if w["start"] < 7.5)
    if EARLY_VIOLENCE.search(early):
        flags.append(flag("early_violence", "info",
                          "Violence words in the first 7s: YouTube limits ads on graphic violence in the opening seconds."))
    final = rendered_duration if rendered_duration is not None else duration
    if final < 8:
        flags.append(flag("too_short", "review", f"{final:.1f}s may be too short to stand alone."))
    if final > 180:
        flags.append(flag("too_long_for_shorts", "block", "Over 3 minutes is not a YouTube Short."))
    return flags


def decide(flags: list[dict], creator_id: int, is_demo: bool) -> str:
    """Return review status for a freshly rendered clip."""
    if is_demo:
        return "needs_review"
    if any(f["severity"] == "block" for f in flags):
        return "needs_review"
    creator = db.q1("SELECT auto_approve FROM creators WHERE id=?", (creator_id,))
    trusted_after = int(db.get_setting("trusted_after") or 3)
    approved = db.q1("SELECT COUNT(*) n FROM clips WHERE creator_id=? AND review_status='approved'", (creator_id,))["n"]
    trusted = bool(creator and creator["auto_approve"]) or approved >= trusted_after
    if not trusted:
        return "needs_review"
    if any(f["severity"] == "review" for f in flags):
        return "needs_review"
    return "auto_approved"
