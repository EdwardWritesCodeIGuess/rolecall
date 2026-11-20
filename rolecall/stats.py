"""Dashboard numbers: KPIs, per-format / per-creator performance, revenue and cost per clip."""

from __future__ import annotations

from . import db

LATEST = """
SELECT pm.* FROM post_metrics pm
JOIN (SELECT post_id, MAX(id) mid FROM post_metrics GROUP BY post_id) x ON x.mid = pm.id
"""


def overview() -> dict:
    one = lambda sql, *a: db.q1(sql, a)[0] or 0  # noqa: E731
    month = db.now_iso()[:7]
    rev = one("SELECT SUM(amount_cents) FROM ledger WHERE kind='revenue' AND substr(date,1,7)=?", month)
    ext_cost = one("SELECT SUM(amount_cents) FROM ledger WHERE kind='cost' AND substr(date,1,7)=?", month)
    clips_month = one("SELECT COUNT(*) FROM clips WHERE is_demo=0 AND substr(created_at,1,7)=?", month)
    compute = one("SELECT SUM(cost_cents) FROM clips WHERE is_demo=0 AND substr(created_at,1,7)=?", month)
    followers = {r["platform"]: r["followers"] for r in db.q(
        "SELECT a.platform, a.followers FROM account_stats a JOIN (SELECT platform, MAX(id) mid FROM account_stats "
        "GROUP BY platform) x ON x.mid=a.id")}
    m = db.q1(f"SELECT SUM(views) v, SUM(watch_time_sec) w, AVG(avg_view_pct) c FROM ({LATEST})")
    return {
        "needs_review": one("SELECT COUNT(*) FROM clips WHERE review_status='needs_review' AND is_demo=0"),
        "demo_clips": one("SELECT COUNT(*) FROM clips WHERE is_demo=1"),
        "queued": one("SELECT COUNT(*) FROM posts WHERE status='queued'"),
        "export_ready": one("SELECT COUNT(*) FROM posts WHERE status='export_ready'"),
        "failed": one("SELECT COUNT(*) FROM posts WHERE status='failed'"),
        "published_7d": one("SELECT COUNT(*) FROM posts WHERE status='published' AND published_at>=datetime('now','-7 days')"),
        "published_total": one("SELECT COUNT(*) FROM posts WHERE status='published'"),
        "clips_total": one("SELECT COUNT(*) FROM clips WHERE is_demo=0"),
        "sources_processing": one("SELECT COUNT(*) FROM sources WHERE status IN ('new','processing')"),
        "views": m["v"] or 0, "watch_hours": round((m["w"] or 0) / 3600, 1),
        "completion": round(m["c"], 1) if m["c"] is not None else None,
        "followers": followers,
        "revenue_month": rev / 100, "cost_month": (ext_cost + compute) / 100,
        "cost_per_clip": round((ext_cost + compute) / 100 / clips_month, 2) if clips_month else None,
        "leads_new": one("SELECT COUNT(*) FROM leads WHERE status='new'"),
        "clients": one("SELECT COUNT(*) FROM creators WHERE is_client=1"),
        "removals_open": one("SELECT COUNT(*) FROM removal_requests WHERE status='open'"),
        "perms_pending": one("SELECT COUNT(*) FROM permissions WHERE status='pending' OR verified=0"),
    }


def breakdown(group: str) -> list[dict]:
    col = {"format": "c.fmt", "creator": "cr.display_name", "platform": "p.platform", "layout": "c.layout"}[group]
    rows = db.q(f"""
        SELECT {col} k, COUNT(DISTINCT p.id) posts, AVG(m.views) avg_views, SUM(m.views) views,
               AVG(m.avg_view_pct) completion, SUM(m.watch_time_sec)/3600.0 watch_h,
               SUM(m.followers_gained) followers, AVG(c.cost_cents)/100.0 cost
        FROM posts p JOIN clips c ON c.id=p.clip_id JOIN creators cr ON cr.id=c.creator_id
        LEFT JOIN ({LATEST}) m ON m.post_id=p.id
        WHERE p.status='published' GROUP BY {col} ORDER BY avg_views DESC""")
    return [dict(r) for r in rows]


def recommendations() -> list[str]:
    tips = []
    for group, label in (("format", "format"), ("creator", "creator"), ("layout", "layout")):
        rows = [r for r in breakdown(group) if r["posts"] >= 3 and r["avg_views"] is not None]
        if len(rows) >= 2:
            best, worst = rows[0], rows[-1]
            if best["avg_views"] and worst["avg_views"] is not None and best["avg_views"] > 1.5 * (worst["avg_views"] or 1):
                tips.append(f"Double down on {label} '{best['k']}': {best['avg_views']:.0f} avg views vs "
                            f"{worst['avg_views']:.0f} for '{worst['k']}' ({best['posts']} vs {worst['posts']} posts).")
    for r in breakdown("format"):
        if r["completion"] is not None and r["posts"] >= 3 and r["completion"] < 45:
            tips.append(f"'{r['k']}' clips average {r['completion']:.0f}% viewed: cut them shorter or open on the punchline.")
    if not tips:
        tips.append("Not enough published posts yet. Recommendations appear after 3+ posts per format or creator.")
    return tips


def ledger_summary() -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT substr(date,1,7) month, "
        "SUM(CASE WHEN kind='revenue' THEN amount_cents ELSE 0 END)/100.0 revenue, "
        "SUM(CASE WHEN kind='cost' THEN amount_cents ELSE 0 END)/100.0 cost "
        "FROM ledger GROUP BY month ORDER BY month DESC LIMIT 12")]
