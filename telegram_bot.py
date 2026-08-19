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
import time
from pathlib import Path

from telegram import Update
from telegram import error as telegram_error
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import config
from core import state as pipeline_state
from core import video_facts

log = logging.getLogger("mentahanpov.bot")

REPO_ROOT = Path(__file__).parent
INCOMING_DIR = REPO_ROOT / "incoming"
RUN_LOG_DIR = REPO_ROOT / "state" / "run_logs"
# The HG680P is a weak ARM board: watermark rendering, the HD master
# upload, and five sequential platform posts have measured close to 15
# minutes end to end on it. That used to be the timeout itself, so a
# slightly slower run got killed with no explanation. Bumped from 30 to
# 60 minutes for real headroom on this hardware (and room for more
# platforms, e.g. Threads, in the sequential fan-out) — a run that's
# actually stuck is still caught, just later.
PIPELINE_TIMEOUT_S = 60 * 60
STATUS_TICK_S = 5  # seconds between status redraws (Telegram edit-rate safe)

_FINAL_STATE_RE = re.compile(r"Final state: (\{.*\})\s*$")
_GDRIVE_PCT_RE = re.compile(r"\[gdrive\] (\d+)%")

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


def _format_partial_result(video_path: Path, header: str, log_path: Path) -> str:
    """What to report when the subprocess had to be killed mid-run.

    main.py records each platform's result to STATE_FILE as it finishes —
    not just at the end — so even a killed run usually has real,
    cross-checkable links for whatever completed before the cutoff. This
    reads that state back instead of leaving the user with a bare
    "timed out" and no way to tell what, if anything, actually posted.
    """
    entry = pipeline_state.get(config.state_file, video_path)
    platforms = entry.get("platforms", {})
    lines = [header]
    if platforms:
        for name, info in platforms.items():
            if info.get("status") == "ok":
                lines.append(f"✅ {name}: {info.get('url')}")
            else:
                lines.append(f"❌ {name}: {info.get('error')}")
        done = {*platforms}
        pending = [p for p in config.platforms if p not in done]
        if pending:
            lines.append(f"❔ belum sempat dicoba: {', '.join(pending)}")
    else:
        lines.append("Belum ada platform yang sempat selesai diproses.")
    lines.append(f"\nLog lengkap: {log_path}")
    return "\n".join(lines)


