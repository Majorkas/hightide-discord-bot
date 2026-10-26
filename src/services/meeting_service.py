"""Meeting-notes pipeline: download audio, chunk if needed, transcribe, extract notes."""
import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src import config

log = logging.getLogger("hightide.meetings")

WHISPER_API = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
_TIMEOUT = 60.0  # transcription of a long chunk can be slow

# Whisper rejects files over 25MB; Discord free-tier uploads also cap at 25MB.
_MAX_FILE_BYTES = 25 * 1024 * 1024
# Don't even attempt to download/chunk something enormous.
_MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".flac", ".mp4", ".mpeg", ".mpga", ".webm"}


class MeetingError(Exception):
    """A user-facing processing failure (too big, unreadable, can't transcribe)."""


@dataclass
class TranscriptResult:
    text: str
    chunks: int


def is_audio_attachment(filename: str, content_type: str | None) -> bool:
    """True when an attachment looks like audio we can transcribe."""
    if content_type and content_type.startswith(("audio/", "video/mp4", "application/ogg")):
        return True
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in _AUDIO_EXTS


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


async def download_attachment(url: str) -> bytes:
    """Download an attachment; MeetingError when too large or unreachable."""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=60.0)
            if resp.status_code != 200:
                raise MeetingError(f"couldn't download the file (HTTP {resp.status_code})")
            data = resp.content
    except httpx.HTTPError as e:
        raise MeetingError(f"couldn't download the file ({type(e).__name__})") from e
    if len(data) > _MAX_DOWNLOAD_BYTES:
        raise MeetingError("that file is far too large to process — trim it to a shorter clip.")
    return data


def _chunk_audio(path: str) -> list[str]:
    """Split the audio file at `path` into ≤25MB chunks; returns chunk file paths.

    Uses pydub/ffmpeg. Raises MeetingError when the file can't be read/split.
    """
    from pydub import AudioSegment
    try:
        audio = AudioSegment.from_file(path)
    except Exception as e:
        raise MeetingError("couldn't read that audio file — is it a real recording?") from e

    size = os.path.getsize(path)
    if size <= _MAX_FILE_BYTES:
        return [path]

    # Chunk by proportional duration so each chunk stays under the byte cap.
    n_chunks = (size // _MAX_FILE_BYTES) + 1
    chunk_ms = max(len(audio) // n_chunks, 1)
    out = []
    start = 0
    i = 0
    while start < len(audio):
        seg = audio[start:start + chunk_ms]
        chunk_path = f"{path}.part{i}.ogg"
        seg.export(chunk_path, format="ogg", bitrate="64k")
        out.append(chunk_path)
        start += chunk_ms
        i += 1
    if not out:
        raise MeetingError("couldn't split that recording — try a shorter clip.")
    return out


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10),
       retry=retry_if_exception(_is_retryable), reraise=True)
async def _transcribe_chunk(client: httpx.AsyncClient, path: str) -> httpx.Response:
    with open(path, "rb") as f:
        resp = await client.post(
            WHISPER_API,
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            data={"model": WHISPER_MODEL},
            files={"file": (os.path.basename(path), f, "application/octet-stream")},
            timeout=_TIMEOUT)
    if resp.status_code >= 500 or resp.status_code == 429:
        resp.raise_for_status()
    return resp


async def transcribe_audio(data: bytes, filename: str) -> TranscriptResult:
    """Transcribe audio bytes, chunking when over the Whisper size cap. MeetingError on failure."""
    if not config.OPENAI_API_KEY:
        raise MeetingError("transcription isn't configured.")
    tmpdir = tempfile.mkdtemp(prefix="hightide_meeting_")
    base = os.path.join(tmpdir, filename or "meeting.ogg")
    try:
        with open(base, "wb") as f:
            f.write(data)
        chunks = await asyncio.to_thread(_chunk_audio, base)

        texts: list[str] = []
        async with httpx.AsyncClient() as client:
            for chunk_path in chunks:
                resp = await _transcribe_chunk(client, chunk_path)
                if resp.status_code != 200:
                    raise MeetingError(f"transcription failed (HTTP {resp.status_code})")
                try:
                    texts.append(resp.json().get("text", "").strip())
                except ValueError as e:
                    raise MeetingError("transcription returned an unreadable response.") from e
        text = " ".join(t for t in texts if t).strip()
        if not text:
            raise MeetingError("couldn't make out any speech in that recording.")
        return TranscriptResult(text=text, chunks=len(chunks))
    finally:
        # best-effort cleanup of temp chunks
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
