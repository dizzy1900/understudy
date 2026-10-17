import asyncio
import base64
import struct
from types import SimpleNamespace

import httpx
import openai
import pytest
from starlette.testclient import TestClient

from tests.agent_support import ScriptedChat, make_context
from understudy import llm, voice
from understudy.agents.chaos import ChaosOps
from understudy.server import create_app

AUDIO_ERROR = "Please install vllm[audio] for audio support"


def wav(frames: int = 1600, rate: int = 16000) -> bytes:
    pcm = b"\x01\x00" * frames
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def webm() -> bytes:
    return b"\x1a\x45\xdf\xa3" + b"\x9f\x42\x86\x81\x01" * 100


def api_error(status: int, message: str) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request)
    body = {"error": {"message": message, "type": "InternalServerError", "code": status}}
    return openai.APIStatusError(message, response=response, body=body)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def served(tmp_path):
    orch = SimpleNamespace(promoter=None)
    with TestClient(create_app(orch, web_dist=None)) as client:
        chat = ScriptedChat()
        ctx = client.portal.call(make_context, tmp_path, chat)
        orch.ctx, orch.db, orch.bus, orch.state = ctx, ctx.db, ctx.bus, ctx.state
        orch.chaos = ChaosOps(ctx)
        yield client, orch, chat
        client.portal.call(ctx.db.close)


def post(client, body, content_type="audio/wav"):
    return client.post("/api/voice", content=body, headers={"content-type": content_type})


# format detection and limits -----------------------------------------------------------------


@pytest.mark.parametrize(
    "data, declared, expected",
    [
        (wav(), "audio/wav", "wav"),
        (wav(), "application/octet-stream", "wav"),
        (webm(), "audio/webm;codecs=opus", "webm"),
        (webm(), None, "webm"),
        (b"ID3\x04" + b"\0" * 20, "audio/mpeg", "mp3"),
        (b"\xff\xfb\x90\x00" + b"\0" * 20, None, "mp3"),
        (b"OggS\0\x02" + b"\0" * 20, "audio/ogg", "ogg"),
        (b"\0\0\0\x20ftypM4A " + b"\0" * 20, "audio/mp4", "mp4"),
        (b"\0" * 40, "audio/webm; codecs=opus", "webm"),
        (b"\0" * 40, "AUDIO/MPEG", "mp3"),
        (b"\0" * 40, "text/plain", None),
        (b"\0" * 40, None, None),
    ],
)
def test_detect_format(data, declared, expected):
    assert voice.detect_format(declared, data) == expected


def test_magic_bytes_win_over_the_declared_type():
    assert voice.detect_format("audio/webm", wav()) == "wav"


def test_wav_frames_counts_samples_and_handles_streamed_sizes():
    assert voice.wav_frames(wav(1600)) == 1600
    streamed = bytearray(wav(800))
    streamed[40:44] = struct.pack("<I", 0xFFFFFFFF)
    assert voice.wav_frames(bytes(streamed)) == 800
    assert voice.wav_frames(b"not a wav") is None
    assert voice.wav_frames(wav(0)) == 0


def test_check_audio_limits():
    assert voice.check_audio(wav(), "audio/wav") == "wav"
    with pytest.raises(voice.VoiceError) as exc:
        voice.check_audio(b"\x1a\x45\xdf\xa3" + b"\0" * voice.MAX_BYTES, "audio/webm")
    assert exc.value.status == 413
    with pytest.raises(voice.VoiceError) as exc:
        voice.check_audio(b"", "audio/wav")
    assert exc.value.status == 422
    padded = wav(0)[:36] + b"LIST" + struct.pack("<I", 300) + b"\0" * 300 + wav(0)[36:]
    with pytest.raises(voice.VoiceError) as exc:
        voice.check_audio(padded, "audio/wav")
    assert exc.value.status == 422 and "no audio samples" in exc.value.detail
    with pytest.raises(voice.VoiceError) as exc:
        voice.check_audio(b"\0" * 1000, "text/plain")
    assert exc.value.status == 415 and "wav" in exc.value.detail and "webm" in exc.value.detail


