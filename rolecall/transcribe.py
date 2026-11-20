"""Local speech-to-text with word timestamps (faster-whisper; free, runs on CPU)."""

from __future__ import annotations

import threading
from pathlib import Path

from .config import get_config

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            cfg = get_config()
            _model = WhisperModel(cfg.whisper_model, device=cfg.whisper_device, compute_type="int8",
                                  download_root=str(cfg.dir("models")))
        return _model


CHUNK_S = 600.0  # decode + transcribe 10 minutes at a time so multi-hour VODs stay within RAM


def transcribe(path: str | Path) -> dict:
    """Return {"language", "segments": [{start, end, text, avg_logprob, no_speech_prob,
    words: [{start, end, word, prob}]}]}. Audio is decoded by ffmpeg (no PyAV dependency)."""
    from .media import load_audio, probe

    model = _get_model()
    total = probe(path)["duration"]
    out: list[dict] = []
    language = None
    offset = 0.0
    while offset < total - 0.05:
        audio = load_audio(path, sr=16000, start=offset, duration=CHUNK_S)
        if len(audio) < 1600:
            break
        segments, info = model.transcribe(
            audio,
            language="en" if get_config().whisper_model.endswith(".en") else None,
            word_timestamps=True,
            vad_filter=True,
            # lower threshold than the 0.5 default: shouting over engines/music still counts as speech
            vad_parameters={"min_silence_duration_ms": 400, "threshold": 0.3, "speech_pad_ms": 300},
            condition_on_previous_text=False,  # reduces hallucinated repeats on gameplay noise
            beam_size=1,
        )
        language = language or info.language
        for s in segments:
            words = [
                {"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": w.word,
                 "prob": round(w.probability, 3)}
                for w in (s.words or [])
            ]
            out.append({
                "start": round(s.start + offset, 3), "end": round(s.end + offset, 3), "text": s.text.strip(),
                "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                "words": words,
            })
        offset += CHUNK_S
    return {"language": language, "duration": total, "segments": out}


def words_in(transcript: dict, start: float, end: float) -> list[dict]:
    """Words fully or mostly inside [start, end], re-timed relative to `start`."""
    res = []
    for seg in transcript.get("segments", []):
        if seg["end"] < start or seg["start"] > end:
            continue
        for w in seg["words"]:
            mid = (w["start"] + w["end"]) / 2
            if start <= mid <= end:
                res.append({**w, "start": max(0.0, w["start"] - start), "end": min(end, w["end"]) - start})
    return res


def text_in(transcript: dict, start: float, end: float) -> str:
    return " ".join(w["word"].strip() for w in words_in(transcript, start, end)).strip()
