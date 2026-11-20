"""End-to-end orchestration: ingest -> transcribe -> rank -> render -> review -> queue."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from . import copywriter, db, events, media, review
from .config import get_config
from .rank import FORMATS, find_moments
from .render import RenderSpec, render
from .transcribe import transcribe, words_in

log = logging.getLogger(__name__)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".flv", ".ts", ".m4v"}


class PermissionDenied(Exception):
    pass


class StillLive(Exception):
    """The stream is still in progress: clip it once the VOD is finished."""


LIVE_STATES = {"is_live", "is_upcoming", "post_live"}


# ---------------------------------------------------------------- permissions
def norm_channel(url: str) -> str:
    u = url.strip().lower()
    if "://" not in u:
        u = "https://" + u
    p = urlparse(u)
    host = re.sub(r"^(www\.|m\.)", "", p.netloc)
    return f"{host}{p.path.rstrip('/')}"


def active_permission(creator_id: int) -> dict | None:
    p = review.permission_for(creator_id)
    if p and p["status"] == "active" and p["verified"]:
        return p
    return None


def url_is_covered(perm: dict, urls: list[str]) -> bool:
    allowed = [norm_channel(c) for c in db.jload(perm["source_channels"], [])]
    for u in urls:
        if not u:
            continue
        n = norm_channel(u)
        if any(n == a or n.startswith(a + "/") for a in allowed):
            return True
    return False


# ---------------------------------------------------------------- ingest
def _store(creator_slug: str, src: Path, move: bool) -> Path:
    dest_dir = get_config().dir("sources", creator_slug)
    digest = media.sha256_file(src)
    dest = dest_dir / f"{digest[:16]}{src.suffix.lower()}"
    if not dest.exists():
        (shutil.move if move else shutil.copy2)(str(src), dest)
    elif move:
        src.unlink(missing_ok=True)
    return dest


def ingest_file(creator_id: int, path: str | Path, kind: str = "upload", origin: str | None = None,
                title: str | None = None, is_demo: bool = False, move: bool = False) -> int:
    creator = db.q1("SELECT * FROM creators WHERE id=?", (creator_id,))
    if not creator:
        raise ValueError(f"Unknown creator {creator_id}")
    perm = active_permission(creator_id)
    if not perm and not is_demo:
        raise PermissionDenied(f"{creator['display_name']} has no active, verified permission. "
                               "Record and verify one before ingesting their footage.")
    path = Path(path)
    sha = media.sha256_file(path)
    dup = db.q1("SELECT id FROM sources WHERE sha256=?", (sha,))
    if dup:
        if move:
            path.unlink(missing_ok=True)
        return dup["id"]
    info = media.probe(path)
    if not info["has_audio"]:
        raise ValueError("Source has no audio track; moment detection needs audio.")
    stored = _store(creator["slug"], path, move)
    sid = db.ex(
        "INSERT INTO sources(creator_id, permission_id, kind, origin, title, path, duration, width, height, fps,"
        " sha256, is_demo, audio_only) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (creator_id, perm["id"] if perm else None, kind, origin, title or path.name, str(stored), info["duration"],
         info["width"], info["height"], info["fps"], sha, int(is_demo), int(not info["has_video"])))
    db.log_event("info", "ingest", f"Ingested {path.name} ({info['duration']:.0f}s)", "source", sid)
    return sid


def ingest_url(creator_id: int, url: str, kind: str = "url") -> int:
    """Download an authorized VOD/clip with yt-dlp. Long VODs: audio first, video sections later."""
    import yt_dlp

    perm = active_permission(creator_id)
    if not perm:
        raise PermissionDenied("No active, verified permission for this creator.")
    if db.q1("SELECT id FROM sources WHERE origin=?", (url,)):
        return db.q1("SELECT id FROM sources WHERE origin=?", (url,))["id"]
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("is_live") or info.get("live_status") in LIVE_STATES:
        raise StillLive(f"{url} is still live; it will be clipped once the VOD is finished.")
    owners = [info.get("channel_url"), info.get("uploader_url"), url]
    if info.get("extractor_key", "").lower().startswith("twitch") and info.get("uploader_id"):
        owners.append(f"https://twitch.tv/{info['uploader_id']}")
    if not url_is_covered(perm, owners):
        raise PermissionDenied(f"{url} is not on a channel covered by this creator's permission "
                               f"({', '.join(db.jload(perm['source_channels'], []))}).")
    duration = info.get("duration") or 0
    tmp = get_config().dir("tmp")
    long_vod = duration > 20 * 60
    opts = {"quiet": True, "noplaylist": True, "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            "format": "bestaudio/best" if long_vod else "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        got = ydl.extract_info(url, download=True)
        fn = Path(ydl.prepare_filename(got))
    if not fn.exists():
        cands = list(tmp.glob(f"{got['id']}.*"))
        if not cands:
            raise RuntimeError("Download finished but the file was not found.")
        fn = cands[0]
    return ingest_file(creator_id, fn, kind=kind, origin=url, title=info.get("title"), move=True)


def _download_section(url: str, start: float, end: float, out: Path) -> Path:
    import yt_dlp
    from yt_dlp.utils import download_range_func

    opts = {"quiet": True, "noplaylist": True, "outtmpl": str(out.with_suffix(".%(ext)s")),
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b", "merge_output_format": "mp4",
            "download_ranges": download_range_func(None, [(start, end)]), "force_keyframes_at_cuts": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    cands = sorted(out.parent.glob(out.stem + ".*"))
    if not cands:
        raise RuntimeError("Section download produced no file")
    return cands[0]


def scan_inbox() -> list[int]:
    """data/inbox/<creator-slug>/<file>: drop footage here (Syncthing, SMB share, etc.)."""
    ids = []
    root = get_config().dir("inbox")
    for d in root.iterdir():
        if not d.is_dir():
            continue
        creator = db.q1("SELECT id FROM creators WHERE slug=?", (d.name,))
        for f in sorted(d.iterdir()):
            if f.suffix.lower() not in VIDEO_EXT or f.name.startswith("."):
                continue
            if not creator:
                db.log_event("warn", "inbox", f"Folder inbox/{d.name} has no matching creator slug")
                break
            try:
                ids.append(ingest_file(creator["id"], f, kind="inbox", move=True))
            except PermissionDenied as e:
                db.log_event("warn", "inbox", str(e))
                break
            except Exception as e:  # noqa: BLE001 - keep scanning other files
                db.log_event("error", "inbox", f"{f.name}: {e}")
    return ids


# ---------------------------------------------------------------- analysis
def process_source(source_id: int) -> list[int]:
    src = db.q1("SELECT * FROM sources WHERE id=?", (source_id,))
    db.ex("UPDATE sources SET status='processing', error=NULL WHERE id=?", (source_id,))
    try:
        tr_row = db.q1("SELECT data FROM transcripts WHERE source_id=?", (source_id,))
        if tr_row:
            tr = json.loads(tr_row["data"])
        else:
            tr = transcribe(src["path"])
            db.ex("INSERT OR REPLACE INTO transcripts(source_id, model, language, data) VALUES(?,?,?,?)",
                  (source_id, get_config().whisper_model, tr.get("language"), json.dumps(tr)))
        loud = media.file_loudness_curve(src["path"])
        formats = [f for f in (db.get_setting("formats") or ["punch", "story"]) if f in FORMATS]
        moments = find_moments(tr, loud, src["duration"], formats)
        db.ex("DELETE FROM moments WHERE source_id=? AND status='candidate'", (source_id,))
        ids = []
        for m in moments:
            text = " ".join(w["word"].strip() for w in words_in(tr, m.start, m.end))
            ids.append(db.ex(
                "INSERT INTO moments(source_id, start, end, fmt, score, hook, context, payoff, reasons, text)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (source_id, m.start, m.end, m.fmt, m.score, m.hook, m.context, m.payoff, json.dumps(m.reasons), text)))
        llm = copywriter.rerank([{"id": i, "text": db.q1("SELECT text FROM moments WHERE id=?", (i,))["text"]}
                                 for i in ids[:12]])
        if llm:
            for mid, s in llm.items():
                db.ex("UPDATE moments SET score=round(0.5*score+0.5*?,4) WHERE id=?", (s, mid))
        limit = int(db.get_setting("clips_per_source") or 4)
        if src["duration"] > 3600:
            limit = max(limit, int(src["duration"] // 1800))  # long VODs: ~1 clip per 30 min
        min_score = float(db.get_setting("min_moment_score") or 0.45)
        chosen = db.q("SELECT id FROM moments WHERE source_id=? AND score>=? AND status='candidate' "
                      "ORDER BY score DESC LIMIT ?", (source_id, min_score, limit))
        clip_ids = []
        for row in chosen:
            db.ex("UPDATE moments SET status='selected' WHERE id=?", (row["id"],))
            try:
                clip_ids.append(render_moment(row["id"]))
            except Exception as e:  # noqa: BLE001
                db.log_event("error", "render", f"Moment {row['id']}: {e}", "moment", row["id"])
        db.ex("UPDATE sources SET status='done' WHERE id=?", (source_id,))
        db.log_event("info", "process", f"Source {source_id}: {len(moments)} moments, {len(clip_ids)} clips",
                     "source", source_id)
        return clip_ids
    except Exception as e:
        db.ex("UPDATE sources SET status='error', error=? WHERE id=?", (str(e)[:2000], source_id))
        db.log_event("error", "process", f"Source {source_id}: {e}", "source", source_id)
        raise


def credit_for(creator: dict, perm: dict | None) -> str:
    if perm and perm.get("credit_format"):
        return perm["credit_format"]
    if creator.get("credit_text"):
        return creator["credit_text"]
    if creator.get("primary_url"):
        return norm_channel(creator["primary_url"])
    return creator["display_name"]


def render_moment(moment_id: int, layout: str | None = None) -> int:
    cfg = get_config()
    m = dict(db.q1("SELECT * FROM moments WHERE id=?", (moment_id,)))
    src = dict(db.q1("SELECT * FROM sources WHERE id=?", (m["source_id"],)))
    creator = dict(db.q1("SELECT * FROM creators WHERE id=?", (src["creator_id"],)))
    perm = review.permission_for(creator["id"])
    tr = json.loads(db.q1("SELECT data FROM transcripts WHERE source_id=?", (src["id"],))["data"])
    words = words_in(tr, m["start"], m["end"])
    segments = [s for s in tr["segments"] if s["end"] > m["start"] and s["start"] < m["end"]]
    facecam = db.jload(creator["facecam_box"])
    source_path, start, end = Path(src["path"]), m["start"], m["end"]
    if src["audio_only"]:
        if not src["origin"]:
            raise RuntimeError("Audio-only source without an origin URL to fetch video from")
        source_path = _download_section(src["origin"], start, end, cfg.dir("tmp") / f"sec_{moment_id}")
        start, end = 0.0, end - start
    clip_key = hashlib.sha1(f"{moment_id}:{layout}".encode()).hexdigest()[:10]
    out = cfg.dir("clips") / f"{creator['slug']}_{moment_id}_{clip_key}.mp4"
    delivery = None
    if creator["is_client"]:
        delivery = cfg.dir("deliveries", creator["slug"]) / f"{creator['slug']}_{moment_id}_{clip_key}.mp4"
    credit = credit_for(creator, perm)
    spec = RenderSpec(source=source_path, start=start, end=end, words=words,
                      layout=layout or creator["layout"] or "auto", facecam_box=facecam,
                      credit=credit, brand=f"@{cfg.brand_handle}", demo=bool(src["is_demo"]))
    res = render(spec, out, cfg.dir("tmp") / f"work_{moment_id}_{clip_key}", delivery)
    if src["audio_only"]:
        source_path.unlink(missing_ok=True)
    thumb = media.thumbnail(out, out.with_suffix(".jpg"), t=min(1.5, res["duration"] / 3))
    platforms = db.get_setting("platforms_enabled") or []
    pflags, _allowed = review.permission_flags(creator["id"], platforms)
    audio = media.load_audio(src["path"], start=m["start"], duration=m["end"] - m["start"])
    cflags = review.content_flags(" ".join(w["word"] for w in words), words, m["end"] - m["start"],
                                  media.loudness_curve(audio), media.spectral_flatness_curve(audio), segments,
                                  rendered_duration=res["duration"])
    flags = pflags + cflags
    if src["is_demo"]:
        flags.insert(0, review.flag("demo_footage", "block", "Labeled local demo: never published publicly."))
    taken = {r["title"] for r in db.q("SELECT title FROM clips WHERE source_id=? AND title IS NOT NULL", (src["id"],))}
    copy = copywriter.write_copy(segments, creator["display_name"], credit, m["fmt"], avoid=taken)
    copy = copywriter.ensure_credit(copy, credit)
    if db.get_setting("ocr_enabled"):
        flags += review.screen_text_flags(out, res["duration"], cfg.dir("tmp") / f"ocr_{moment_id}_{clip_key}")
    status = review.decide(flags, creator["id"], bool(src["is_demo"]))
    compute = res["render_seconds"] / 3600 * cfg.compute_cost_per_hour * 100
    cost = round(compute + float(copy.get("cost_cents", 0)), 3)
    cid = db.ex(
        "INSERT INTO clips(moment_id, source_id, creator_id, fmt, layout, path, thumb_path, delivery_path, duration,"
        " title, copy, flags, review_status, render_seconds, cost_cents, score, is_demo)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (moment_id, src["id"], creator["id"], m["fmt"], res["layout"], str(out), str(thumb),
         str(delivery) if delivery else None, res["duration"], copy["title"], json.dumps(copy), json.dumps(flags),
         status, res["render_seconds"], cost, m["score"], src["is_demo"]))
    db.ex("UPDATE moments SET status='rendered' WHERE id=?", (moment_id,))
    db.log_event("info", "render", f"Clip {cid} ({res['duration']:.0f}s, {res['layout']}) -> {status}", "clip", cid)
    if status == "auto_approved":
        queue_posts(cid)
    elif status == "needs_review" and not src["is_demo"]:
        db.log_event("action", "review", f"Clip {cid} needs review: {copy['title']}", "clip", cid)
    events.emit("clip.needs_review" if status == "needs_review" else "clip.ready", events.clip_summary(cid))
    return cid


def ingest_submissions(creator_id: int) -> None:
    """Footage sent through /p/join waits until the permission is verified, then gets ingested."""
    for s in db.q("SELECT * FROM submissions WHERE creator_id=? AND status='new'", (creator_id,)):
        try:
            if s["file_path"] and Path(s["file_path"]).exists():
                ingest_file(creator_id, s["file_path"], kind="submission", move=True)
            if s["footage_url"]:
                ingest_url(creator_id, s["footage_url"], kind="submission")
            db.ex("UPDATE submissions SET status='ingested' WHERE id=?", (s["id"],))
        except Exception as e:  # noqa: BLE001
            db.ex("UPDATE submissions SET status='error' WHERE id=?", (s["id"],))
            db.log_event("error", "submission", f"Submission {s['id']}: {e}")


def autoverify() -> int:
    """Confirm channel ownership: the code from /p/join appears on the streamer's public channel page."""
    import httpx

    n = 0
    rows = db.q("SELECT c.id cid, c.primary_url, c.verify_code, p.id pid FROM creators c JOIN permissions p "
                "ON p.creator_id=c.id WHERE p.verified=0 AND p.status='pending' AND c.verify_code IS NOT NULL "
                "AND c.primary_url IS NOT NULL AND p.created_at>=datetime('now','-14 days')")
    for r in rows:
        url = r["primary_url"] if r["primary_url"].startswith("http") else "https://" + r["primary_url"]
        targets = [url]
        if "youtube.com" in url and not url.rstrip("/").endswith("/about"):
            targets.append(url.rstrip("/") + "/about")
        found = False
        for t in targets:
            try:
                resp = httpx.get(t, follow_redirects=True, timeout=20,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; RolecallVerifier/1.0)"})
            except httpx.HTTPError:
                continue
            if r["verify_code"] in resp.text:
                found = True
                break
        if found:
            db.ex("UPDATE permissions SET verified=1, status='active', evidence=COALESCE(evidence,'') || ? WHERE id=?",
                  (f"; ownership code {r['verify_code']} seen on {url} at {db.now_iso()}", r["pid"]))
            db.log_event("action", "permission", f"Channel ownership verified automatically for creator {r['cid']}",
                         "creator", r["cid"])
            ingest_submissions(r["cid"])
            n += 1
    return n


