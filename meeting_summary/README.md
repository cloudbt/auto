# Meeting Summary CLI

Python CLI that turns meeting audio/video into Markdown transcripts, summaries,
and action-item (TODO) lists. Transcription uses a **pluggable, quota-aware STT
provider chain** (Gladia → Speechmatics → Gemini) that auto-switches to the next
provider when one's free tier is exhausted. Summaries are written by Gemini.

## Architecture

```
Telegram / Windows script
        │  audio/video
        ▼
   media.py     ffmpeg → mono 16 kHz MP3 (+ time-aligned chunks)
        ▼
  providers/    transcribe_with_fallback(): gladia → speechmatics → gemini
        │       skip if no key / locally exhausted; switch on quota errors
        ▼
  TranscriptResult  (unified [HH:MM:SS] segments)
        ▼
  summarize.py  Gemini → Summary (会议纪要) + Action Items (TODO)
        ▼
  export.py     Markdown → output/ → Obsidian copy → GitHub push
```

| Module | Role |
|---|---|
| `main.py` | CLI + orchestration. |
| `config.py` | `.env` loading, API keys, provider order, language. |
| `media.py` | ffmpeg normalize/split + media cache. |
| `gemini_client.py` | Gemini client pool, retry, generation primitives. |
| `summarize.py` | Gemini meeting notes (Summary + TODO). |
| `export.py` | Markdown build, Obsidian copy, GitHub publish. |
| `usage.py` | Local monthly free-quota + speed tracking. |
| `providers/` | STT provider package (`gladia`, `speechmatics`, `gemini`). |

## Setup

```powershell
cd C:\work\work-git\git\auto
python -m pip install -r meeting_summary\requirements.txt
Copy-Item meeting_summary\.env.example meeting_summary\.env
```

Edit `meeting_summary/.env` and set the keys you have (any blank key is skipped):

```text
STT_PROVIDER_ORDER=gladia,speechmatics,gemini
MEETING_LANGUAGE=ja
GLADIA_API_KEY=your_gladia_key
SPEECHMATICS_API_KEY=your_speechmatics_key
GEMINI_API_KEY1=your_gemini_key
GEMINI_MODEL=gemini-2.5-flash
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

`meeting_summary/.env` is ignored by git through the repository `.gitignore`.

A Gemini key is required for `meeting`/`compact` modes (it writes the summary).
`transcript` mode works with any single STT provider key.

## Usage

```powershell
python meeting_summary\main.py transcript "C:\path\to\meeting.m4a"
python meeting_summary\main.py meeting "C:\path\to\meeting.mp3"
python meeting_summary\main.py compact "C:\path\to\meeting.mp4"
```

Output defaults to `meeting_summary/output/<input_stem>_<mode>_<YYYYMMDD-HHMMSS>.md`,
is also copied to the Obsidian/iCloud folder, and pushed to the `meeting/`
directory of `https://github.com/cloudbt/dev.git` (`main` branch).

Useful flags: `--output`, `--no-copy`, `--no-publish`, `--no-cache`,
`--stt-order gladia,gemini`, `--language ja`, `--model gemini-2.5-flash`.

## Provider fallback & free-quota tracking

- Providers are tried in `STT_PROVIDER_ORDER` (override per run with `--stt-order`).
- A provider with no API key is skipped. When a provider returns a quota/credit
  error it is marked exhausted **for the current month** and the next provider is
  used.
- `usage.py` also tracks minutes used per provider each month (in
  `%LOCALAPPDATA%/meeting_summary/usage.json`) and pre-skips a provider once its
  known free allowance would be exceeded. Override the limits with
  `GLADIA_FREE_MINUTES` / `SPEECHMATICS_FREE_MINUTES`.

### Benchmark (auto speed test)

Time every configured provider on a short sample of the input:

```powershell
python meeting_summary\main.py benchmark "C:\path\to\meeting.m4a" --sample-seconds 60
```

Prints each provider's status, elapsed time, segment count, and seconds-per-minute.

## Modes

- `transcript`: timestamped transcript only (no summary; no Gemini key needed).
- `meeting`: Summary + Action Items (TODO) + timestamped transcript.
- `compact`: Summary + Action Items (TODO) only.

## Media handling

- Audio/video is normalized with `ffmpeg` to mono MP3 at 16 kHz / 64 kbps.
- Video inputs are treated as audio; frames are not analyzed.
- Inputs longer than 2 hours are rejected by default (`--max-hours`).
- Audio is split into 25-minute chunks by default (`--chunk-minutes`); whole-file
  providers (Gladia/Speechmatics) use the full MP3, Gemini consumes chunks.
- Normalized MP3, chunks, and the final transcript (`transcript.json`) are cached
  beside the input under `.meeting_summary_cache`. Rerunning the same command
  reuses the cached transcript and skips STT entirely. `--no-cache` disables this.

## Output policy

- Transcript text preserves the original spoken language.
- Summary and action items follow the dominant meeting language.
- Japanese/Chinese mixed meetings keep mixed wording naturally.
- Unknown action-item owner or due date is written as `TBD`.

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on `PATH`
- At least one STT provider key; a Gemini key for summaries

## Telegram bot

```powershell
python meeting_summary\telegram_bot.py
```

- `/meeting`, `/transcript`, `/compact`: choose the mode, then send a file.
- A direct file upload (no command) uses `meeting` mode.
- On success the bot returns the generated Markdown file.
- Set `TELEGRAM_ALLOWED_CHAT_IDS` to a comma-separated allow-list (empty = allow all).

## Tests

```powershell
python -m pytest meeting_summary\tests
```

The tests cover the provider fallback logic and the usage tracker with fakes —
no API keys or network required.
