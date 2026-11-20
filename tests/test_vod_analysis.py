import wave

import numpy as np
import pytest

from rolecall import media
from rolecall.rank import find_moments


@pytest.mark.parametrize("windows,extra", [(0, 0), (0, 19), (128, 0), (128, 7), (259, 31)])
def test_file_loudness_matches_whole_decode(tmp_path, windows, extra):
    sr = 8000
    hop = 0.125
    count = windows * int(sr * hop) + extra
    samples = np.random.default_rng(3).integers(-30000, 30000, size=count, dtype=np.int16)
    path = tmp_path / "recording.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, sr, 0, "NONE", "not compressed"))
        wav.writeframes(samples.astype("<i2").tobytes())

    expected = media.loudness_curve(media.load_audio(path, sr), sr, hop)
    np.testing.assert_array_equal(media.file_loudness_curve(path, sr, hop), expected)


def test_file_loudness_reports_decoder_failure(tmp_path):
    with pytest.raises(media.MediaError, match="ffmpeg failed"):
        media.file_loudness_curve(tmp_path / "missing.mp4")


def test_late_payoff_keeps_setup_scores_and_reasons():
    def segment(t, text):
        words = [{"start": t + i * 0.5, "end": t + i * 0.5 + 0.4, "word": token}
                 for i, token in enumerate(text.split())]
        return {"start": t, "end": words[-1]["end"], "words": words, "text": text}

    lines = ["Officer I can explain", "I was parking here", "I do have a license",
             "I left it in the taxi", "No way you drove here!", "Haha good one!"]
    offset = 7200
    scene = [segment(offset + 2 + i * 3, line) for i, line in enumerate(lines)]
    duration = offset + 30
    loud = np.full(duration * 4, -45.0)
    loud[offset * 4 + 58:offset * 4 + 76] = -10.0
    expected = find_moments({"segments": scene}, loud, duration, ["punch"])
    best = expected[0]
    assert best.start == pytest.approx(scene[0]["start"] - 0.1)
    assert best.end >= scene[-1]["end"]
    assert {"RP scenario", "laughter at the end"} <= set(best.reasons)

    earlier = [segment(t, "ordinary driving") for t in range(0, offset - 60, 4)]
    assert find_moments({"segments": earlier + scene}, loud, duration, ["punch"]) == expected