class LiveStatus:
    """A single chat message that keeps ticking while work happens, plus
    a periodic checkpoint PING as its own new message.

    Editing only when the stage *changes* leaves the message frozen for
    minutes at a time — during the Telegram download, or while Gemini
    thinks — which is indistinguishable from a crash from the user's side.
    So the elapsed clock and a spinner are redrawn on every tick, giving
    the text something that always moves.

    That's still not enough on its own: Telegram doesn't notify on message
    *edits*, so if you're not already staring at the chat there's no signal
    at all for minutes at a stretch. A checkpoint is sent as a fresh
    message every CHECKPOINT_INTERVAL_S instead — a real notification —
    saying how long it's been and whether that's still comfortably inside
    the timeout or starting to cut it close.
    """

    _FRAMES = "◐◓◑◒"
    CHECKPOINT_INTERVAL_S = 180  # 3 min — real ping, not just an edit

    def __init__(self, message, stage: str, *, timeout_s: int = PIPELINE_TIMEOUT_S) -> None:
        self._message = message
        self._stage = stage
        self._note = ""
        self._start = time.monotonic()
        self._tick = 0
        self._timeout_s = timeout_s
        self._last_checkpoint = 0.0
        self._task: asyncio.Task | None = None

    def set(self, stage: str, note: str = "") -> None:
        self._stage = stage
        self._note = note

    def _render(self) -> str:
        elapsed = int(time.monotonic() - self._start)
        mins, secs = divmod(elapsed, 60)
        clock = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        spinner = self._FRAMES[self._tick % len(self._FRAMES)]
        text = f"{spinner} {clock} · {self._stage}"
        return f"{text}\n{self._note}" if self._note else text

    async def _maybe_checkpoint(self) -> None:
        elapsed = time.monotonic() - self._start
        if elapsed - self._last_checkpoint < self.CHECKPOINT_INTERVAL_S:
            return
        self._last_checkpoint = elapsed
        mins = int(elapsed // 60)
        remaining = self._timeout_s - elapsed
        if remaining <= 5 * 60:
            warn = f"\n⚠️ Sisa ~{max(0, int(remaining // 60))} menit sebelum dihentikan otomatis."
        else:
            warn = "\n✅ Masih on track, belum dekat batas waktu."
        try:
            await self._message.reply_text(
                f"⏳ Checkpoint {mins} menit — {self._stage}{warn}"
            )
        except Exception:  # noqa: BLE001 — a missed ping shouldn't kill the run
            log.exception("[bot] failed to send checkpoint ping")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_TICK_S)
            self._tick += 1
            try:
                await self._message.edit_text(self._render())
            except Exception:  # noqa: BLE001 — a dropped frame is harmless
                pass
            await self._maybe_checkpoint()

    async def __aenter__(self) -> LiveStatus:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task:
            self._task.cancel()

    async def finish(self, text: str) -> None:
        """Stop ticking and leave `text` as the message's final content."""
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            await self._message.edit_text(text, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.exception("[bot] failed to edit final status, sending fresh one")
            await self._message.reply_text(text, disable_web_page_preview=True)


async def _run_pipeline(video_path: Path, status: LiveStatus) -> str:
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

    # main.py's own stdout/stderr only ever lived in an in-memory list
    # before this — invisible to `docker compose logs` and gone forever if
    # the process got killed on timeout. Persisting it as it streams means
    # a run that goes wrong can actually be cross-checked afterward instead
    # of guessed at.
    RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUN_LOG_DIR / f"{pipeline_state.video_key(video_path)}.log"

    lines: list[str] = []

    async def _read_stream() -> None:
        assert proc.stdout is not None
        with log_path.open("w", encoding="utf-8") as log_fh:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                lines.append(line)
                log_fh.write(line + "\n")
                log_fh.flush()

                pct = _GDRIVE_PCT_RE.search(line)
                if pct:
                    status.set(f"☁️ Upload video HD ke Google Drive... {pct.group(1)}%")
                    continue
                for needle, message in _STAGE_PATTERNS:
                    if needle in line:
                        status.set(message)

    try:
        await asyncio.wait_for(_read_stream(), timeout=PIPELINE_TIMEOUT_S)
        returncode = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        minutes = PIPELINE_TIMEOUT_S // 60
        return _format_partial_result(
            video_path,
            f"⏱️ Lewat {minutes} menit, aku hentiin paksa. Yang sempat kepublish:",
            log_path,
        )

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

    size_mb = (getattr(tg_video, "file_size", 0) or 0) / 1048576
    size_note = f"{size_mb:.0f} MB" if size_mb else "ukuran belum diketahui"
    status_msg = await update.message.reply_text("📥 Video diterima...")

    # The clock starts before get_file, not after: with a Local Bot API
    # Server that call blocks for minutes on a big file, and that silent
    # gap is exactly where a run previously looked dead.
    async with LiveStatus(
        status_msg,
        f"Ambil video dari Telegram ({size_note})...",
    ) as status:
        try:
            INCOMING_DIR.mkdir(parents=True, exist_ok=True)
            tg_file = await context.bot.get_file(
                tg_video.file_id, read_timeout=900, connect_timeout=30
            )
            filename = (
                getattr(tg_video, "file_name", None) or f"{tg_video.file_unique_id}.mp4"
            )
            local_path = INCOMING_DIR / filename
            status.set(f"Simpan {filename}...")
            await tg_file.download_to_drive(str(local_path), read_timeout=900)

            await _warn_if_metadata_stripped(
                local_path, sent_as_document, update.message
            )

            status.set(
                "⚙️ Mulai proses...",
                note="Biasanya 3-5 menit: Gemini → Drive → watermark → posting.",
            )
            result = await _run_pipeline(local_path, status)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
            log.exception("[bot] pipeline crashed")
            result = f"💥 Error gak terduga: {type(exc).__name__}: {exc}"
            local_path = None

        await status.finish(result)

    if local_path is not None:
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
    """Log the failure *and* tell the chat about it.

    Logging alone meant a crash looked identical to a slow run: the status
    message just sat at "downloading" forever with no way to tell whether
    to keep waiting.
    """
    log.error("[bot] unhandled exception", exc_info=context.error)
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    err = context.error
    if isinstance(err, telegram_error.TimedOut):
        text = (
            "⏱️ Timeout waktu ambil video dari Telegram.\n\n"
            "Kalau videonya besar banget, coba kirim ulang — "
            "server lokal biasanya sudah menyimpan sebagian, jadi "
            "percobaan kedua lebih cepat."
        )
    else:
        text = f"💥 Gagal: {type(err).__name__}: {err}"
    try:
        await message.reply_text(text)
    except Exception:  # noqa: BLE001
        log.exception("[bot] could not report the error to the chat")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not config.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is empty. See telegram_bot.py's docstring → 'Setup'."
        )

    builder = (
        Application.builder()
        .token(config.telegram_bot_token)
        # getFile against a Local Bot API Server blocks while that server
        # pulls the whole file from Telegram — minutes for the untouched HD
        # masters this is built for. The library's 5s default aborts long
        # before that and the video is silently dropped. Sending the TikTok
        # kit back pushes tens of MB the other way, hence the write budget.
        .connect_timeout(30)
        .read_timeout(900)
        .write_timeout(900)
        .pool_timeout(60)
        .media_write_timeout(1800)
    )
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
