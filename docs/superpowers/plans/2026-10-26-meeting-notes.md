# Meeting Notes Feature Implementation Plan

> **For agentic workers:** Implement task-by-task (subagent-driven-development or executing-plans). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Staff record an in-person meeting on a phone and upload the audio to a designated Discord channel; the decision bot transcribes it (OpenAI Whisper), extracts key notes + decisions (GPT-4o-mini), posts a meeting-notes card in-channel, and feeds each detected decision into the existing Confirm/Dismiss → Trello flow.

**Locked decisions (from planning):**
- **Capture:** upload an audio file to a designated "meetings" channel (no Discord voice-channel recording).
- **Output:** key-points summary posted in-channel + decisions fed into the existing confirm→Trello flow.
- **Chunking:** include the audio library now (ffmpeg + pydub) so recordings longer than the 25MB limit are split and transcribed in parts — no retrofit later.

**Architecture:** a branch in the decision bot's `on_message` listener: audio attachment in the meetings channel → download → (split if >25MB) → transcribe each chunk (Whisper) → combine → extract notes+decisions (GPT) → post notes card + per-decision Confirm cards. Shares the decision bot's process and DB; no new Discord app or credentials.

**Tech stack:** existing (discord.py, SQLAlchemy/SQLite, httpx+tenacity, pytest) + `pydub` (audio splitting) + `ffmpeg` (system package, via Dockerfile) + OpenAI Whisper (`/audio/transcriptions`) + GPT-4o-mini (existing).

**Cost:** Whisper ~$0.006/min + cents of GPT. A 30-min meeting ≈ $0.20. ffmpeg/pydub are free/open-source.

## Global Constraints

- Reuse house patterns: `_md_escape`/`_safe_url`; `allowed_mentions.none()`; per-guild config; cached session factory; `asyncio.to_thread` for blocking calls; DynamicItem for persistent buttons; best-effort external calls with tenacity (5xx/429/transport), never raise into the bot loop.
- All datetimes timezone-aware UTC; render in guild tz (default Australia/Perth).
- **Hard limits:** Discord free-tier upload cap and Whisper both cap at 25MB. Over-25MB files are split with pydub (ffmpeg) into ≤25MB chunks; if a file can't be split (no ffmpeg, unreadable, or a format we don't support), reply with a clear "too long/big — split or trim it" message.
- Whisper + GPT calls never crash the bot; failure posts a "couldn't transcribe, try again" card.
- Tests run with `.venv/bin/python -m pytest` from repo root; suite green, zero warnings.

## New/changed files

| File | Responsibility |
|---|---|
| `src/services/meeting_service.py` + | download attachment, split-if-needed, transcribe (Whisper), extract notes+decisions (GPT) |
| `src/decision_bot.py` | on_message branch: audio attachment in the meetings channel → process_meeting |
| `src/services/decision_service.py` | reuse detect/confirm; + a notes-extraction call (or move to meeting_service) |
| `src/discord_ui/decision_commands.py` | + `/decision meetings_channel <#channel>` + `/decision meetings` (list past notes) |
| `src/db/models.py` | + `Meeting` table (id, guild_id, channel_id, filename, transcript_excerpt, summary, created_at) |
| `src/db/session.py` | + idempotent DDL for `meetings` |
| `src/db/repositories.py` | + meeting repos (add/list) |
| `Dockerfile` | install `ffmpeg` (system) so pydub can split audio |
| `pyproject.toml` | + `pydub` dependency |
| `.env.example` | no new vars (OPENAI_API_KEY reused) |

## DB table

- `meetings`: `id` (autoincrement), `guild_id`, `channel_id`, `filename`, `summary` (key points), `transcript_excerpt` (capped), `created_at`

## Flow detail

1. `on_message` (decision bot): a message with an audio attachment (content-type audio/* or a known audio/video extension) posted in the configured meetings channel, guild enabled → process.
2. Download the attachment (size-capped; reject clearly if absurdly large even for chunking, e.g. >150MB).
3. If >25MB (or Whisper rejects it), split into ≤25MB chunks with pydub/ffmpeg; else transcribe whole.
4. Transcribe each chunk via Whisper (`model="whisper-1"`), concatenate transcripts in order.
5. GPT-4o-mini extracts: a short key-points summary (posted as the notes card) + a list of decisions (each gets its own Confirm/Dismiss card via the existing flow). Subject+outcome rule already in the prompt carries over.
6. Store a `Meeting` row (summary + excerpt) for `/decision meetings`.

## Tasks

- [x] **Task 1: DB + repos + DDL.** `Meeting` model, `ensure_meeting_tables` DDL, add/list repos, config reuse. Tests: repos + DDL idempotency.
- [x] **Task 2: meeting_service — download + chunk + transcribe.** Attachment download (size guard), pydub chunking (ffmpeg), Whisper transcription per chunk, ordered combine. Tests: mock httpx + pydub; chunking boundary; oversized/format rejection; Whisper failure isolation.
- [x] **Task 3: meeting_service — notes + decisions extraction.** GPT call producing `{summary, decisions[]}` (JSON mode, retry, subject+outcome rule). Tests: parse robustness, failure isolation, decision-list shape.
- [x] **Task 4: listener branch.** Audio-in-meetings-channel detection (content-type/extension), orchestrate the pipeline, post notes card + per-decision Confirm cards, store the Meeting row. Tests: filter branches (wrong channel, no audio, disabled, not audio), happy path, oversized rejection message, partial-failure cards.
- [x] **Task 5: admin commands + Dockerfile + deps.** `/decision meetings_channel`, `/decision meetings`; Dockerfile installs ffmpeg; pyproject adds pydub. Tests: commands happy/invalid; command-surface registration.
- [x] **Task 6: council + full pass.** 4-agent council (correctness/security/regressions/coverage), fix waves, README, full suite green.
- [x] **Task 7: deploy.** `fly deploy --ha=false` (image 212MB, ffmpeg 7.1.5 verified on machine; both shards resumed after deploy). Remaining go-live gate: user uploads a real short recording in the meetings channel and confirms a notes card + decision cards post.

## Out of scope

- Discord voice-channel recording (in-person upload only).
- Speaker diarisation (who said what) — Whisper gives a single transcript; the GPT extraction references "the team".
- Editing/merging meeting notes after posting (edit in Trello if needed).

## Go-live gates (verify before done)

- ffmpeg present in the container (`ffmpeg -version` via `fly ssh console`).
- A real audio upload in the meetings channel produces a notes card + decision card(s).
- A >25MB recording splits and transcribes without a 25MB error.
