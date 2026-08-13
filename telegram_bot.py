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
import logging
import re
import subprocess
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import config

log = logging.getLogger("mentahanpov.bot")

REPO_ROOT = Path(__file__).parent
INCOMING_DIR = REPO_ROOT / "incoming"
PIPELINE_TIMEOUT_S = 15 * 60  # generous — Gemini + 3 uploads can take a while

_FINAL_STATE_RE = re.compile(r"Final state: (\{.*\})\s*$")


def _allowed(user_id: int) -> bool:
    if not config.telegram_allowed_user_ids:
        return True  # no allowlist configured -> anyone who has the bot link
    return user_id in config.telegram_allowed_user_ids


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    match = None
    for line in stdout.splitlines():
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


def _run_pipeline(video_path: Path) -> str:
    args = [sys.executable, "main.py", str(video_path)]
    if config.telegram_platforms:
        args += ["--platforms", ",".join(config.telegram_platforms)]
    log.info("[bot] running: %s", " ".join(args))
    proc = subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=PIPELINE_TIMEOUT_S,
    )
    return _format_result(proc.returncode, proc.stdout, proc.stderr)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not _allowed(update.effective_user.id):
        await update.message.reply_text(
            "Bot ini private, akunmu belum diizinkan. "
            f"(user id: {update.effective_user.id})"
        )
        return

    tg_video = update.message.video or (
        update.message.document
        if update.message.document
        and (update.message.document.mime_type or "").startswith("video/")
        else None
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

    await status_msg.edit_text(
        f"⚙️ Diproses: {filename}\nBiasanya 3-5 menit (Gemini + upload + posting). Sabar ya..."
    )

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, _run_pipeline, local_path
        )
    except subprocess.TimeoutExpired:
        result = "⏱️ Kelamaan (>15 menit), aku hentiin. Cek manual lewat terminal."
    except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
        log.exception("[bot] pipeline crashed")
        result = f"💥 Error gak terduga: {type(exc).__name__}: {exc}"

    await status_msg.edit_text(result, disable_web_page_preview=True)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Kirim video mentahan-nya ke sini, nanti aku proses & posting otomatis."
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not config.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is empty. See telegram_bot.py's docstring → 'Setup'."
        )

    app = Application.builder().token(config.telegram_bot_token).build()
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )
    app.add_handler(MessageHandler(filters.ALL, handle_other))

    log.info("[bot] MentahanPOV bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
