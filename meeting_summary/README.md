# Gemini Meeting Summary CLI

Python CLI for converting meeting audio/video into Markdown transcripts,
summaries, and action items with the Gemini API.

## Setup

```powershell
cd C:\work\work-git\git\auto
python -m pip install -r meeting_summary\requirements.txt
Copy-Item meeting_summary\.env.example meeting_summary\.env
```

Edit `meeting_summary/.env` and set:

```text
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-3.5-flash
```

`meeting_summary/.env` is ignored by git through the repository `.gitignore`.

## Usage

```powershell
python meeting_summary\main.py transcript "C:\path\to\meeting.m4a"
python meeting_summary\main.py meeting "C:\path\to\meeting.mp3"
python meeting_summary\main.py compact "C:\path\to\meeting.mp4"
```

Output defaults to:

```text
meeting_summary/output/<input_stem>_<mode>_<YYYYMMDD-HHMMSS>.md
```

Use `--output` to choose a specific Markdown path:

```powershell
python meeting_summary\main.py meeting "C:\path\to\meeting.m4a" --output ".\notes.md"
```

Logs are written to stderr with local timestamps:

```text
[2026-05-30 00:15:12] Transcribing chunk 1/2 (00:00:00-00:20:00)...
```

## Modes

- `transcript`: timestamped transcript only.
- `meeting`: summary, action items, and timestamped transcript.
- `compact`: summary and action items only.

## Media Handling

- Audio and video are normalized with `ffmpeg` to mono MP3 at 16 kHz and 64 kbps.
- MP4 and other video inputs are treated as audio sources; video frames are not analyzed.
- Inputs longer than 2 hours are rejected by default. Override with `--max-hours`.
- Audio is split into 20-minute chunks by default. Override with `--chunk-minutes`.
- Converted MP3 and chunks are cached beside the input file under `.meeting_summary_cache`.
  If a Gemini call fails, rerun the same command and the CLI will reuse the cached
  MP3/chunks instead of converting the source video/audio again.
- Chunk-level Gemini responses are also cached in the same directory. If `meeting`
  mode finishes transcript extraction but fails while creating the final summary,
  rerunning the command reuses the cached chunk transcripts.
- Use `--no-cache` to force temporary conversion and delete intermediate media after
  the run.

## Output Policy

- Transcript text preserves the original spoken language.
- Summary and action items follow the dominant meeting language.
- Japanese/Chinese mixed meetings keep mixed wording naturally.
- Unknown action item owner or due date is written as `TBD`.

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on `PATH`
- Gemini API key
