import numpy as np

from rolecall import copywriter, review
from rolecall.captions import group_words
from rolecall.render import keep_segments, remap
from rolecall.rank import find_moments

from .conftest import make_creator


def W(word, s, e, p=0.95):
    return {"word": word, "start": s, "end": e, "prob": p}


def test_keep_segments_never_cuts_loud_untranscribed_audio():
    words = [W("hey", 0.2, 0.6), W("there", 5.0, 5.4)]
    hop = 0.1
    loud = np.full(60, -60.0)
    loud[20:35] = -12.0  # 2.0s-3.5s: laughter the transcriber missed
    segs = keep_segments(words, 6.0, loud_db=loud, hop=hop)
    kept = sum(b - a for a, b in segs)
    covered = lambda t: any(a <= t <= b for a, b in segs)  # noqa: E731
    assert covered(2.5) and covered(3.4), "loud reaction must be kept"
    assert not covered(4.3), "quiet dead air should be trimmed"
    assert kept < 6.0


def test_keep_segments_without_audio_info_keeps_everything():
    assert keep_segments([W("a", 1, 1.2)], 10.0, loud_db=None) == [(0.0, 10.0)]


def test_remap_through_cuts():
    segs = [(0.0, 2.0), (5.0, 7.0)]
    assert remap(1.0, segs) == 1.0
    assert remap(5.5, segs) == 2.5
    assert remap(3.0, segs) is None


def test_caption_grouping_breaks_on_punctuation_and_gaps():
    groups = group_words([W("No", 0, .2), W("way.", .2, .5), W("Bro", 1.5, 1.7), W("what", 1.7, 1.9),
                          W("are", 1.9, 2.0), W("you", 2.0, 2.1)])
    assert [len(g) for g in groups] == [2, 3, 1]


def test_find_moments_prefers_clip_ending_on_laughter():
    segs, t = [], 1.0
    lines = ["Sir do you know why I pulled you over", "Because you were lonely", "License and registration",
             "Here is my library card", "This is a library card", "It has my picture on it!", "Ha ha ha ha"]
    for line in lines:
        words = [W(w, t + i * 0.3, t + i * 0.3 + 0.25) for i, w in enumerate(line.split())]
        segs.append({"start": words[0]["start"], "end": words[-1]["end"], "text": line, "words": words})
        t = words[-1]["end"] + 0.7
    dur = t + 30
    loud = np.full(int(dur / 0.25), -45.0)
    for s in segs:
        loud[int(s["start"] / .25):int(s["end"] / .25)] = -20
    loud[int(segs[-2]["start"] / .25):int(segs[-1]["end"] / .25)] = -6  # shouting + laughter
    moments = find_moments({"segments": segs}, loud, dur, ["punch"])
    assert moments, "should find a punch clip"
    best = moments[0]
    assert best.end >= segs[-1]["end"] - 0.01, "clip should include the laughter payoff"
    assert best.start <= segs[0]["start"] + 0.2, "clip should keep the setup"


def test_pii_and_slur_flags():
    flags = review.content_flags("call me at 555-123-4567 you retard", [W("x", 0, 1)], 20, None, None, [])
    codes = {f["code"] for f in flags}
    assert {"pii_suspected", "slur"} <= codes
    assert all(f["severity"] == "block" for f in flags if f["code"] in {"pii_suspected", "slur"})


def test_music_marker_flag():
    flags = review.content_flags("hi", [W("hi", 0, 1)], 20, None, None, [{"text": "♪ la la ♪"}])
    assert any(f["code"] == "music_suspected" for f in flags)


def test_permission_flags(env):
    cid = make_creator(monetize=False, platforms=("youtube",))
    flags, allowed = review.permission_flags(cid, ["youtube", "tiktok"])
    codes = {f["code"] for f in flags}
    assert "no_monetize" in codes and "platform_not_covered" in codes
    assert allowed == ["youtube"]


def test_unverified_permission_blocks(env):
    cid = make_creator(verified=False)
    flags, _ = review.permission_flags(cid, ["youtube"])
    assert any(f["code"] == "permission_unverified" and f["severity"] == "block" for f in flags)


def test_rule_based_copy_uses_quote_and_credit():
    segs = [{"start": 0, "end": 2, "text": "Officer, I would like to report a wall."},
            {"start": 2, "end": 3, "text": "Are you serious right now?"}]
    c = copywriter.rule_based(segs, "Streamer", "twitch.tv/streamer", "punch")
    assert "twitch.tv/streamer" in c["instagram"]["caption"] and "twitch.tv/streamer" in c["tiktok"]["caption"]
    assert len(c["youtube"]["title"]) <= 100
    assert "Cop RP" in c["title"] or "GTA RP" in c["title"]
    other = copywriter.rule_based(segs, "Streamer", "x", "punch", avoid={c["title"]})
    assert other["title"] != c["title"]
