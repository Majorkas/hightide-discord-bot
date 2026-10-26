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
# Cap processed audio duration: a tiny low-bitrate file can hold hours of speech
# (decompression bomb → OOM on decode + a large Whisper bill). 90 minutes covers
# realistic staff meetings.
_MAX_DURATION_MS = 90 * 60 * 1000
# Chunks are re-encoded to 64 kbps OGG, so chunk byte-size is duration-driven and
# independent of the source bitrate. Cap chunk duration so each export stays under
# the Whisper cap (~10% margin for VBR/container overhead): 25MB*8*1000/64000 ≈ 3125s.
_MAX_CHUNK_MS = int(_MAX_FILE_BYTES * 8 * 1000 / 64000 * 0.9)
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
            async with client.stream("GET", url, timeout=60.0) as resp:
                if resp.status_code != 200:
                    raise MeetingError(f"couldn't download the file (HTTP {resp.status_code})")
                # Guard on the declared length before buffering anything.
                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > _MAX_DOWNLOAD_BYTES:
                    raise MeetingError("that file is far too large to process — trim it to a shorter clip.")
                # Stream and abort past the cap (also stops gzip-bomb amplification).
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MAX_DOWNLOAD_BYTES:
                        raise MeetingError("that file is far too large to process — trim it to a shorter clip.")
                return bytes(buf)
    except MeetingError:
        raise
    except httpx.HTTPError as e:
        raise MeetingError(f"couldn't download the file ({type(e).__name__})") from e


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
    duration_ms = len(audio)
    if duration_ms > _MAX_DURATION_MS:
        raise MeetingError("that recording is too long to process — trim it to a shorter clip.")
    if size <= _MAX_FILE_BYTES:
        return [path]

    # Chunk duration is capped by the 64k export bitrate so every chunk stays under
    # the Whisper cap even when the source is a long low-bitrate recording.
    chunk_ms = min(max(duration_ms // ((size // _MAX_FILE_BYTES) + 1), 1), _MAX_CHUNK_MS)
    out = []
    start = 0
    i = 0
    while start < duration_ms:
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
    # Read the chunk off the event loop; httpx's multipart encoder would otherwise
    # call .read() on a sync file object inline (up to 25MB of blocking I/O).
    data = await asyncio.to_thread(_read_file, path)
    resp = await client.post(
        WHISPER_API,
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        data={"model": WHISPER_MODEL},
        files={"file": (os.path.basename(path), data, "application/octet-stream")},
        timeout=_TIMEOUT)
    if resp.status_code >= 500 or resp.status_code == 429:
        resp.raise_for_status()
    return resp


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def transcribe_audio(data: bytes, filename: str) -> TranscriptResult:
    """Transcribe audio bytes, chunking when over the Whisper size cap. MeetingError on failure."""
    if not config.OPENAI_API_KEY:
        raise MeetingError("transcription isn't configured.")
    tmpdir = tempfile.mkdtemp(prefix="hightide_meeting_")
    # The attachment filename is uploader-controlled; keep only its basename so a
    # crafted name can't escape the temp dir.
    base = os.path.join(tmpdir, os.path.basename(filename or "") or "meeting.ogg")
    try:
        await asyncio.to_thread(_write_file, base, data)
        chunks = await asyncio.to_thread(_chunk_audio, base)

        texts: list[str] = []
        try:
            async with httpx.AsyncClient() as client:
                for chunk_path in chunks:
                    resp = await _transcribe_chunk(client, chunk_path)
                    if resp.status_code != 200:
                        raise MeetingError(f"transcription failed (HTTP {resp.status_code})")
                    try:
                        texts.append(resp.json().get("text", "").strip())
                    except ValueError as e:
                        raise MeetingError("transcription returned an unreadable response.") from e
        except httpx.HTTPError as e:
            # Retry exhaustion / transport failure surfaces here as raw httpx; keep the
            # MeetingError contract so the user gets a transcription-specific message.
            raise MeetingError("transcription failed — the service is unreachable, try again.") from e
        text = " ".join(t for t in texts if t).strip()
        if not text:
            raise MeetingError("couldn't make out any speech in that recording.")
        return TranscriptResult(text=text, chunks=len(chunks))
    finally:
        # best-effort cleanup of temp chunks
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- notes + decisions extraction (GPT) ---

OPENAI_CHAT_API = "https://api.openai.com/v1/chat/completions"
NOTES_MODEL = "gpt-4o-mini"
MAX_TRANSCRIPT_CHARS = 12000  # GPT input cap (meetings can be long)
MAX_NOTES_CHARS = 1500
_MAX_DECISIONS = 10

_NOTES_SYSTEM = (
    "You summarise a hobby-store staff meeting transcript. Reply with strict JSON only: "
    '{"notes": "<=1200 char bulleted key points, one per line starting with - ", '
    '"decisions": ["<one-line decision naming the subject and the outcome>", ...]}. '
    "Decisions are concrete choices the team settled on (name the subject AND the outcome, "
    "resolving pronouns to the thing discussed); omit proposals and chat. If none, use an "
    "empty list."
)


@dataclass
class MeetingNotes:
    notes: str
    decisions: list[str]


def _parse_notes(raw: str) -> MeetingNotes | None:
    import json
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    notes = data.get("notes")
    notes = notes.strip()[:MAX_NOTES_CHARS] if isinstance(notes, str) else ""
    decisions = data.get("decisions")
    if isinstance(decisions, list):
        decisions = [d.strip()[:200] for d in decisions
                     if isinstance(d, str) and d.strip()][:_MAX_DECISIONS]
    else:
        decisions = []
    if not notes and not decisions:
        return None
    return MeetingNotes(notes, decisions)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=6),
       retry=retry_if_exception(_is_retryable), reraise=True)
async def _call_notes(client: httpx.AsyncClient, transcript: str) -> httpx.Response:
    resp = await client.post(
        OPENAI_CHAT_API,
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": NOTES_MODEL,
            "messages": [
                {"role": "system", "content": _NOTES_SYSTEM},
                {"role": "user", "content": transcript[:MAX_TRANSCRIPT_CHARS]},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 800,
        },
        timeout=_TIMEOUT)
    if resp.status_code >= 500 or resp.status_code == 429:
        resp.raise_for_status()
    return resp


async def extract_notes(transcript: str) -> MeetingNotes:
    """Extract key notes + decisions from a transcript; MeetingError on failure."""
    if not config.OPENAI_API_KEY:
        raise MeetingError("note extraction isn't configured.")
    if not (transcript or "").strip():
        raise MeetingError("nothing to summarise — the transcript was empty.")
    try:
        async with httpx.AsyncClient() as client:
            resp = await _call_notes(client, transcript)
        if resp.status_code != 200:
            raise MeetingError(f"note extraction failed (HTTP {resp.status_code})")
        content = resp.json()["choices"][0]["message"]["content"]
    except MeetingError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        raise MeetingError("note extraction failed — try again.") from e
    notes = _parse_notes(content)
    if notes is None:
        raise MeetingError("note extraction returned an unreadable response.")
    return notes
