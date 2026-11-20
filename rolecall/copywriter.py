"""Titles, captions, descriptions and tags, written per clip and per platform.

Default: a free rule-based writer that pulls the strongest quote from the clip.
Optional: Claude (set ANTHROPIC_API_KEY) writes sharper copy and reranks moments. It is a paid
service, so it is off until you add a key.
"""

from __future__ import annotations

import json
import logging
import re

from .config import get_config
from .rank import LAUGH, RP, SURPRISE

log = logging.getLogger(__name__)

BASE_TAGS = ["gtarp", "gta5", "gtav", "gtaroleplay", "roleplay", "gaming"]

SCENARIOS = [
    (re.compile(r"\b(judge|court|lawyer|trial|sentence|bail|plead)\b", re.I), "Court"),
    (re.compile(r"\b(heist|bank|robb\w*|hostage|vault)\b", re.I), "Heist"),
    (re.compile(r"\b(chase|pursuit|running|fleeing|flee)\b", re.I), "Chase"),
    (re.compile(r"\b(cop|cops|officer|police|pd|sheriff|trooper|dispatch|suspect|cuff\w*|arrest\w*|"
                r"pull(ed)? (you )?over|license and registration)\b", re.I), "Cop"),
    (re.compile(r"\b(ems|doctor|hospital|medic)\b", re.I), "EMS"),
    (re.compile(r"\b(mechanic|repair)\b", re.I), "Mechanic"),
    (re.compile(r"\b(taxi|uber|driver)\b", re.I), "Taxi"),
]


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" -")


def best_quote(segments: list[dict], max_len: int = 70, avoid: set[str] | None = None) -> str:
    """Pick the punchiest short line: cue words, exclamations, questions; prefer later lines (payoff)."""
    best, best_score = "", -1.0
    n = len(segments)
    avoid = {a.lower() for a in (avoid or set())}
    for i, seg in enumerate(segments):
        t = _clean(seg.get("text", ""))
        if not t or len(t) < 6 or LAUGH.fullmatch(t.strip(" !.?,")):
            continue
        if any(t.lower()[:40] in a for a in avoid):
            continue
        sc = 0.0
        uniq = lambda rx: len({m.group(0).lower() for m in rx.finditer(t)})  # noqa: E731
        sc += 2.0 * uniq(SURPRISE) + 1.5 * uniq(LAUGH) + 1.0 * uniq(RP)
        sc += 1.5 * t.count("!") + 1.0 * t.count("?")
        sc += 1.0 * (i / max(1, n - 1))           # later lines are usually the payoff
        sc -= max(0, len(t) - max_len) / 15        # shorter is quotable
        if sc > best_score:
            best, best_score = t, sc
    if len(best) > max_len:
        cut = best[:max_len].rsplit(" ", 1)[0]
        best = cut.rstrip(",.;:") + "..."
    return best


def scenario(text: str) -> str | None:
    for rx, name in SCENARIOS:
        if rx.search(text):
            return name
    return None


def rule_based(segments: list[dict], streamer: str, credit: str, fmt: str, avoid: set[str] | None = None) -> dict:
    text = " ".join(s.get("text", "") for s in segments)
    quote = best_quote(segments, avoid=avoid)
    scen = scenario(text)
    hook = f"\"{quote}\"" if quote else (f"{scen} RP went sideways" if scen else "This RP went sideways")
    label = f"{scen} RP" if scen else "GTA RP"
    yt_title = f"{hook} | {streamer} {label}"
    if len(yt_title) > 95:
        yt_title = f"{hook[:95 - len(label) - 3]} | {label}"
    tags = BASE_TAGS + [re.sub(r"[^a-z0-9]", "", streamer.lower())]
    if scen:
        tags.append(f"gtarp{scen.lower()}")
    hashtags = " ".join(f"#{t}" for t in tags[:6])
    return {
        "title": yt_title,
        "youtube": {
            "title": yt_title[:100],
            "description": (f"{hook}\n\nStreamer: {streamer} ({credit})\n"
                            f"Clipped and shared with the streamer's permission.\n\n{hashtags} #shorts"),
            "tags": list(dict.fromkeys(["gta rp", "gta 5 rp", "gta roleplay", streamer, label.lower(), "rolecall"])),
        },
        "instagram": {
            "caption": f"{hook}\n\nClip from {streamer} - {credit}\n\n{hashtags} #gtarpclips #funnymoments",
        },
        "tiktok": {
            "caption": f"{hook} (via {credit}) {hashtags}",
        },
        "writer": "rules",
    }


COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "youtube_title": {"type": "string"},
        "youtube_description": {"type": "string"},
        "youtube_tags": {"type": "array", "items": {"type": "string"}},
        "instagram_caption": {"type": "string"},
        "tiktok_caption": {"type": "string"},
    },
    "required": ["youtube_title", "youtube_description", "youtube_tags", "instagram_caption", "tiktok_caption"],
    "additionalProperties": False,
}

SYSTEM = """You write short-form copy for Rolecall, a channel that posts GTA roleplay (RP) clips with the
streamer's permission. Viewers scroll fast: the title must make them stay for the payoff without
spoiling it. Rules:
- Base everything on the transcript. Never invent events, names or quotes that are not in it.
- YouTube title: max 80 characters, curiosity-driven, may quote the funniest line, include "GTA RP".
- YouTube description: 2-3 lines plus the streamer credit line given to you, then 3-5 hashtags incl. #shorts.
- YouTube tags: 5-10 lowercase search phrases.
- Instagram caption: hook line, credit line, 5-8 hashtags.
- TikTok caption: under 150 characters, hook + credit + 3-5 hashtags.
- Always include the credit exactly as given. No slurs, no engagement bait like "like and subscribe".
- No claims that the clip is official or affiliated with Rockstar Games or any RP server."""


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=get_config().anthropic_api_key, max_retries=3)


def _ask_json(prompt: str, schema: dict) -> dict | None:
    import anthropic

    cfg = get_config()
    try:
        resp = _client().beta.messages.create(
            model=cfg.anthropic_model,
            max_tokens=4000,
            system=SYSTEM,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.RateLimitError as e:
        log.warning("Claude rate limited: %s", e.message)
        return None
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s: %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Claude unreachable; using rule-based copy")
        return None
    if resp.stop_reason == "refusal":
        log.warning("Claude declined the copy request; using rule-based copy")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return None
    usage = resp.usage
    # List prices in USD per million input/output tokens, used for cost-per-clip.
    price_in, price_out = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10),
                           "claude-haiku-5-5": (0.10, 0.50)}.get(cfg.anthropic_model, (4, 20))
    cost_cents = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000 * 100
    data = json.loads(text)
    data["_cost_cents"] = round(cost_cents, 3)
    return data


def write_copy(segments: list[dict], streamer: str, credit: str, fmt: str, avoid: set[str] | None = None) -> dict:
    base = rule_based(segments, streamer, credit, fmt, avoid)
    if not get_config().anthropic_api_key:
        return base
    transcript = "\n".join(f"[{s['start']:.1f}s] {s['text']}" for s in segments)
    taken = "\n".join(f"- {t}" for t in sorted(avoid or [])) or "(none)"
    prompt = (f"Streamer: {streamer}\nCredit line (use exactly): {credit}\nClip format: {fmt}\n"
              f"Titles already used for other clips from this stream (write a different angle):\n{taken}\n\n"
              f"Transcript of the clip:\n{transcript}")
    data = _ask_json(prompt, COPY_SCHEMA)
    if not data:
        return base
    return {
        "title": data["youtube_title"],
        "youtube": {"title": data["youtube_title"][:100], "description": data["youtube_description"],
                    "tags": data["youtube_tags"][:15]},
        "instagram": {"caption": data["instagram_caption"]},
        "tiktok": {"caption": data["tiktok_caption"][:2200]},
        "writer": get_config().anthropic_model,
        "cost_cents": data.get("_cost_cents", 0),
    }


RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ratings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "hook": {"type": "integer"},
                               "context": {"type": "integer"}, "payoff": {"type": "integer"}},
                "required": ["id", "hook", "context", "payoff"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ratings"],
    "additionalProperties": False,
}


def rerank(candidates: list[dict]) -> dict[int, float] | None:
    """Optional LLM pass: {candidate_id: 0..1}. candidates: [{id, text}]."""
    if not get_config().anthropic_api_key or not candidates:
        return None
    body = "\n\n".join(f"### Candidate {c['id']}\n{c['text']}" for c in candidates)
    prompt = ("Rate each GTA RP clip candidate from its transcript, 0-10 on: hook (grabs attention in the "
              "first 3 seconds), context (a stranger understands it without the rest of the stream), "
              "payoff (ends on a punchline, twist or reaction).\n\n" + body)
    data = _ask_json(prompt, RERANK_SCHEMA)
    if not data:
        return None
    return {r["id"]: (0.35 * r["hook"] + 0.25 * r["context"] + 0.4 * r["payoff"]) / 10 for r in data["ratings"]}
