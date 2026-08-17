"""Telegram bot front-end for the MentahanPOV pipeline.

Send a video from your phone to your bot, get it processed and posted
automatically — no terminal needed after setup.

Setup:
    1. Message @BotFather on Telegram -> /newbot -> follow the prompts ->
       copy the token it gives you.
    2. Add to .env:  TELEGRAM_BOT_TOKEN=<paste token>
       Optionally restrict to just you:
       TELEGRAM_ALLOWED_USER_IDS=<your numeric Telegram user id>
       (message @userinfobot to find your id)
    3. Run:  python telegram_bot.py
    4. Open your bot in Telegram, send it a video file.

Each incoming video runs `main.py` as a subprocess (same as running it
from the terminal yourself) so the bot process itself stays simple and
can't be taken down by a pipeline crash. Results (platform links or
errors) are sent back as a chat message.
"""

from __future__ import annotations

import ast
import asyncio
import html
import logging
import re
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import config
from core import state as pipeline_state
from core import video_facts

log = logging.getLogger("mentahanpov.bot")

REPO_ROOT = Path(__file__).parent
INCOMING_DIR = REPO_ROOT / "incoming"
PIPELINE_TIMEOUT_S = 15 * 60  # generous — Gemini + 3 uploads can take a while
STATUS_TICK_S = 3  # how often the status message is allowed to be edited

_FINAL_STATE_RE = re.compile(r"Final state: (\{.*\})\s*$")

# Substring -> friendly status text. Checked in order against every log
# line as it streams in; the last match found "wins" as the current stage.
_STAGE_PATTERNS: list[tuple[str, str]] = [
    ("[facts]", "📼 Baca metadata video (GPS, durasi, resolusi)..."),
    ("Location:", "📍 Alamat ketemu dari GPS, lanjut ke Gemini..."),
    ("[gemini] uploading", "🤖 Upload video ke Gemini..."),
    ("[gemini] generating", "🤖 Gemini mikirin caption + nama file..."),
    ("Folder:", "🤖 Caption jadi, lanjut upload ke Drive..."),
    ("[gdrive] uploading", "☁️ Upload video HD ke Google Drive..."),
    ("[gdrive] uploaded id=", "☁️ Upload Drive selesai..."),
    ("[watermark] rendering", "🎨 Render watermark..."),
    ("Posting copy ready", "🎨 Watermark selesai, mulai posting..."),
    ("[youtube] posting", "📤 Posting ke YouTube..."),
    ("[youtube] OK", "✅ YouTube beres, lanjut ke platform berikutnya..."),
    ("[facebook] posting", "📤 Posting ke Facebook..."),
    ("[facebook] OK", "✅ Facebook beres, lanjut ke platform berikutnya..."),
    ("[instagram] posting", "📤 Posting ke Instagram..."),
    ("[instagram] OK", "✅ Instagram beres..."),
    ("[tiktok] posting", "📤 Posting ke TikTok..."),
    ("[facebook_story] posting", "📱 Posting Story Facebook..."),
    ("[facebook_story] OK", "✅ Story Facebook beres..."),
    ("[instagram_story] posting", "📱 Posting Story Instagram..."),
    ("[instagram_story] OK", "✅ Story Instagram beres..."),
]


def _allowed(user_id: int) -> bool:
    if not config.telegram_allowed_user_ids:
        return True  # no allowlist configured -> anyone who has the bot link
    return user_id in config.telegram_allowed_user_ids


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    # main.py's logging.basicConfig() defaults to stderr, not stdout — the
    # "Final state: {...}" summary line lives there, so both must be
    # searched (or it's silently "not found" even on a full success).
    match = None
    for line in (stdout + "\n" + stderr).splitlines():
        m = _FINAL_STATE_RE.search(line)
        if m:
            match = m
    if match:
        try:
            platforms = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            platforms = None
        if platforms:
            lines = []
            for name, info in platforms.items():
                if info.get("status") == "ok":
                    lines.append(f"✅ {name}: {info.get('url')}")
                else:
                    lines.append(f"❌ {name}: {info.get('error')}")
            header = "🎉 Selesai!" if returncode == 0 else "⚠️ Selesai dengan error:"
            return header + "\n" + "\n".join(lines)

    # Fallback: couldn't find/parse the summary line — dump the tail.
    tail = (stdout[-1500:] + "\n" + stderr[-1000:]).strip()
    status = "selesai" if returncode == 0 else f"gagal (exit {returncode})"
    return f"Proses {status}, tapi gak nemu ringkasan hasil. Log terakhir:\n```\n{tail}\n```"


