"""Speech input: audio bytes to a transcript through the OMNI model."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from config import settings
from understudy import llm

log = logging.getLogger("understudy.voice")

MODEL_KEY = "omni"
ROLE = "voice"
PROMPT = "Transcribe the spoken command exactly."
SYSTEM = "You transcribe speech. Reply with the transcript only: no quotes, labels or commentary."

MAX_BYTES = 8 * 1024 * 1024
MIN_BYTES = 256
MAX_TOKENS = 200
RETRY_AFTER_FAILURE_S = 300.0

# Formats the endpoint forwards, keyed by the name sent as input_audio.format.
FORMATS = ("wav", "webm", "mp3", "ogg", "mp4")
# What a client should upload. Only wav has a self-test behind it, so browsers convert to it.
PREFERRED = ("wav",)

_CONTENT_TYPES = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
    "audio/webm": "webm", "video/webm": "webm",
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/ogg": "ogg", "application/ogg": "ogg",
    "audio/mp4": "mp4", "audio/m4a": "mp4", "audio/x-m4a": "mp4", "audio/aac": "mp4",
}

_UNSUPPORTED = re.compile(
    r"vllm\[audio\]|audio.{0,40}(not supported|unsupported|support|install)|"
    r"(not supported|unsupported|does not support).{0,40}audio|input_audio",
    re.IGNORECASE | re.DOTALL,
)
_UNDECODABLE = re.compile(r"padding|decod|corrupt|invalid|could not (read|load)|format not recognised", re.IGNORECASE)


class VoiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def detect_format(content_type: str | None, data: bytes) -> str | None:
    """Container sniffed from magic bytes, falling back to the declared type (parameters ignored)."""
    head = data[:16]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    declared = (content_type or "").split(";")[0].strip().lower()
    return _CONTENT_TYPES.get(declared)


def wav_frames(data: bytes) -> int | None:
    """Sample frames in a PCM WAV, or None when the header cannot be read."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, frame_bytes = 12, 0
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if tag == b"fmt " and pos + 8 + 16 <= len(data):
            _, channels, _, _, _, bits = struct.unpack("<HHIIHH", data[pos + 8:pos + 24])
            frame_bytes = channels * bits // 8
        elif tag == b"data":
            # Streamed WAVs leave the size at 0 or 0xFFFFFFFF; use what is actually there.
            available = len(data) - pos - 8
            size = available if size in (0, 0xFFFFFFFF) or size > available else size
            return size // frame_bytes if frame_bytes else None
        pos += 8 + size + (size & 1)
    return None


def check_audio(data: bytes, content_type: str | None) -> str:
    """Returns the format name, or raises VoiceError (413, 415 or 422)."""
    if len(data) > MAX_BYTES:
        raise VoiceError(413, f"audio is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if len(data) < MIN_BYTES:
        raise VoiceError(422, "audio is empty or too short")
    fmt = detect_format(content_type, data)
    if fmt is None:
        raise VoiceError(415, f"unsupported audio format; send one of: {', '.join(FORMATS)}")
    if fmt == "wav" and not wav_frames(data):
        raise VoiceError(422, "wav has no audio samples")
    return fmt


def make_tone_wav(seconds: float = 0.6, rate: int = 16000, freq: float = 440.0) -> bytes:
    """16-bit mono PCM sine, used as the self-test clip."""
    count = int(seconds * rate)
    pcm = b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * n / rate))) for n in range(count)
    )
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)
_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$", re.IGNORECASE)
_LABEL = re.compile(r"^(transcript(ion)?|command|speech|text)\s*[:\-]\s*", re.IGNORECASE)
_QUOTES = "\"'`\u201c\u201d\u2018\u2019\u00ab\u00bb"
_JSON_KEYS = ("transcript", "transcription", "text", "command")


def clean_transcript(text: str | None) -> str:
    """Plain text of what was said: no reasoning, fences, JSON wrapper, label, quotes or trailing remarks."""
    if not text:
        return ""
    text = _THINK.sub("", text).strip()
    text = _FENCE.sub("", text).strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for key in _JSON_KEYS:
                if isinstance(obj.get(key), str):
                    text = obj[key]
                    break
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    line = _LABEL.sub("", lines[0]).strip()
    # A quoted answer followed by commentary: keep the quoted part.
    match = re.match(r"^[\"\u201c\u2018']([^\"\u201d\u2019]+)[\"\u201d\u2019']\s*(?:[(\[-].*)?$", line)
    if match:
        line = match.group(1)
    line = line.strip(_QUOTES + " ")
    return " ".join(line.split())


