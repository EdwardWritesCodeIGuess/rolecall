"""Dashboard (HTTP Basic auth) + public streamer pages (intake, removal, pricing, deliveries)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, pipeline, scheduler, security, stats
from ..config import get_config
from ..publishers import PLATFORMS, get_publisher
from ..publishers import tiktok as tiktok_mod
from ..publishers import youtube as yt_mod

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["fromjson"] = lambda s: db.jload(s, [])
templates.env.filters["money"] = lambda v: f"${v:,.2f}" if v is not None else "-"
templates.env.filters["num"] = lambda v: f"{v:,.0f}" if v is not None else "-"

DEFAULT_PACKAGES = [
    {"id": "starter", "name": "Starter", "price": 99, "clips": 12,
     "blurb": "12 finished vertical clips a month from your VODs, captioned and ready to post."},
    {"id": "growth", "name": "Growth", "price": 199, "clips": 30,
     "blurb": "30 clips a month, titles and captions for TikTok, Reels and Shorts, 48h turnaround."},
    {"id": "daily", "name": "Daily", "price": 349, "clips": 60,
     "blurb": "Two clips a day, priority turnaround, monthly performance report."},
]


# ------------------------------------------------------------------ auth / csrf
def admin_password() -> str:
    cfg = get_config()
    if cfg.admin_password:
        return cfg.admin_password
    p = cfg.data_dir / ".admin_password"
    if not p.exists():
        p.write_text(secrets.token_urlsafe(12))
        p.chmod(0o600)
        log.warning("Generated dashboard password in %s", p)
    return p.read_text().strip()


basic = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
    cfg = get_config()
    ok = creds is not None and hmac.compare_digest(creds.username.encode(), cfg.admin_user.encode()) and \
        hmac.compare_digest(creds.password.encode(), admin_password().encode())
    if not ok:
        raise HTTPException(401, "Login required", headers={"WWW-Authenticate": 'Basic realm="Rolecall"'})
    return creds.username


def csrf_token() -> str:
    return hmac.new(security._secret(), b"csrf", hashlib.sha256).hexdigest()[:32]


async def check_csrf(request: Request) -> None:
    if request.method == "POST":
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), csrf_token()):
            raise HTTPException(403, "Bad form token: reload the page and try again.")


templates.env.globals["csrf"] = csrf_token


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    cfg = get_config()
    ctx.setdefault("brand", cfg.brand_name)
    ctx.setdefault("handle", cfg.brand_handle)
    ctx.setdefault("paused", db.get_setting("paused"))
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


# ------------------------------------------------------------------ app
@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    admin_password()
    if get_config().worker_enabled:
        scheduler.start_background()
    yield
    scheduler.stop()


app = FastAPI(title="Rolecall", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
admin = APIRouter(dependencies=[Depends(require_admin), Depends(check_csrf)])
public = APIRouter()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "paused": db.get_setting("paused")}


# ------------------------------------------------------------------ overview + controls
@admin.get("/", response_class=HTMLResponse)
def overview(request: Request):
    todo = []
    k = stats.overview()
    if k["needs_review"]:
        todo.append(("Review clips", f"{k['needs_review']} clip(s) waiting", "/clips?status=needs_review"))
    if k["export_ready"]:
        todo.append(("Post by hand", f"{k['export_ready']} clip(s) ready to post (TikTok / Instagram export mode)", "/posts?status=export_ready"))
    if k["failed"]:
        todo.append(("Fix failed posts", f"{k['failed']} failed", "/posts?status=failed"))
    if k["removals_open"]:
        todo.append(("Removal requests", f"{k['removals_open']} open", "/requests"))
    if k["perms_pending"]:
        todo.append(("Verify streamer permissions", f"{k['perms_pending']} pending", "/creators"))
    snap = scheduler.status_snapshot()
    for p, s in snap.items():
        if not s["connected"]:
            todo.append((f"Connect {p}", s["connection"], "/connections"))
    events = db.q("SELECT * FROM events ORDER BY id DESC LIMIT 15")
    return render(request, "overview.html", k=k, todo=todo, snap=snap, events=events,
                  settings={key: db.get_setting(key) for key in db.DEFAULT_SETTINGS})


@admin.post("/pause")
async def pause(request: Request):
    form = await request.form()
    key = "paused" if not form.get("platform") else f"paused_{form['platform']}"
    db.set_setting(key, not db.get_setting(key))
    db.log_event("action", "pause", f"{key} -> {db.get_setting(key)}")
    return back(request)


@admin.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    ints = ["daily_limit_youtube", "daily_limit_instagram", "daily_limit_tiktok", "min_gap_minutes",
            "post_window_start", "post_window_end", "clips_per_source", "trusted_after", "max_attempts"]
    for key in ints:
        if key in form and str(form[key]).strip():
            db.set_setting(key, max(0, int(form[key])))
    if "min_moment_score" in form:
        db.set_setting("min_moment_score", float(form["min_moment_score"]))
    if form.get("platforms_form"):
        db.set_setting("platforms_enabled", [p for p in PLATFORMS if form.get(f"enable_{p}")])
        db.set_setting("formats", [f for f in ("punch", "story", "long") if form.get(f"fmt_{f}")] or ["punch"])
    if form.get("tiktok_mode") in ("export", "inbox"):
        db.set_setting("tiktok_mode", form["tiktok_mode"])
    if form.get("instagram_mode") in ("api", "export"):
        db.set_setting("instagram_mode", form["instagram_mode"])
    return back(request)


@admin.post("/worker/tick")
def worker_tick(request: Request):
    threading.Thread(target=scheduler.tick, daemon=True).start()
    return back(request)


# ------------------------------------------------------------------ clips
@admin.get("/clips", response_class=HTMLResponse)
def clips(request: Request, status: str = "", creator: int | None = None):
    where, args = ["1=1"], []
    if status:
        where.append("c.review_status=?")
        args.append(status)
    if creator:
        where.append("c.creator_id=?")
        args.append(creator)
    rows = db.q(f"SELECT c.*, cr.display_name FROM clips c JOIN creators cr ON cr.id=c.creator_id "
                f"WHERE {' AND '.join(where)} ORDER BY c.id DESC LIMIT 200", tuple(args))
    posts = defaultdict(list)
    for p in db.q("SELECT * FROM posts WHERE clip_id IN (SELECT id FROM clips ORDER BY id DESC LIMIT 200)"):
        posts[p["clip_id"]].append(p)
    return render(request, "clips.html", clips=rows, posts=posts, status=status)


@admin.get("/clips/{cid}", response_class=HTMLResponse)
def clip_detail(request: Request, cid: int):
    c = db.q1("SELECT c.*, cr.display_name, cr.slug FROM clips c JOIN creators cr ON cr.id=c.creator_id WHERE c.id=?", (cid,))
    if not c:
        raise HTTPException(404)
    m = db.q1("SELECT * FROM moments WHERE id=?", (c["moment_id"],))
    src = db.q1("SELECT * FROM sources WHERE id=?", (c["source_id"],))
    posts = db.q("SELECT * FROM posts WHERE clip_id=?", (cid,))
    return render(request, "clip.html", c=c, m=m, src=src, posts=posts, copy=db.jload(c["copy"], {}),
                  flags=db.jload(c["flags"], []))


@admin.post("/clips/{cid}/approve")
async def clip_approve(request: Request, cid: int):
    form = await request.form()
    overrides = {k: v for k, v in form.items() if k.count(".") == 1 and k.split(".")[0] in PLATFORMS + ["youtube"]}
    if "youtube.tags" in overrides:
        overrides["youtube.tags"] = [t.strip() for t in str(overrides["youtube.tags"]).split(",") if t.strip()]
    try:
        pipeline.approve(cid, overrides)
    except pipeline.PermissionDenied as e:
        db.log_event("warn", "review", f"Clip {cid}: {e}", "clip", cid)
        return render(request, "message.html", title="Can't approve this clip", message=str(e), link=f"/clips/{cid}")
    return RedirectResponse(form.get("next") or "/clips?status=needs_review", status_code=303)


@admin.post("/clips/{cid}/reject")
async def clip_reject(request: Request, cid: int):
    pipeline.reject(cid)
    form = await request.form()
    return RedirectResponse(form.get("next") or "/clips?status=needs_review", status_code=303)


@admin.post("/clips/{cid}/rerender")
async def clip_rerender(request: Request, cid: int):
    form = await request.form()
    c = db.q1("SELECT * FROM clips WHERE id=?", (cid,))
    layout = form.get("layout") or None

    def job():
        try:
            new_id = pipeline.render_moment(c["moment_id"], layout=layout)
            db.ex("UPDATE clips SET review_status='rejected' WHERE id=? AND review_status!='rejected'", (cid,))
            db.log_event("info", "render", f"Clip {cid} re-rendered as clip {new_id} ({layout})", "clip", new_id)
        except Exception as e:  # noqa: BLE001
            db.log_event("error", "render", f"Re-render of clip {cid} failed: {e}", "clip", cid)

    threading.Thread(target=job, daemon=True).start()
    return render(request, "message.html", title="Re-rendering",
                  message="The new version appears in Clips in under a minute. The old one is marked rejected.",
                  link="/clips")


@admin.get("/media/clip/{cid}")
def media_clip(cid: int, kind: str = "clip"):
    c = db.q1("SELECT path, thumb_path, delivery_path FROM clips WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404)
    path = {"clip": c["path"], "thumb": c["thumb_path"], "delivery": c["delivery_path"]}.get(kind)
    if not path or not Path(path).exists():
        raise HTTPException(404)
    return FileResponse(path)


@admin.get("/media/frame/{creator_id}")
def media_frame(creator_id: int):
    src = db.q1("SELECT path, duration FROM sources WHERE creator_id=? AND audio_only=0 ORDER BY id DESC LIMIT 1",
                (creator_id,))
    if not src:
        raise HTTPException(404, "No footage yet for this creator")
    from ..media import frame_png

    out = get_config().dir("tmp") / f"frame_{creator_id}.png"
    if not out.exists() or time.time() - out.stat().st_mtime > 3600:
        frame_png(src["path"], out, t=min(30.0, (src["duration"] or 2) / 2))
    return FileResponse(out)


# ------------------------------------------------------------------ sources
@admin.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    rows = db.q("SELECT s.*, cr.display_name, (SELECT COUNT(*) FROM clips WHERE source_id=s.id) nclips "
                "FROM sources s JOIN creators cr ON cr.id=s.creator_id ORDER BY s.id DESC LIMIT 200")
    creators = db.q("SELECT id, display_name, slug FROM creators ORDER BY display_name")
    return render(request, "sources.html", sources=rows, creators=creators, inbox=str(get_config().dir("inbox")))


@admin.post("/sources/upload")
async def source_upload(request: Request):
    form = await request.form()
    up: UploadFile = form["file"]
    tmp = get_config().dir("tmp") / f"up_{secrets.token_hex(4)}_{Path(up.filename or 'upload.mp4').name}"
    with tmp.open("wb") as f:
        shutil.copyfileobj(up.file, f, length=8 * 1024 * 1024)
    try:
        pipeline.ingest_file(int(form["creator_id"]), tmp, kind="upload", title=up.filename, move=True)
    except (pipeline.PermissionDenied, ValueError) as e:
        tmp.unlink(missing_ok=True)
        return render(request, "message.html", title="Not ingested", message=str(e), link="/sources")
    return RedirectResponse("/sources", status_code=303)


@admin.post("/sources/url")
async def source_url(request: Request):
    form = await request.form()
    cid, url = int(form["creator_id"]), str(form["url"]).strip()

    def job():
        try:
            pipeline.ingest_url(cid, url)
        except Exception as e:  # noqa: BLE001
            db.log_event("error", "ingest", f"{url}: {e}")

    threading.Thread(target=job, daemon=True).start()
    return render(request, "message.html", title="Downloading",
                  message="The download runs in the background. It shows up in Sources when done, or in the event "
                          "log if the URL isn't covered by the creator's permission.", link="/sources")


@admin.post("/sources/{sid}/reprocess")
def source_reprocess(request: Request, sid: int):
    db.ex("UPDATE sources SET status='new', error=NULL WHERE id=?", (sid,))
    return back(request, "/sources")


@admin.post("/demo")
def make_demo(request: Request):
    def job():
        from .. import demo

        try:
            cid = demo.ensure_demo_creator()
            out = demo.make_demo(get_config().dir("tmp") / f"demo_{int(time.time())}.mp4")
            pipeline.ingest_file(cid, out, kind="demo", title="DEMO: traffic stop + court (synthetic)",
                                 is_demo=True, move=True)
        except Exception as e:  # noqa: BLE001
            db.log_event("error", "demo", str(e))

    threading.Thread(target=job, daemon=True).start()
    return render(request, "message.html", title="Demo started",
                  message="Generating the labeled demo source; the worker processes it within a minute.",
                  link="/sources")


# ------------------------------------------------------------------ creators + permissions
def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or f"creator-{secrets.token_hex(2)}"


@admin.get("/creators", response_class=HTMLResponse)
def creators(request: Request):
    rows = db.q("""SELECT c.*, p.status pstatus, p.verified, p.platforms, p.allow_monetize,
                   (SELECT COUNT(*) FROM clips WHERE creator_id=c.id AND is_demo=0) nclips
                   FROM creators c LEFT JOIN permissions p ON p.id=(SELECT id FROM permissions WHERE creator_id=c.id
                   ORDER BY id DESC LIMIT 1) ORDER BY c.id DESC""")
    return render(request, "creators.html", creators=rows)


@admin.post("/creators")
async def creator_create(request: Request):
    form = await request.form()
    name = str(form["display_name"]).strip()
    slug = slugify(form.get("slug") or name)
    cid = db.ex("INSERT INTO creators(slug, display_name, primary_url, contact_email, credit_text) VALUES(?,?,?,?,?)",
                (slug, name, form.get("primary_url"), form.get("contact_email"), form.get("credit_text") or None))
    return RedirectResponse(f"/creators/{cid}", status_code=303)


@admin.get("/creators/{cid}", response_class=HTMLResponse)
def creator_detail(request: Request, cid: int):
    c = db.q1("SELECT * FROM creators WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404)
    perms = db.q("SELECT * FROM permissions WHERE creator_id=? ORDER BY id DESC", (cid,))
    subs = db.q("SELECT * FROM submissions WHERE creator_id=? ORDER BY id DESC", (cid,))
    has_frame = db.q1("SELECT 1 FROM sources WHERE creator_id=? AND audio_only=0", (cid,)) is not None
    return render(request, "creator.html", c=c, perms=perms, subs=subs, has_frame=has_frame,
                  facecam=db.jload(c["facecam_box"], None), base_url=get_config().public_base_url or get_config().base_url)


@admin.post("/creators/{cid}")
async def creator_update(request: Request, cid: int):
    form = await request.form()
    facecam = None
    if form.get("fc_enabled"):
        facecam = json.dumps({k: round(float(form[f"fc_{k}"]) / 100, 4) for k in ("x", "y", "w", "h")})
    is_client = 1 if form.get("is_client") else 0
    token = db.q1("SELECT delivery_token FROM creators WHERE id=?", (cid,))["delivery_token"]
    if is_client and not token:
        token = security.random_token()
    db.ex("UPDATE creators SET display_name=?, primary_url=?, contact_email=?, credit_text=?, layout=?, facecam_box=?,"
          " auto_approve=?, is_client=?, delivery_token=?, watch_url=?, notes=? WHERE id=?",
          (form["display_name"], form.get("primary_url"), form.get("contact_email"), form.get("credit_text") or None,
           form.get("layout") or "auto", facecam, 1 if form.get("auto_approve") else 0, is_client, token,
           form.get("watch_url") or None, form.get("notes"), cid))
    return RedirectResponse(f"/creators/{cid}", status_code=303)


@admin.post("/creators/{cid}/permissions")
async def permission_create(request: Request, cid: int):
    form = await request.form()
    channels = [c.strip() for c in str(form.get("source_channels", "")).splitlines() if c.strip()]
    db.ex("INSERT INTO permissions(creator_id, status, verified, source_channels, platforms, allow_edit, allow_repost,"
          " allow_monetize, credit_required, credit_format, revenue_share_pct, revenue_terms, granted_by, evidence,"
          " granted_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (cid, "active" if form.get("verified") else "pending", 1 if form.get("verified") else 0, json.dumps(channels),
           json.dumps([p for p in PLATFORMS if form.get(f"p_{p}")]), 1 if form.get("allow_edit") else 0,
           1 if form.get("allow_repost") else 0, 1 if form.get("allow_monetize") else 0, 1,
           form.get("credit_format") or None, float(form.get("revenue_share_pct") or 0), form.get("revenue_terms"),
           form.get("granted_by"), form.get("evidence"), form.get("granted_at") or db.now_iso(),
           form.get("expires_at") or None))
    db.log_event("action", "permission", f"Permission recorded for creator {cid}", "creator", cid)
    return RedirectResponse(f"/creators/{cid}", status_code=303)


@admin.post("/permissions/{pid}/{action}")
def permission_action(request: Request, pid: int, action: str):
    p = db.q1("SELECT * FROM permissions WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    if action == "verify":
        db.ex("UPDATE permissions SET verified=1, status='active' WHERE id=?", (pid,))
        from .. import discover

        discover.ensure_watch(p["creator_id"])
        _ingest_pending_submissions(p["creator_id"])
    elif action == "revoke":
        db.ex("UPDATE permissions SET status='revoked', revoked_at=? WHERE id=?", (db.now_iso(), pid))
        n = db.conn().execute("UPDATE posts SET status='cancelled', last_error='permission revoked' WHERE status IN "
                              "('queued','failed','processing') AND clip_id IN (SELECT id FROM clips WHERE creator_id=?)",
                              (p["creator_id"],)).rowcount
        db.conn().commit()
        db.log_event("action", "permission", f"Permission {pid} revoked; {n} pending posts cancelled", "creator", p["creator_id"])
    else:
        raise HTTPException(400)
    return RedirectResponse(f"/creators/{p['creator_id']}", status_code=303)


def _ingest_pending_submissions(creator_id: int) -> None:
    threading.Thread(target=pipeline.ingest_submissions, args=(creator_id,), daemon=True).start()


# ------------------------------------------------------------------ posts
@admin.get("/posts", response_class=HTMLResponse)
def posts(request: Request, status: str = ""):
    where = "WHERE p.status=?" if status else ""
    rows = db.q(f"SELECT p.*, c.title, c.copy, c.thumb_path, cr.display_name FROM posts p JOIN clips c ON c.id=p.clip_id "
                f"JOIN creators cr ON cr.id=c.creator_id {where} ORDER BY p.id DESC LIMIT 300", (status,) if status else ())
    return render(request, "posts.html", posts=rows, status=status, snap=scheduler.status_snapshot())


@admin.post("/posts/{pid}/{action}")
async def post_action(request: Request, pid: int, action: str):
    form = await request.form()
    if action == "retry":
        scheduler.retry_post(pid)
    elif action == "cancel":
        db.ex("UPDATE posts SET status='cancelled' WHERE id=? AND status IN ('queued','failed','export_ready')", (pid,))
    elif action == "manual":
        url = str(form.get("url", "")).strip()
        if not url.startswith("https://"):
            return render(request, "message.html", title="Need the post URL", message="Paste the https:// link.",
                          link="/posts")
        scheduler.mark_manual_posted(pid, url)
    elif action == "publish_now":
        post = db.q1("SELECT * FROM posts WHERE id=?", (pid,))
        if db.get_setting("paused"):
            return render(request, "message.html", title="Paused", message="Unpause publishing first.", link="/posts")
        threading.Thread(target=scheduler.publish_one, args=(post,), daemon=True).start()
    return back(request, "/posts")


@admin.get("/posts/{pid}/video")
def post_video(pid: int):
    p = db.q1("SELECT c.path FROM posts p JOIN clips c ON c.id=p.clip_id WHERE p.id=?", (pid,))
    if not p:
        raise HTTPException(404)
    return FileResponse(p["path"], filename=f"rolecall_{pid}.mp4")


# ------------------------------------------------------------------ metrics + money
@admin.get("/metrics", response_class=HTMLResponse)
def metrics(request: Request):
    return render(request, "metrics.html", k=stats.overview(), by_format=stats.breakdown("format"),
                  by_creator=stats.breakdown("creator"), by_platform=stats.breakdown("platform"),
                  by_layout=stats.breakdown("layout"), tips=stats.recommendations(), ledger=stats.ledger_summary(),
                  entries=db.q("SELECT * FROM ledger ORDER BY date DESC, id DESC LIMIT 50"),
                  manual_posts=db.q("SELECT p.id, p.platform, c.title FROM posts p JOIN clips c ON c.id=p.clip_id "
                                    "WHERE p.status='published' AND p.platform='tiktok' ORDER BY p.id DESC LIMIT 30"),
                  creators=db.q("SELECT id, display_name FROM creators WHERE is_client=1"))


@admin.post("/ledger")
async def ledger_add(request: Request):
    form = await request.form()
    db.ex("INSERT INTO ledger(date, kind, category, platform, creator_id, amount_cents, note) VALUES(?,?,?,?,?,?,?)",
          (form.get("date") or db.now_iso()[:10], form["kind"], form["category"], form.get("platform") or None,
           int(form["creator_id"]) if form.get("creator_id") else None, int(round(float(form["amount"]) * 100)),
           form.get("note")))
    return back(request, "/metrics")


@admin.post("/metrics/manual")
async def metrics_manual(request: Request):
    form = await request.form()
    db.ex("INSERT INTO post_metrics(post_id, views, avg_view_pct, likes, followers_gained) VALUES(?,?,?,?,?)",
          (int(form["post_id"]), int(form.get("views") or 0), float(form.get("avg_view_pct") or 0) or None,
           int(form.get("likes") or 0), int(form.get("followers_gained") or 0)))
    if form.get("followers"):
        db.ex("INSERT INTO account_stats(platform, followers) VALUES('tiktok', ?)", (int(form["followers"]),))
    return back(request, "/metrics")


# ------------------------------------------------------------------ leads + requests + events
@admin.get("/prospects", response_class=HTMLResponse)
def prospects(request: Request, status: str = "new"):
    from .. import discover

    where, args = ("WHERE status=?", (status,)) if status != "all" else ("", ())
    rows = [discover.summary(p) for p in db.q(f"SELECT * FROM prospects {where} ORDER BY score DESC, last_seen DESC "
                                              "LIMIT 200", args)]
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM prospects GROUP BY status")}
    cfg = get_config()
    return render(request, "prospects.html", prospects=rows, status=status, counts=counts,
                  public_ok=bool(cfg.public_base_url), twitch_ok=bool(cfg.twitch_client_id and cfg.twitch_client_secret),
                  youtube_ok=get_publisher("youtube").connected()[0])


@admin.post("/prospects/run")
def prospects_run(request: Request):
    from .. import autopilot

    try:
        autopilot.start("discover", by="dashboard")
    except autopilot.Busy:
        pass
    return back(request, "/prospects")


@admin.post("/prospects/{pid}/{action}")
def prospect_action(request: Request, pid: int, action: str):
    from .. import discover

    if action not in ("invited", "ignored", "declined", "new"):
        raise HTTPException(400)
    discover.mark(pid, action)
    return back(request, "/prospects")


@admin.get("/leads", response_class=HTMLResponse)
def leads(request: Request):
    return render(request, "leads.html", leads=db.q("SELECT * FROM leads ORDER BY id DESC"),
                  packages=db.get_setting("packages") or DEFAULT_PACKAGES)


@admin.post("/leads/{lid}")
async def lead_update(request: Request, lid: int):
    form = await request.form()
    db.ex("UPDATE leads SET status=? WHERE id=?", (form["status"], lid))
    return back(request, "/leads")


@admin.get("/requests", response_class=HTMLResponse)
def requests_page(request: Request):
    return render(request, "requests.html",
                  removals=db.q("SELECT * FROM removal_requests ORDER BY id DESC"),
                  subs=db.q("SELECT s.*, c.display_name FROM submissions s LEFT JOIN creators c ON c.id=s.creator_id "
                            "ORDER BY s.id DESC LIMIT 100"))


@admin.post("/removals/{rid}/{status}")
def removal_status(request: Request, rid: int, status: str):
    if status not in ("done", "rejected", "open"):
        raise HTTPException(400)
    db.ex("UPDATE removal_requests SET status=? WHERE id=?", (status, rid))
    return back(request, "/requests")


@admin.get("/events", response_class=HTMLResponse)
def events(request: Request):
    return render(request, "events.html", events=db.q("SELECT * FROM events ORDER BY id DESC LIMIT 500"))


# ------------------------------------------------------------------ connections
def _new_state(platform: str) -> str:
    st = secrets.token_urlsafe(16)
    db.set_setting(f"oauth_state_{platform}", {"state": st, "ts": time.time()})
    return st


def _check_state(platform: str, state: str) -> bool:
    """One-time, 30-minute state token: the only thing guarding the (unauthenticated) OAuth callbacks."""
    saved = db.get_setting(f"oauth_state_{platform}") or {}
    ok = bool(saved) and hmac.compare_digest(saved.get("state", ""), state or "") and time.time() - saved.get("ts", 0) < 1800
    if ok:
        db.set_setting(f"oauth_state_{platform}", {})  # single use
    return ok


@admin.get("/connections", response_class=HTMLResponse)
def connections(request: Request):
    cfg = get_config()
    accounts = {r["platform"]: r for r in db.q("SELECT platform, account_name, updated_at FROM tokens")}
    return render(request, "connections.html", snap=scheduler.status_snapshot(), accounts=accounts, cfg=cfg,
                  yt_redirect=yt_mod.redirect_uri(), tt_redirect=tiktok_mod.redirect_uri(),
                  tiktok_mode=db.get_setting("tiktok_mode"), instagram_mode=db.get_setting("instagram_mode"))


@admin.get("/connect/youtube")
def connect_youtube():
    cfg = get_config()
    if not (cfg.google_client_id and cfg.google_client_secret):
        raise HTTPException(400, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first (see docs/LAUNCH.md).")
    return RedirectResponse(yt_mod.auth_url(_new_state("youtube")))


# OAuth callbacks are public routes (the browser returning from Google/TikTok may not carry the
# dashboard login); they are protected by the single-use state token minted by /connect/*.
@public.get("/oauth/youtube/callback", response_class=HTMLResponse)
def youtube_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not _check_state("youtube", state):
        return render(request, "public_message.html", title="YouTube not connected",
                      message=error or "This link expired or was already used. Start again from Connections.")
    try:
        name = yt_mod.YouTube().exchange_code(code)
    except Exception as e:  # noqa: BLE001
        return render(request, "public_message.html", title="YouTube not connected", message=str(e))
    db.log_event("action", "connect", f"YouTube connected: {name}")
    return render(request, "public_message.html", title="YouTube connected",
                  message=f"Rolecall can now post to the channel “{name}”. You can close this tab.")


@admin.post("/connect/youtube/paste")
async def youtube_paste(request: Request):
    form = await request.form()
    qs = parse_qs(urlparse(str(form.get("url", ""))).query)
    return youtube_callback(request, code=(qs.get("code") or [""])[0], state=(qs.get("state") or [""])[0],
                            error=(qs.get("error") or [""])[0])


@admin.post("/connect/instagram")
async def connect_instagram(request: Request):
    form = await request.form()
    from ..publishers.instagram import Instagram

    try:
        name = Instagram().connect_with_token(str(form.get("token", "")).strip())
    except Exception as e:  # noqa: BLE001
        return render(request, "message.html", title="Instagram not connected", message=str(e), link="/connections")
    db.log_event("action", "connect", f"Instagram connected: @{name}")
    return RedirectResponse("/connections", status_code=303)


@admin.get("/connect/tiktok")
def connect_tiktok():
    if not get_config().tiktok_client_key:
        raise HTTPException(400, "Set TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET first.")
    return RedirectResponse(tiktok_mod.auth_url(_new_state("tiktok")))


@public.get("/oauth/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not _check_state("tiktok", state):
        return render(request, "public_message.html", title="TikTok not connected",
                      message=error or "This link expired or was already used. Start again from Connections.")
    try:
        tiktok_mod.TikTok().exchange_code(code)
    except Exception as e:  # noqa: BLE001
        return render(request, "public_message.html", title="TikTok not connected", message=str(e))
    db.set_setting("tiktok_mode", "inbox")
    return render(request, "public_message.html", title="TikTok connected", message="You can close this tab.")


@admin.post("/disconnect/{platform}")
def disconnect(request: Request, platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(400)
    security.delete_token(platform)
    db.log_event("action", "connect", f"{platform} disconnected (local tokens deleted)")
    return back(request, "/connections")


# ------------------------------------------------------------------ public pages
_hits: dict[str, list[float]] = defaultdict(list)


def rate_limited(request: Request, key: str, per_hour: int = 6) -> bool:
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now = time.time()
    lst = [t for t in _hits[f"{key}:{ip}"] if now - t < 3600]
    lst.append(now)
    _hits[f"{key}:{ip}"] = lst
    return len(lst) > per_hour


@public.get("/p/join", response_class=HTMLResponse)
def join_form(request: Request, ref: str = ""):
    from .. import discover

    p = discover.by_token(ref)
    prefill = {"ref": ref, "name": p["name"], "channel": p["url"],
               "credit": p["url"].split("://", 1)[-1].removeprefix("www.")} if p else {}
    return render(request, "public_join.html", platforms=PLATFORMS, prefill=prefill)


@public.post("/p/join", response_class=HTMLResponse)
async def join_submit(request: Request):
    form = await request.form()
    if form.get("website") or rate_limited(request, "join"):  # honeypot / flood
        return render(request, "public_message.html", title="Thanks", message="Received.")
    required = ["display_name", "channel_url", "email", "signature"]
    if any(not str(form.get(k, "")).strip() for k in required) or not form.get("agree"):
        return render(request, "public_message.html", title="Missing details",
                      message="Please fill in every required field and tick the agreement box.")
    name = str(form["display_name"]).strip()[:80]
    channel = str(form["channel_url"]).strip()[:300]
    slug = slugify(name)
    existing = db.q1("SELECT id, verify_code FROM creators WHERE slug=?", (slug,))
    code = f"RC-{secrets.token_hex(3).upper()}"
    if existing:
        cid = existing["id"]
        code = existing["verify_code"] or code
        db.ex("UPDATE creators SET verify_code=? WHERE id=?", (code, cid))
    else:
        cid = db.ex("INSERT INTO creators(slug, display_name, primary_url, contact_email, credit_text, verify_code)"
                    " VALUES(?,?,?,?,?,?)", (slug, name, channel, str(form["email"])[:200],
                                             str(form.get("credit_format") or "")[:120] or None, code))
    channels = [channel] + [c.strip() for c in str(form.get("other_channels", "")).splitlines() if c.strip()][:10]
    consent = {k: bool(form.get(k)) for k in ("allow_edit", "allow_repost", "allow_monetize", "agree", "want_service")}
    consent["terms_version"] = "2026-11-19"
    consent["signature"] = str(form["signature"])[:120]
    file_path = None
    up = form.get("file")
    if isinstance(up, UploadFile) and up.filename:
        if Path(up.filename).suffix.lower() not in pipeline.VIDEO_EXT:
            return render(request, "public_message.html", title="Unsupported file", message="Upload a video file.")
        dest = get_config().dir("submissions", slug) / f"{int(time.time())}_{Path(up.filename).name}"
        with dest.open("wb") as f:
            shutil.copyfileobj(up.file, f, length=8 * 1024 * 1024)
        file_path = str(dest)
    share = float(form.get("revenue_share_pct") or 0) if form.get("revenue_share") == "pct" else 0.0
    pid = db.ex("INSERT INTO permissions(creator_id, status, verified, source_channels, platforms, allow_edit,"
                " allow_repost, allow_monetize, credit_format, revenue_share_pct, revenue_terms, granted_by, evidence,"
                " granted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, "pending", 0, json.dumps(channels), json.dumps([p for p in PLATFORMS if form.get(f"p_{p}")]),
                 int(consent["allow_edit"]), int(consent["allow_repost"]), int(consent["allow_monetize"]),
                 str(form.get("credit_format") or "")[:120] or None, min(max(share, 0), 100),
                 str(form.get("revenue_terms") or "")[:500], consent["signature"], "", db.now_iso()))
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
    sid = db.ex("INSERT INTO submissions(creator_id, permission_id, name, email, channel_url, footage_url, file_path,"
                " consent, ip) VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, pid, name, str(form["email"])[:200], channel, str(form.get("footage_url") or "")[:500] or None,
                 file_path, json.dumps(consent), ip))
    db.ex("UPDATE permissions SET evidence=? WHERE id=?", (f"web form submission #{sid} from {ip} at {db.now_iso()}", pid))
    if consent["want_service"]:
        db.ex("INSERT INTO leads(name, email, channel_url, message, package) VALUES(?,?,?,?,?)",
              (name, str(form["email"])[:200], channel, "Interested via the join form", "unknown"))
    if form.get("ref"):
        from .. import discover

        discover.link_join(str(form["ref"])[:40], cid)
    db.log_event("action", "join", f"New streamer permission from {name} ({channel}); code {code}", "creator", cid)
    return render(request, "public_message.html", title="Thanks! One last step",
                  message=f"To prove the channel is yours, add the code {code} anywhere in your channel bio / About "
                          f"section. We check automatically and you can remove it once we confirm by email.",
                  code=code)


@public.get("/p/remove", response_class=HTMLResponse)
def remove_form(request: Request):
    return render(request, "public_remove.html")


@public.post("/p/remove", response_class=HTMLResponse)
async def remove_submit(request: Request):
    form = await request.form()
    if form.get("website") or rate_limited(request, "remove", 10):
        return render(request, "public_message.html", title="Received", message="Thanks.")
    channel = str(form.get("channel_url", "")).strip()
    creator_id = None
    if channel:
        n = pipeline.norm_channel(channel)
        for c in db.q("SELECT c.id, c.primary_url, p.source_channels FROM creators c LEFT JOIN permissions p "
                      "ON p.creator_id=c.id"):
            urls = [c["primary_url"] or ""] + db.jload(c["source_channels"], [])
            if any(u and pipeline.norm_channel(u) == n for u in urls):
                creator_id = c["id"]
                break
    rid = db.ex("INSERT INTO removal_requests(name, email, channel_url, creator_id, urls, reason) VALUES(?,?,?,?,?,?)",
                (str(form.get("name", ""))[:120], str(form.get("email", ""))[:200], channel[:300], creator_id,
                 str(form.get("urls", ""))[:3000], str(form.get("reason", ""))[:3000]))
    pipeline.handle_removal(rid)
    return render(request, "public_message.html", title="Removal request received",
                  message="Any scheduled posts for that channel are already stopped. Posts that are live come down "
                          "within 24 hours, and we confirm by email.")


@public.get("/p/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return render(request, "public_pricing.html", packages=db.get_setting("packages") or DEFAULT_PACKAGES)


@public.post("/p/pricing", response_class=HTMLResponse)
async def pricing_lead(request: Request):
    form = await request.form()
    if form.get("website") or rate_limited(request, "lead"):
        return render(request, "public_message.html", title="Thanks", message="Received.")
    db.ex("INSERT INTO leads(name, email, channel_url, avg_viewers, package, message) VALUES(?,?,?,?,?,?)",
          (str(form.get("name", ""))[:120], str(form.get("email", ""))[:200], str(form.get("channel_url", ""))[:300],
           str(form.get("avg_viewers", ""))[:40], str(form.get("package", ""))[:40], str(form.get("message", ""))[:2000]))
    db.log_event("action", "lead", f"New lead: {form.get('name')} ({form.get('channel_url')}) - {form.get('package')}")
    return render(request, "public_message.html", title="Thanks!",
                  message="We'll reply by email within a day with three free sample clips from your recent VODs.")


@public.get("/p/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "public_privacy.html", contact=_contact())


@public.get("/p/terms", response_class=HTMLResponse)
def terms(request: Request):
    return render(request, "public_terms.html", contact=_contact())


def _contact() -> str:
    import os

    return os.environ.get("CONTACT_EMAIL", "")


@public.get("/d/{token}", response_class=HTMLResponse)
def deliveries(request: Request, token: str):
    c = db.q1("SELECT * FROM creators WHERE delivery_token=? AND is_client=1", (token,))
    if not c or len(token) < 16:
        raise HTTPException(404)
    rows = db.q("SELECT * FROM clips WHERE creator_id=? AND review_status!='rejected' AND delivery_path IS NOT NULL "
                "ORDER BY id DESC LIMIT 200", (c["id"],))
    return render(request, "public_deliveries.html", c=c, clips=rows, token=token)


@public.get("/d/{token}/{cid}.mp4")
def delivery_file(token: str, cid: int):
    c = db.q1("SELECT id FROM creators WHERE delivery_token=? AND is_client=1", (token,))
    clip = db.q1("SELECT delivery_path FROM clips WHERE id=? AND creator_id=?", (cid, c["id"] if c else -1))
    if not c or not clip or not clip["delivery_path"] or not Path(clip["delivery_path"]).exists():
        raise HTTPException(404)
    return FileResponse(clip["delivery_path"], filename=f"clip_{cid}.mp4")


@public.get("/m/{post_id}/clip.mp4")
def signed_media(post_id: int, sig: str = ""):
    """Short-lived URL that Instagram's servers download the Reel from."""
    if not security.verify(f"/m/{post_id}/clip.mp4", sig):
        raise HTTPException(403)
    p = db.q1("SELECT c.path FROM posts p JOIN clips c ON c.id=p.clip_id WHERE p.id=? AND p.status IN "
              "('uploading','processing','queued')", (post_id,))
    if not p:
        raise HTTPException(404)
    return FileResponse(p["path"], media_type="video/mp4")


@public.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> str:
    return "User-agent: *\nDisallow: /\nAllow: /p/\n"


app.include_router(public)
app.include_router(admin)

from .api import api  # noqa: E402 - machine API for n8n (bearer token)

app.include_router(api)
