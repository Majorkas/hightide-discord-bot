"""Tests for the meeting-notes download/chunk/transcribe pipeline."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src import config
from src.services import meeting_service
from src.services.meeting_service import (
    MeetingError, _chunk_audio, download_attachment, is_audio_attachment,
    transcribe_audio,
)


# --- is_audio_attachment ---

@pytest.mark.parametrize("filename,ctype,expected", [
    ("meeting.m4a", None, True),
    ("meeting.mp3", None, True),
    ("meeting.wav", None, True),
    ("meeting.ogg", None, True),
    ("clip.mp4", None, True),
    ("notes.txt", None, False),
    ("image.png", None, False),
    ("noext", "audio/webm", True),       # content-type wins
    ("noext", "video/mp4", True),
    ("noext", "image/png", False),
    ("meeting.m4a", "audio/mp4", True),  # both signal audio
])
def test_is_audio_attachment(filename, ctype, expected):
    assert is_audio_attachment(filename, ctype) is expected


# --- download_attachment ---

def _http_client(status=200, content=b"audio-bytes", side_effect=None,
                 content_length=None):
    patcher = patch("src.services.meeting_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    if side_effect is not None:
        client.stream = MagicMock(side_effect=side_effect)
    else:
        resp = AsyncMock()
        resp.status_code = status
        resp.headers = {} if content_length is None else {"content-length": str(content_length)}
        async def _aiter(_chunk=8192):
            yield content
        resp.aiter_bytes = lambda chunk_size=8192: _aiter(chunk_size)
        stream_ctx = AsyncMock()
        stream_ctx.__aenter__.return_value = resp
        # httpx's .stream() is a sync method returning an async context manager.
        client.stream = MagicMock(return_value=stream_ctx)
    cls.return_value.__aenter__.return_value = client
    return patcher, client


async def test_download_success():
    p, _ = _http_client(content=b"data")
    try:
        assert await download_attachment("https://x/audio.m4a") == b"data"
    finally:
        p.stop()


async def test_download_non200_rejected():
    p, _ = _http_client(status=404)
    try:
        with pytest.raises(MeetingError, match="couldn't download"):
            await download_attachment("https://x/audio.m4a")
    finally:
        p.stop()


async def test_download_network_error_rejected():
    p, _ = _http_client(side_effect=httpx.ConnectError("down"))
    try:
        with pytest.raises(MeetingError, match="couldn't download"):
            await download_attachment("https://x/audio.m4a")
    finally:
        p.stop()


async def test_download_oversized_rejected():
    big = b"x" * (meeting_service._MAX_DOWNLOAD_BYTES + 1)
    p, _ = _http_client(content=big)
    try:
        with pytest.raises(MeetingError, match="too large"):
            await download_attachment("https://x/audio.m4a")
    finally:
        p.stop()


# --- _chunk_audio (mock pydub) ---

def test_chunk_audio_small_file_not_split(tmp_path, monkeypatch):
    f = tmp_path / "m.ogg"
    f.write_bytes(b"x" * 1000)  # well under the cap
    fake_audio = MagicMock()
    fake_audio.__len__.return_value = 1000
    monkeypatch.setattr("pydub.AudioSegment.from_file", lambda p: fake_audio)
    assert _chunk_audio(str(f)) == [str(f)]  # no split


def test_chunk_audio_large_file_splits(tmp_path, monkeypatch):
    f = tmp_path / "big.ogg"
    f.write_bytes(b"x" * (meeting_service._MAX_FILE_BYTES + 1))
    fake_audio = MagicMock()
    fake_audio.__len__.return_value = 60000  # 60s
    exported = []

    def _export(path, **kw):
        # create the chunk file so later steps can read it
        with open(path, "wb") as fh:
            fh.write(b"chunk")
        exported.append(path)

    fake_seg = MagicMock()
    fake_seg.__getitem__.return_value = MagicMock(export=_export)
    fake_audio.__getitem__ = fake_seg.__getitem__
    monkeypatch.setattr("pydub.AudioSegment.from_file", lambda p: fake_audio)
    out = _chunk_audio(str(f))
    assert len(out) >= 2 and exported  # split into multiple chunk files


def test_chunk_audio_respects_chunk_duration_cap(tmp_path, monkeypatch):
    """Chunk duration must never exceed the 64k cap so each export stays under the
    Whisper byte cap, regardless of source bytes — the proportional-bytes bug the
    council flagged. Duration kept under the 90-min processing cap so we exercise
    the chunking math, not the duration guard."""
    f = tmp_path / "long.ogg"
    f.write_bytes(b"x" * (meeting_service._MAX_FILE_BYTES + 1))  # just over the byte cap
    duration_ms = 80 * 60 * 1000  # 80 min (under the 90-min cap)
    fake_audio = MagicMock()
    fake_audio.__len__.return_value = duration_ms
    exported = []

    def _export(path, **kw):
        with open(path, "wb") as fh:
            fh.write(b"chunk")
        exported.append(path)

    fake_audio.__getitem__.return_value = MagicMock(export=_export)
    monkeypatch.setattr("pydub.AudioSegment.from_file", lambda p: fake_audio)
    out = _chunk_audio(str(f))
    # 80 min > _MAX_CHUNK_MS (~52 min) → must split into ≥2 chunks to respect the cap.
    cap = meeting_service._MAX_CHUNK_MS
    assert len(out) >= -(-duration_ms // cap)  # ceil division
    assert len(out) >= 2


def test_chunk_audio_too_long_rejected(tmp_path, monkeypatch):
    """Over the duration cap → MeetingError before any chunking (decompression guard)."""
    f = tmp_path / "epic.ogg"
    f.write_bytes(b"x" * (meeting_service._MAX_FILE_BYTES + 1))
    fake_audio = MagicMock()
    fake_audio.__len__.return_value = meeting_service._MAX_DURATION_MS + 1
    monkeypatch.setattr("pydub.AudioSegment.from_file", lambda p: fake_audio)
    with pytest.raises(MeetingError, match="too long"):
        _chunk_audio(str(f))


def test_chunk_audio_unreadable_raises_meeting_error(tmp_path, monkeypatch):
    f = tmp_path / "bad.ogg"
    f.write_bytes(b"garbage")
    monkeypatch.setattr("pydub.AudioSegment.from_file",
                        MagicMock(side_effect=Exception("not audio")))
    with pytest.raises(MeetingError, match="couldn't read"):
        _chunk_audio(str(f))


# --- transcribe_audio (mock chunking + whisper) ---

def _whisper_client(*statuses_texts):
    patcher = patch("src.services.meeting_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    responses = []
    for status, text in statuses_texts:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {"text": text}
        responses.append(resp)
    client.post.side_effect = responses
    cls.return_value.__aenter__.return_value = client
    return patcher, client


async def test_transcribe_single_chunk(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    monkeypatch.setattr(meeting_service, "_chunk_audio", lambda p: [p])  # no split
    p, client = _whisper_client((200, "hello world"))
    try:
        result = await transcribe_audio(b"bytes", "meeting.ogg")
    finally:
        p.stop()
    assert result.text == "hello world" and result.chunks == 1
    assert client.post.call_args.kwargs["data"]["model"] == "whisper-1"


async def test_transcribe_multiple_chunks_combined_in_order(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")

    def _two_chunks(path):
        for suffix in (".0", ".1"):
            with open(path + suffix, "wb") as fh:
                fh.write(b"chunk")
        return [path + ".0", path + ".1"]

    monkeypatch.setattr(meeting_service, "_chunk_audio", _two_chunks)
    p, _ = _whisper_client((200, "part one"), (200, "part two"))
    try:
        result = await transcribe_audio(b"bytes", "meeting.ogg")
    finally:
        p.stop()
    assert result.text == "part one part two" and result.chunks == 2


async def test_transcribe_chunk_failure_raises_meeting_error(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    monkeypatch.setattr(meeting_service, "_chunk_audio", lambda p: [p])
    p, _ = _whisper_client((500, ""))
    try:
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(MeetingError, match="transcription failed"):
                await transcribe_audio(b"bytes", "meeting.ogg")
    finally:
        p.stop()


async def test_transcribe_empty_result_raises(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    monkeypatch.setattr(meeting_service, "_chunk_audio", lambda p: [p])
    p, _ = _whisper_client((200, "   "))
    try:
        with pytest.raises(MeetingError, match="couldn't make out"):
            await transcribe_audio(b"bytes", "meeting.ogg")
    finally:
        p.stop()


async def test_transcribe_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    with pytest.raises(MeetingError, match="isn't configured"):
        await transcribe_audio(b"bytes", "meeting.ogg")