def failure_for(exc: BaseException) -> VoiceError:
    """Maps an upstream exception to the HTTP status the endpoint answers with."""
    if isinstance(exc, VoiceError):
        return exc
    if isinstance(exc, llm.BudgetExceeded):
        return VoiceError(503, "the LLM budget cap is reached; voice input is paused")
    if isinstance(exc, (llm.ModelUnavailable, KeyError)):
        return VoiceError(501, "the omni model is not available to this project")
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        err = body.get("error", body)
        message = str(err.get("message", "")) if isinstance(err, dict) else str(err)
    message = message or getattr(exc, "message", "") or str(exc)
    message = " ".join(message.split())[:200]
    if _UNSUPPORTED.search(message):
        return VoiceError(501, f"the omni endpoint cannot decode audio: {message} (HTTP {status or 'error'})")
    if status in (400, 415, 422) or (status is None and _UNDECODABLE.search(message)):
        return VoiceError(422, f"the audio could not be decoded: {message}")
    if status == 429:
        return VoiceError(503, "the omni model is rate limited; try again shortly")
    return VoiceError(502, f"the omni model failed: {type(exc).__name__}: {message}"[:240])


@dataclass
class VoiceState:
    """What is known about whether OMNI transcription works, for the life of the process."""

    clock: Callable[[], float] = time.monotonic
    retry_after_s: float = RETRY_AFTER_FAILURE_S
    works: bool = False
    reason: str = ""
    checked_at: float | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def mark_ok(self) -> None:
        self.works, self.reason, self.checked_at = True, "", self.clock()

    def mark_failed(self, reason: str) -> None:
        self.works, self.reason, self.checked_at = False, reason, self.clock()

    def stale(self) -> bool:
        if self.checked_at is None:
            return True
        return not self.works and self.clock() - self.checked_at >= self.retry_after_s

    def blocked(self) -> str | None:
        """The cached failure reason while a retry is not yet due."""
        if self.checked_at is not None and not self.works and not self.stale():
            return self.reason
        return None


async def transcribe(ctx: Any, audio: bytes, content_type: str | None, state: VoiceState | None = None) -> str:
    """Transcript of `audio`. `ctx` needs .chat and .resolve (agents.context.Context). Raises VoiceError."""
    fmt = check_audio(audio, content_type)
    if state is not None and (cached := state.blocked()):
        raise VoiceError(501, cached)
    try:
        ctx.resolve(MODEL_KEY)
    except Exception as exc:
        raise failure_for(exc) from None
    if ctx.gate is not None and await ctx.gate.exhausted():
        raise failure_for(llm.BudgetExceeded(0.0, ctx.gate.limit or 0.0))
    return await _ask(ctx, audio, fmt, state, meta={"bytes": len(audio), "format": fmt}, allow_empty=False)


async def _ask(ctx: Any, audio: bytes, fmt: str, state: VoiceState | None, meta: dict, allow_empty: bool) -> str:
    part = {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [{"type": "text", "text": PROMPT}, part]},
    ]
    try:
        result = await ctx.chat(
            ROLE, MODEL_KEY, messages, thinking=False, max_tokens=MAX_TOKENS, temperature=0.0, meta=meta
        )
    except Exception as exc:
        err = failure_for(exc)
        if err.status == 501 and state is not None:
            state.mark_failed(err.detail)
        if err.status == 502:
            log.warning("voice: %s", err.detail)
        raise err from None
    if state is not None:
        state.mark_ok()
    text = clean_transcript(result.content)
    if not text and not allow_empty:
        raise VoiceError(422, "no speech was recognised in the audio")
    return text


async def self_test(ctx: Any, state: VoiceState) -> None:
    """One short call with a generated clip. A reply of any kind shows the audio path is wired up."""
    async with state._lock:
        if not state.stale():
            return
        try:
            ctx.resolve(MODEL_KEY)
        except Exception as exc:
            state.mark_failed(failure_for(exc).detail)
            return
        if ctx.gate is not None and await ctx.gate.exhausted():
            state.mark_failed("the LLM budget cap is reached")
            return
        try:
            await _ask(ctx, make_tone_wav(), "wav", state, meta={"selftest": True}, allow_empty=True)
        except VoiceError as err:
            state.mark_failed(err.detail)


async def status(ctx: Any, state: VoiceState) -> dict[str, Any]:
    if state.stale():
        await self_test(ctx, state)
    try:
        model = ctx.resolve(MODEL_KEY)[1]
    except Exception:
        model = settings.MODELS[MODEL_KEY].id
    return {
        "available": state.works,
        "path": "omni" if state.works else "browser",
        "model": model,
        "formats": list(PREFERRED),
        "reason": "" if state.works else state.reason,
    }
