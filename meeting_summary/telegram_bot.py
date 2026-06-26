from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from main import load_local_env, sanitize_filename


SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = SCRIPT_DIR / "telegram_downloads"
LOG_PATH = SCRIPT_DIR / "telegram_bot.log"
VALID_MODES = {"meeting", "transcript", "compact"}
SUBPROCESS_TIMEOUT_SECONDS = 1800  # Backstop so the bot never hangs forever.


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def load_token() -> str:
    load_local_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Add it to meeting_summary/.env."
        )
    return token


def allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Ignoring invalid Telegram allow-list id: %s", part)
    return ids


def is_allowed(update: Update) -> bool:
    ids = allowed_ids()
    if not ids:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    return chat_id in ids or user_id in ids


def attached_file(update: Update):
    message = update.effective_message
    if not message:
        return None
    return message.document or message.video or message.audio or message.voice


def attached_filename(update: Update) -> str:
    file_obj = attached_file(update)
    name = getattr(file_obj, "file_name", None)
    if name:
        return name
    extension = ".oga" if getattr(file_obj, "mime_type", "") == "audio/ogg" else ".dat"
    unique_id = getattr(file_obj, "file_unique_id", "telegram_file")
    return f"{unique_id}{extension}"


def mode_from_command(update: Update) -> str | None:
    text = update.effective_message.text if update.effective_message else None
    if not text:
        return None
    command = text.split()[0].split("@", 1)[0].lstrip("/").lower()
    return command if command in VALID_MODES else None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_if_denied(update):
        return
    await update.message.reply_text(
        "Send an audio/video file for meeting mode, or use:\n"
        "/meeting\n/transcript\n/compact\n\n"
        "After choosing a command, send the audio/video file."
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_if_denied(update):
        return

    mode = mode_from_command(update)
    if mode is None:
        await update.message.reply_text("Unknown mode.")
        return

    if attached_file(update):
        await process_update_file(update, context, mode)
        return

    context.user_data["pending_mode"] = mode
    await update.message.reply_text(f"Mode set to {mode}. Send an audio/video file.")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_if_denied(update):
        return

    mode = context.user_data.pop("pending_mode", "meeting")
    if mode not in VALID_MODES:
        mode = "meeting"
    await process_update_file(update, context, mode)


async def reject_if_denied(update: Update) -> bool:
    if is_allowed(update):
        return True
    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    user_id = update.effective_user.id if update.effective_user else "unknown"
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Access denied. chat_id={chat_id}, user_id={user_id}"
        )
    return False


async def process_update_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
) -> None:
    message = update.effective_message
    file_obj = attached_file(update)
    if not message or not file_obj:
        return

    status = await message.reply_text(f"Downloading file for {mode} mode...")
    try:
        input_path = await download_attachment(context, update)
        await status.edit_text(
            f"Downloaded: {input_path.name}\nProcessing in {mode} mode..."
        )
        output_path = await asyncio.to_thread(run_meeting_summary, mode, input_path)
        await status.edit_text(f"Done: {output_path.name}")
        with output_path.open("rb") as document:
            await message.reply_document(
                document=document,
                filename=output_path.name,
                caption=f"Meeting summary completed ({mode}).",
            )
    except Exception as exc:
        logger.exception("Meeting summary job failed")
        error_text = str(exc)
        if len(error_text) > 3500:
            error_text = error_text[:3500] + "\n..."
        await status.edit_text(f"Error while processing {mode} mode:\n{error_text}")


async def download_attachment(context: ContextTypes.DEFAULT_TYPE, update: Update) -> Path:
    file_obj = attached_file(update)
    if file_obj is None:
        raise RuntimeError("No audio/video file found in the message.")

    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = sanitize_filename(Path(attached_filename(update)).stem) + Path(
        attached_filename(update)
    ).suffix
    target_dir = DOWNLOAD_DIR / str(chat_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{timestamp}_{filename}"

    telegram_file = await context.bot.get_file(file_obj.file_id)
    await telegram_file.download_to_drive(custom_path=target_path)
    logger.info("Downloaded Telegram file: %s", target_path)
    return target_path


def run_meeting_summary(mode: str, input_path: Path) -> Path:
    command = [
        sys.executable,
        str(SCRIPT_DIR / "main.py"),
        mode,
        str(input_path),
    ]
    logger.info("Running: %s", " ".join(command))
    # Force UTF-8 stdio so non-ASCII output paths (e.g. Japanese filenames)
    # survive the round-trip and parse_output_path can match the real file.
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            command,
            cwd=SCRIPT_DIR.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Processing timed out after {SUBPROCESS_TIMEOUT_SECONDS // 60} minutes; "
            "aborted."
        ) from exc
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    # Record the full pipeline detail for every job (success or failure), so the
    # bot log is no longer just "Running..." / "output".
    logger.info(
        "main.py finished for %s (exit %s).\n--- stderr ---\n%s\n--- stdout ---\n%s",
        input_path.name,
        completed.returncode,
        stderr or "(empty)",
        stdout or "(empty)",
    )
    if completed.returncode != 0:
        detail = "\n".join(part for part in (stderr, stdout) if part)
        raise RuntimeError(detail or f"meeting_summary exited with {completed.returncode}")

    output_path = parse_output_path(stdout)
    if output_path is None or not output_path.exists():
        detail = "\n".join(part for part in (stdout, stderr) if part)
        raise RuntimeError(f"Could not find output Markdown path.\n{detail}")
    logger.info("Meeting summary output: %s", output_path)
    return output_path


def parse_output_path(stdout: str) -> Path | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("Wrote "):
            return Path(line[len("Wrote ") :].strip())
    return None


def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("meeting", cmd_mode))
    app.add_handler(CommandHandler("transcript", cmd_mode))
    app.add_handler(CommandHandler("compact", cmd_mode))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE,
            handle_file,
        )
    )
    return app


async def run_bot() -> None:
    token = load_token()
    app = build_app(token)
    logger.info("Meeting Summary Telegram Bot starting...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Meeting Summary Telegram Bot is running.")
        try:
            await asyncio.Event().wait()
        finally:
            logger.info("Meeting Summary Telegram Bot shutting down...")
            await app.updater.stop()
            await app.stop()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception("Fatal Telegram bot error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