# ---------------------------------------------------------------- review + queue
def queue_posts(clip_id: int) -> list[int]:
    clip = db.q1("SELECT * FROM clips WHERE id=?", (clip_id,))
    if clip["is_demo"]:
        raise PermissionDenied("Demo footage is never queued for public posting.")
    platforms = db.get_setting("platforms_enabled") or []
    _, allowed = review.permission_flags(clip["creator_id"], platforms)
    ids = []
    for p in allowed:
        key = hashlib.sha256(f"{clip_id}:{p}:{clip['path']}".encode()).hexdigest()[:24]
        manual = {"tiktok": (db.get_setting("tiktok_mode") or "export") == "export",
                  "instagram": (db.get_setting("instagram_mode") or "api") == "export"}
        mode = "export" if manual.get(p) else "api"
        try:
            ids.append(db.ex("INSERT INTO posts(clip_id, platform, mode, idempotency_key) VALUES(?,?,?,?)",
                             (clip_id, p, mode, key)))
        except Exception:  # UNIQUE(clip_id, platform): already queued
            continue
    return ids


def approve(clip_id: int, overrides: dict | None = None) -> list[int]:
    clip = db.q1("SELECT * FROM clips WHERE id=?", (clip_id,))
    flags = db.jload(clip["flags"], [])
    hard = [f for f in flags if f["code"] in {"no_permission", "permission_inactive", "permission_unverified",
                                               "permission_expired", "no_edit", "no_repost", "no_monetize",
                                               "demo_footage", "too_long_for_shorts"}]
    if hard:
        raise PermissionDenied("Cannot approve: " + "; ".join(f["detail"] for f in hard))
    if overrides:
        copy = db.jload(clip["copy"], {})
        for k, v in overrides.items():
            plat, _, field = k.partition(".")
            copy.setdefault(plat, {})[field] = v
            if k == "youtube.title":
                copy["title"] = v
        db.ex("UPDATE clips SET copy=?, title=? WHERE id=?", (json.dumps(copy), copy.get("title"), clip_id))
    db.ex("UPDATE clips SET review_status='approved', reviewed_at=? WHERE id=?", (db.now_iso(), clip_id))
    return queue_posts(clip_id)