async def _run_pipeline(video_path: Path, status_msg) -> str:
    args = [sys.executable, "-u", "main.py", str(video_path)]
    if config.telegram_platforms:
        args += ["--platforms", ",".join(config.telegram_platforms)]
    log.info("[bot] running: %s", " ".join(args))

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=REPO_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    lines: list[str] = []
    state = {"stage": None, "shown": None}

    async def _read_stream() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            for needle, message in _STAGE_PATTERNS:
                if needle in line:
                    state["stage"] = message

    async def _ticker() -> None:
        # Edits the status message periodically instead of on every log
        # line — avoids Telegram's edit-rate limits and message spam.
        while True:
            await asyncio.sleep(STATUS_TICK_S)
            if state["stage"] and state["stage"] != state["shown"]:
                state["shown"] = state["stage"]
                try:
                    await status_msg.edit_text(state["stage"])
                except Exception:  # noqa: BLE001 — e.g. "message not modified"
                    pass

    ticker_task = asyncio.create_task(_ticker())
    try:
        await asyncio.wait_for(_read_stream(), timeout=PIPELINE_TIMEOUT_S)
        returncode = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        return "⏱️ Kelamaan (>15 menit), aku hentiin. Cek manual lewat terminal."
    finally:
        ticker_task.cancel()

    return _format_result(returncode, "\n".join(lines), "")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not _allowed(update.effective_user.id):
        await update.message.reply_text(
            "Bot ini private, akunmu belum diizinkan. "
            f"(user id: {update.effective_user.id})"
        )
        return

    sent_as_document = bool(
        update.message.document
        and (update.message.document.mime_type or "").startswith("video/")
    )
    tg_video = update.message.video or (
        update.message.document if sent_as_document else None
    )
    if tg_video is None:
        await update.message.reply_text("Kirim file video ya (bukan foto/link).")
        return

    status_msg = await update.message.reply_text("📥 Nerima video, download dulu...")

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tg_file = await context.bot.get_file(tg_video.file_id)
    filename = getattr(tg_video, "file_name", None) or f"{tg_video.file_unique_id}.mp4"
    local_path = INCOMING_DIR / filename
    await tg_file.download_to_drive(str(local_path))

    await _warn_if_metadata_stripped(local_path, sent_as_document, update.message)

    await status_msg.edit_text(
        f"⚙️ Diproses: {filename}\nBiasanya 3-5 menit (Gemini + upload + posting). "
        "Status bakal keupdate tiap beberapa detik..."
    )

    try:
        result = await _run_pipeline(local_path, status_msg)
    except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
        log.exception("[bot] pipeline crashed")
        result = f"💥 Error gak terduga: {type(exc).__name__}: {exc}"

    # The result must reach the user even if editing the status message
    # fails (e.g. a transient network blip on a long-polling connection) —
    # otherwise the pipeline can finish successfully and the user never
    # finds out. Fall back to a brand-new message if the edit fails.
    try:
        await status_msg.edit_text(result, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.exception("[bot] failed to edit status message, sending fresh one")
        try:
            await update.message.reply_text(result, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.exception("[bot] failed to send result even as a new message")

    await _send_tiktok_kit(update.message, local_path)


async def _warn_if_metadata_stripped(
    local_path: Path, sent_as_document: bool, message
) -> None:
    """Tell the user when a send method cost them GPS + quality.

    Telegram re-encodes anything sent from the Gallery ("Video"), which
    drops the ISO-6709 `location` tag the caption's 📍 line depends on and
    downscales the footage — fatal for an archive whose whole promise is
    untouched HD. Sending the same file as a Document keeps it byte-exact.
    """
    try:
        facts = video_facts.extract_facts(local_path)
    except Exception:  # noqa: BLE001 — never block a run over a warning
        log.exception("[bot] could not probe video for metadata warning")
        return
    if facts.gps:
        return

    hint = (
        "⚠️ Video ini gak ada data GPS-nya, jadi baris '📍 Location' "
        "bakal dilewati di caption.\n\n"
    )
    if not sent_as_document:
        hint += (
            "Penyebabnya: video dikirim lewat *Galeri*, dan Telegram "
            "meng-compress ulang (GPS hilang + kualitas turun).\n\n"
            "Biar GPS & kualitas HD-nya utuh: kirim ulang pakai "
            "📎 → *File*, bukan Galeri.\n\n"
            "Proses tetap saya lanjutkan untuk yang ini."
        )
    else:
        hint += (
            "File dikirim sebagai File (bagus — gak di-compress), tapi "
            "memang gak ada GPS-nya sejak awal. Pastikan lokasi aktif "
            "di kamera saat merekam."
        )
    try:
        await message.reply_text(hint, parse_mode="Markdown")
    except Exception:  # noqa: BLE001
        log.exception("[bot] failed to send metadata warning")


async def _send_tiktok_kit(message, source_video: Path) -> None:
    """Send back everything needed to post this clip to TikTok by hand.

    TikTok is the one platform this pipeline can't publish to from the
    always-on box: it has no official API here, so it needs a real browser
    driving TikTok Studio — which won't run on an ARM board with ~800MB of
    free RAM. Rather than automate it badly, the bot hands over the exact
    watermarked file and caption so posting is a save-and-upload on the
    phone that's already in your hand.
    """
    entry = pipeline_state.get(config.state_file, source_video)
    caption = entry.get("caption")
    copy_path = entry.get("posting_copy_path")
    if not caption or not copy_path:
        return
    # State stores this relative to the repo root; resolve it explicitly so
    # the lookup doesn't depend on the bot's working directory.
    copy_file = Path(copy_path)
    if not copy_file.is_absolute():
        copy_file = REPO_ROOT / copy_file
    if not copy_file.exists():
        log.warning("[bot] posting copy missing, skipping TikTok kit: %s", copy_file)
        return

    try:
        with copy_file.open("rb") as fh:
            await message.reply_document(
                document=fh,
                filename=copy_file.name,
                caption="📱 Buat TikTok — simpan video ini, captionnya di bawah 👇",
            )
        # <pre> gives Telegram's one-tap copy button, and escaping means the
        # caption's own punctuation can't break the markup.
        await message.reply_text(
            f"<pre>{html.escape(caption)}</pre>", parse_mode="HTML"
        )
    except Exception:  # noqa: BLE001 — the pipeline already succeeded
        log.exception("[bot] failed to send TikTok kit")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Kirim video mentahan-nya ke sini, nanti aku proses & posting otomatis."
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("[bot] unhandled exception", exc_info=context.error)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not config.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is empty. See telegram_bot.py's docstring → 'Setup'."
        )

    builder = Application.builder().token(config.telegram_bot_token)
    if config.telegram_api_id and config.telegram_api_hash:
        # Route through a Local Bot API Server (see README → "Telegram
        # bot") so file downloads aren't capped at api.telegram.org's
        # 20MB limit — raw phone footage routinely exceeds that.
        base = config.telegram_local_api_url.rstrip("/")
        log.info("[bot] using Local Bot API Server at %s", base)
        builder = (
            builder.base_url(f"{base}/bot")
            .base_file_url(f"{base}/file/bot")
            .local_mode(True)
        )
    else:
        log.warning(
            "[bot] TELEGRAM_API_ID/HASH not set — using api.telegram.org "
            "directly, which caps downloads at 20MB. See README → "
            "'Telegram bot' to remove that limit."
        )
    app = builder.build()
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )
    app.add_handler(MessageHandler(filters.ALL, handle_other))
    app.add_error_handler(handle_error)

    log.info("[bot] MentahanPOV bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