def test_tone_clip_is_a_valid_short_wav():
    clip = voice.make_tone_wav()
    assert voice.detect_format(None, clip) == "wav"
    assert voice.wav_frames(clip) == int(0.6 * 16000)
    assert len(clip) < 25_000


# transcript cleanup --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("storm rows two and three", "storm rows two and three"),
        ('"storm rows two and three"', "storm rows two and three"),
        ("\u201cI'm back\u201d", "I'm back"),
        ("Transcript: surprise me", "surprise me"),
        ("<think>they said something</think>\nwhat happened", "what happened"),
        ("<think>never closed and cut off", ""),
        ("```\nfill the disk on rack three-four\n```", "fill the disk on rack three-four"),
        ('{"transcript": "break r1-02\'s model id"}', "break r1-02's model id"),
        ('"surprise me"\n\nThis is the exact transcription of the audio.', "surprise me"),
        ('"surprise me" (spoken clearly)', "surprise me"),
        ("  storm   rows\ttwo  ", "storm rows two"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_transcript(raw, expected):
    assert voice.clean_transcript(raw) == expected


# status ---------------------------------------------------------------------------------------


def test_status_reports_missing_omni_without_calling_the_model(served):
    client, orch, chat = served

    def missing(key):
        raise llm.ModelUnavailable(f"model {key!r} is not available to this project")

    orch.ctx.resolve = missing
    body = client.get("/api/voice/status").json()
    assert body["available"] is False and body["path"] == "browser"
    assert "not available to this project" in body["reason"]
    assert body["model"] == "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B"
    assert chat.calls == []


def test_status_is_available_after_one_successful_self_test_and_caches_it(served):
    client, _, chat = served
    chat.queues["voice"] = [""]
    first = client.get("/api/voice/status").json()
    assert first == {
        "available": True, "path": "omni", "formats": ["wav"], "reason": "",
        "model": "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B",
    }
    assert client.get("/api/voice/status").json()["available"] is True
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call["role"] == "voice" and call["model_key"] == "omni" and call["thinking"] is False
    assert call["meta"] == {"selftest": True}
    part = call["messages"][-1]["content"][1]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"


def test_status_failure_is_cached_then_retried(served):
    client, orch, chat = served
    clock = Clock()
    state = client.app.state.voice
    state.clock = clock
    chat.queues["voice"] = [api_error(500, AUDIO_ERROR), ""]
    body = client.get("/api/voice/status").json()
    assert body["available"] is False and body["path"] == "browser"
    assert AUDIO_ERROR in body["reason"] and "HTTP 500" in body["reason"]
    for _ in range(3):
        assert client.get("/api/voice/status").json()["available"] is False
    assert len(chat.calls) == 1
    clock.now += state.retry_after_s + 1
    assert client.get("/api/voice/status").json()["available"] is True
    assert len(chat.calls) == 2


def test_status_transient_failure_does_not_claim_availability(served):
    client, _, chat = served
    chat.queues["voice"] = [api_error(502, "bad gateway")]
    body = client.get("/api/voice/status").json()
    assert body["available"] is False and "omni model failed" in body["reason"]


# endpoint -------------------------------------------------------------------------------------


def test_transcribe_sends_an_input_audio_part_and_returns_clean_text(served):
    client, orch, chat = served
    chat.queues["voice"] = ['"storm rows two and three"']
    audio = wav()
    reply = post(client, audio)
    assert reply.status_code == 200 and reply.json() == {"transcript": "storm rows two and three"}
    call = chat.of("voice")[0]
    assert call["model_key"] == "omni" and call["thinking"] is False
    user = call["messages"][-1]["content"]
    assert user[0] == {"type": "text", "text": "Transcribe the spoken command exactly."}
    assert user[1]["type"] == "input_audio"
    assert base64.b64decode(user[1]["input_audio"]["data"]) == audio
    assert user[1]["input_audio"]["format"] == "wav"
    assert call["meta"]["format"] == "wav"
    assert client.get("/api/voice/status").json()["available"] is True
    assert len(chat.calls) == 1


def test_webm_is_forwarded_as_webm(served):
    client, _, chat = served
    chat.queues["voice"] = ["surprise me"]
    reply = post(client, webm(), "audio/webm;codecs=opus")
    assert reply.json() == {"transcript": "surprise me"}
    assert chat.of("voice")[0]["messages"][-1]["content"][1]["input_audio"]["format"] == "webm"


def test_unsupported_format_is_415_and_names_the_accepted_ones(served):
    client, _, chat = served
    reply = post(client, b"\0" * 2000, "text/plain")
    assert reply.status_code == 415
    for name in ("wav", "webm", "mp3", "ogg", "mp4"):
        assert name in reply.json()["detail"]
    assert chat.calls == []


def test_too_large_is_413_by_header_and_by_stream(served):
    client, _, chat = served
    big = b"RIFF" + b"\0" * (voice.MAX_BYTES + 1)
    assert post(client, big).status_code == 413
    assert post(client, (c for c in [b"RIFF" + b"\0" * voice.MAX_BYTES, b"\0\0"])).status_code == 413
    assert chat.calls == []


def test_empty_and_unreadable_audio_is_422(served):
    client, _, chat = served
    assert post(client, b"").status_code == 422
    assert post(client, wav(0)).status_code == 422
    assert post(client, b"RIFF" + b"\0" * 400).status_code == 422
    assert chat.calls == []


def test_empty_transcript_is_422(served):
    client, _, chat = served
    chat.queues["voice"] = ["   ", "<think>hm</think>"]
    for _ in range(2):
        reply = post(client, wav())
        assert reply.status_code == 422 and "no speech" in reply.json()["detail"]


def test_upstream_audio_not_supported_is_501_with_the_reason_and_is_remembered(served):
    client, _, chat = served
    chat.queues["voice"] = [api_error(500, AUDIO_ERROR)]
    reply = post(client, wav())
    assert reply.status_code == 501 and AUDIO_ERROR in reply.json()["detail"]
    again = post(client, wav())
    assert again.status_code == 501 and again.json()["detail"] == reply.json()["detail"]
    assert len(chat.calls) == 1
    status = client.get("/api/voice/status").json()
    assert status["available"] is False and status["reason"] == reply.json()["detail"]
    assert len(chat.calls) == 1


@pytest.mark.parametrize(
    "error, status",
    [
        (api_error(400, "Incorrect padding"), 422),
        (api_error(400, "audio input is not supported by this model"), 501),
        (api_error(429, "rate limited"), 503),
        (api_error(502, "bad gateway"), 502),
        (llm.BudgetExceeded(5.0, 5.0), 503),
        (llm.ModelUnavailable("model 'omni' is not available to this project"), 501),
        (httpx.ConnectError("refused"), 502),
    ],
)
def test_upstream_failures_map_to_statuses(served, error, status):
    client, _, chat = served
    chat.queues["voice"] = [error]
    assert post(client, wav()).status_code == status


def test_missing_omni_is_501(served):
    client, orch, chat = served

    def missing(key):
        raise llm.ModelUnavailable("model 'omni' is not available to this project")

    orch.ctx.resolve = missing
    reply = post(client, wav())
    assert reply.status_code == 501 and "not available" in reply.json()["detail"]
    assert chat.calls == []


def test_exhausted_budget_is_503_without_a_call(served):
    client, orch, chat = served

    class Gate:
        limit = 5.0

        async def exhausted(self):
            return True

    orch.ctx.gate = Gate()
    reply = post(client, wav())
    assert reply.status_code == 503 and "budget" in reply.json()["detail"]
    assert chat.calls == []


def test_transcribe_helper_is_usable_without_a_server(tmp_path):
    async def run():
        ctx = await make_context(tmp_path, ScriptedChat(voice=["what happened"]))
        try:
            return await voice.transcribe(ctx, wav(), "audio/wav")
        finally:
            await ctx.db.close()

    assert asyncio.run(run()) == "what happened"