def reject(clip_id: int) -> None:
    db.ex("UPDATE clips SET review_status='rejected', reviewed_at=? WHERE id=?", (db.now_iso(), clip_id))
    db.ex("UPDATE posts SET status='cancelled' WHERE clip_id=? AND status IN ('queued','failed')", (clip_id,))


def handle_removal(request_id: int) -> str:
    """Immediately cancel anything not yet posted for the creator; list live posts to take down."""
    req = db.q1("SELECT * FROM removal_requests WHERE id=?", (request_id,))
    creator_id = req["creator_id"]
    actions = []
    if creator_id:
        n = db.conn().execute(
            "UPDATE posts SET status='cancelled', last_error='removal request' WHERE status IN "
            "('queued','failed','processing') AND clip_id IN (SELECT id FROM clips WHERE creator_id=?)",
            (creator_id,)).rowcount
        db.conn().commit()
        db.ex("UPDATE clips SET review_status='rejected' WHERE creator_id=? AND review_status IN "
              "('pending','needs_review','approved','auto_approved') AND id NOT IN "
              "(SELECT clip_id FROM posts WHERE status='published')", (creator_id,))
        actions.append(f"cancelled {n} pending posts")
        live = db.q("SELECT p.platform, p.remote_url FROM posts p JOIN clips c ON c.id=p.clip_id "
                    "WHERE c.creator_id=? AND p.status='published'", (creator_id,))
        if live:
            actions.append("live posts to delete by hand: " + ", ".join(r["remote_url"] or r["platform"] for r in live))
    db.ex("UPDATE removal_requests SET actions=? WHERE id=?", (json.dumps(actions), request_id))
    db.log_event("action", "removal", f"Removal request {request_id}: " + "; ".join(actions or ["no creator matched"]),
                 "removal", request_id)
    return "; ".join(actions)
