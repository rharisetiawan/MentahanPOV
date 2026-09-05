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

A video isn't posted immediately: it first runs `main.py --dry-run`
(metadata, Gemini caption, master upload to Drive — no watermark, no
posting) and replies with a ✅ Post / ✏️ Edit caption / ❌ Batal keyboard
so a wrong upload or a bad caption can be caught before anything actually
goes out. Confirming resumes with `--skip-gdrive --skip-gemini` so none of
that dry-run work is repeated. See handle_video, handle_confirm_callback,
and handle_text_reply.
"""

from __future__ import annotations

import ast
import asyncio
import datetime
import html
import logging
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram import error as telegram_error
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config
from core import health, video_facts
from core import state as pipeline_state

# The HG680 doesn't necessarily run in the same timezone the daily report
# should land in — hardcoded rather than trusting container TZ, which
# Docker defaults to UTC unless explicitly set.
_REPORT_TZ = ZoneInfo("Asia/Jakarta")

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


# chat_id -> state key, set when "✏️ Edit caption" is tapped so the next
# plain-text message from that chat is read as the replacement caption
# instead of falling through to handle_other's generic reply. Module-level
# and unlocked is fine here: the bot is single-process asyncio, and this is
# just routing state for a human typing a reply within the next message or
# two, not something that needs to survive a restart.
_PENDING_EDIT: dict[int, str] = {}


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


async def _run_pipeline(
    video_path: Path, status: LiveStatus, *, extra_args: tuple[str, ...] = ()
) -> str:
    args = [sys.executable, "-u", "main.py", str(video_path), *extra_args]
    # argparse takes the *last* --platforms if it's passed twice, so this
    # would silently override a caller-supplied one (e.g. a single-platform
    # retry) with the full configured list — only fill it in when the
    # caller didn't already ask for something more specific.
    if config.telegram_platforms and "--platforms" not in extra_args:
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
    #
    # This only runs main.py --dry-run (facts + geocode + Gemini caption +
    # Drive master upload — no watermark, no posting) so there's something
    # concrete to review before anything actually goes out. Confirming
    # resumes with --skip-gdrive --skip-gemini so nothing here gets redone.
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
                "🔍 Cek metadata + siapin caption (dry-run)...",
                note="Biasanya 1-3 menit: metadata → Gemini → upload master ke Drive.",
            )
            result = await _run_pipeline(local_path, status, extra_args=("--dry-run",))
        except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
            log.exception("[bot] dry-run crashed")
            await status.finish(f"💥 Error gak terduga: {type(exc).__name__}: {exc}")
            return

        entry = pipeline_state.get(config.state_file, local_path)
        ready = bool(entry.get("caption")) and bool(entry.get("gemini", {}).get("file_name"))
        if not ready:
            await status.finish(f"⚠️ Gagal nyiapin video buat direview.\n\n{result}")
            return
        await status.finish("🔍 Siap direview — cek pesan di bawah 👇")

    await _send_confirmation(update.message, local_path)


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


def _confirmation_text(entry: dict) -> str:
    gemini = entry.get("gemini", {})
    return (
        f"📁 Folder: {gemini.get('folder', '?')}\n"
        f"📄 Nama file: {gemini.get('file_name', '?')}\n\n"
        f"📝 Caption:\n{entry.get('caption', '(kosong)')}\n\n"
        "Lanjut posting ke semua platform?"
    )


async def _send_confirmation(message, video_path: Path) -> None:
    entry = pipeline_state.get(config.state_file, video_path)
    key = pipeline_state.video_key(video_path)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Post", callback_data=f"post:{key}"),
                InlineKeyboardButton("✏️ Edit caption", callback_data=f"edit:{key}"),
                InlineKeyboardButton("❌ Batal", callback_data=f"cancel:{key}"),
            ]
        ]
    )
    await message.reply_text(_confirmation_text(entry), reply_markup=keyboard)


async def handle_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles the ✅ Post / ✏️ Edit caption / ❌ Batal buttons from
    _send_confirmation. video_path is looked up from state.json by key
    rather than carried in callback_data (Telegram caps that at 64 bytes,
    nowhere near enough for a real path) — see
    core.state.video_path_for_key.
    """
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    if update.effective_user is None or not _allowed(update.effective_user.id):
        await query.answer("Belum diizinkan.", show_alert=True)
        return
    await query.answer()

    action, _, rest = query.data.partition(":")
    # retry's callback_data carries a platform name too: "retry:{key}:{platform}".
    key, _, platform = rest.partition(":")
    video_path = pipeline_state.video_path_for_key(config.state_file, key)
    if video_path is None:
        await query.edit_message_text(
            "⚠️ Sesi ini sudah gak valid (bot mungkin sempat restart). "
            "Kirim ulang videonya."
        )
        return

    if action == "cancel":
        _PENDING_EDIT.pop(query.message.chat_id, None)
        try:
            if video_path.exists():
                video_path.unlink()
        except OSError:
            log.exception("[bot] failed to delete cancelled video: %s", video_path)
        await query.edit_message_text("❌ Dibatalkan, video dihapus.")
        return

    if action == "edit":
        _PENDING_EDIT[query.message.chat_id] = key
        await query.edit_message_text(
            "✏️ Kirim caption baru sebagai balasan (teks biasa)."
        )
        return

    if action == "post":
        await query.edit_message_text("▶️ Lanjut posting...")
        status_msg = await query.message.reply_text("⚙️ Mulai posting...")
        async with LiveStatus(status_msg, "Bersiap posting...") as status:
            status.set(
                "⚙️ Mulai posting...",
                note="Watermark → posting ke platform, biasanya 2-4 menit.",
            )
            try:
                result = await _run_pipeline(
                    video_path, status, extra_args=("--skip-gdrive", "--skip-gemini")
                )
            except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
                log.exception("[bot] confirmed pipeline run crashed")
                result = f"💥 Error gak terduga: {type(exc).__name__}: {exc}"
            await status.finish(result)

        entry = pipeline_state.get(config.state_file, video_path)
        if entry.get("platforms", {}).get("tiktok", {}).get("status") != "ok":
            # Only the manual fallback: an automated TikTok post (see
            # distributors/tiktok_remote.py) already shows up in `result`
            # above like any other platform — sending the kit on top of a
            # real success would just be confusing leftover-menu noise.
            await _send_tiktok_kit(query.message, video_path)
        await _send_retry_keyboard(query.message, video_path, entry)
        return

    if action == "retry":
        if not platform:
            return
        # Remove just the tapped button (not the whole keyboard — a
        # message can offer retries for several failed platforms at once,
        # and the others should stay tappable) so a double-tap can't fire
        # two concurrent retries of the same platform, which could
        # genuinely double-post.
        try:
            markup = query.message.reply_markup
            if markup:
                rows = [
                    [b for b in row if b.callback_data != query.data]
                    for row in markup.inline_keyboard
                ]
                rows = [row for row in rows if row]
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(rows) if rows else None
                )
        except Exception:  # noqa: BLE001 — cosmetic only, never blocks the retry
            pass
        status_msg = await query.message.reply_text(f"🔁 Retry {platform}...")
        async with LiveStatus(status_msg, f"🔁 Retry {platform}...") as status:
            try:
                result = await _run_pipeline(
                    video_path,
                    status,
                    extra_args=(
                        "--platforms",
                        platform,
                        "--skip-gdrive",
                        "--skip-gemini",
                        "--skip-watermark",
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
                log.exception("[bot] retry crashed")
                result = f"💥 Error gak terduga: {type(exc).__name__}: {exc}"
            await status.finish(result)

        entry = pipeline_state.get(config.state_file, video_path)
        await _send_retry_keyboard(query.message, video_path, entry)
        return


async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Routes a plain-text message to whichever ✏️ Edit caption request is
    pending for this chat, if any — otherwise falls through to
    handle_other's generic reply, same as any other non-video message."""
    if update.message is None or update.effective_chat is None:
        return
    key = _PENDING_EDIT.get(update.effective_chat.id)
    if key is None:
        await handle_other(update, context)
        return
    if update.effective_user is None or not _allowed(update.effective_user.id):
        return

    _PENDING_EDIT.pop(update.effective_chat.id, None)
    video_path = pipeline_state.video_path_for_key(config.state_file, key)
    if video_path is None:
        await update.message.reply_text(
            "⚠️ Sesi ini sudah gak valid, kirim ulang videonya."
        )
        return

    pipeline_state.update(config.state_file, video_path, {"caption": update.message.text})
    await update.message.reply_text("✅ Caption diupdate.")
    await _send_confirmation(update.message, video_path)


async def _send_retry_keyboard(message, video_path: Path, entry: dict) -> None:
    """One 🔁 Retry button per platform that didn't end up "ok", so a
    single YouTube failure (say) doesn't mean re-doing Instagram/Facebook
    too — each button re-runs main.py for just that platform with
    --skip-gdrive --skip-gemini --skip-watermark, reusing everything
    already produced. No-op if everything already succeeded."""
    key = pipeline_state.video_key(video_path)
    failed = [
        name
        for name, info in entry.get("platforms", {}).items()
        if info.get("status") != "ok"
    ]
    if not failed:
        return
    buttons = [
        InlineKeyboardButton(f"🔁 Retry {name}", callback_data=f"retry:{key}:{name}")
        for name in failed
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    await message.reply_text(
        "Ada platform yang gagal — retry satu-satu tanpa ngulang yang lain:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _send_tiktok_kit(message, source_video: Path) -> None:
    """Fallback only: send the watermarked file + caption so TikTok can be
    posted by hand from the phone that's already in your hand.

    Only called when TikTok's automated path (distributors/tiktok_remote.py
    when TIKTOK_WORKER_HOST is set, distributors/tiktok.py when a local
    Playwright/Chromium is available) either isn't configured for this run
    or actually failed — a successful automated post already shows up in
    the normal per-platform result text, so sending this on top of that
    would just be confusing leftover-menu noise from before the remote
    worker existed.
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


def _format_health_report(results: list[health.CheckResult]) -> str:
    worst = "ok"
    for r in results:
        if r.status == "error":
            worst = "error"
            break
        if r.status == "warning":
            worst = "warning"
    header = {
        "ok": "✅ Semua integrasi normal",
        "warning": "⚠️ Ada yang perlu diperhatikan",
        "error": "❌ Ada yang rusak, perlu ditindaklanjuti",
    }[worst]
    lines = [f"{r.emoji} {r.name}: {r.detail}" for r in results]
    return header + "\n\n" + "\n".join(lines)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not _allowed(update.effective_user.id):
        return
    msg = await update.message.reply_text("🔍 Ngecek Gemini, Google Drive, YouTube, Meta, Threads...")
    # health.run_all() makes blocking network calls (requests, google-auth,
    # google-genai) — off the event loop so it can't stall the bot's
    # polling loop or any run already in progress.
    results = await asyncio.to_thread(health.run_all)
    await msg.edit_text(_format_health_report(results))


async def handle_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the admin dashboard's link — see admin.py + DEPLOYMENT.md
    -> "Admin dashboard". Same allowlist as /status and video uploads:
    whoever can trigger a post can also rewrite the tokens behind it, so
    this is not something to hand out more loosely than that."""
    if update.message is None or update.effective_user is None:
        return
    if not _allowed(update.effective_user.id):
        return
    if config.admin_dashboard_url:
        await update.message.reply_text(
            f"🛠 Admin dashboard: {config.admin_dashboard_url}\n\n"
            "Login pakai DASHBOARD_USER/DASHBOARD_PASSWORD. Butuh Tailscale "
            "aktif di HP/laptop kalau lagi di luar rumah.",
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "ADMIN_DASHBOARD_URL belum di-set di .env — lihat DEPLOYMENT.md "
            "-> 'Admin dashboard' buat cara aksesnya."
        )


async def daily_status_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    results = await asyncio.to_thread(health.run_all)
    text = "📋 Laporan status harian\n\n" + _format_health_report(results)
    for user_id in config.telegram_allowed_user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception:  # noqa: BLE001 — one recipient failing shouldn't skip the rest
            log.exception("[bot] failed to send daily status to %s", user_id)


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


_BOT_COMMANDS = [
    BotCommand("status", "Cek status integrasi (Gemini, Drive, YouTube, Meta, Threads)"),
    BotCommand("dashboard", "Link ke admin dashboard (edit token, kontrol bot)"),
]


async def _post_init(app: Application) -> None:
    """Registers the / command menu Telegram shows in the chat's own UI
    (the "/" or menu-icon picker) — without this, /status and /dashboard
    still work, they just aren't listed anywhere; you'd have to already
    know to type them. Safe to call on every startup: Telegram just
    overwrites the list with the same values."""
    await app.bot.set_my_commands(_BOT_COMMANDS)


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
        .post_init(_post_init)
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
    # CommandHandler/CallbackQueryHandler/the TEXT handler must all be
    # registered before the filters.ALL catch-all below — same group,
    # first match wins, and filters.ALL matches everything else does too.
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("dashboard", handle_dashboard))
    app.add_handler(
        CallbackQueryHandler(handle_confirm_callback, pattern=r"^(post|cancel|edit|retry):")
    )
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )
    # Catches the caption text typed after "✏️ Edit caption"; falls through
    # to handle_other itself when no edit is pending for that chat.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_reply))
    app.add_handler(MessageHandler(filters.ALL, handle_other))
    app.add_error_handler(handle_error)

    if config.telegram_allowed_user_ids:
        app.job_queue.run_daily(
            daily_status_job,
            time=datetime.time(hour=config.daily_status_hour, tzinfo=_REPORT_TZ),
        )
        log.info(
            "[bot] daily status report scheduled for %02d:00 WIB",
            config.daily_status_hour,
        )
    else:
        log.warning(
            "[bot] TELEGRAM_ALLOWED_USER_IDS empty — daily status report has "
            "nowhere to send to, disabled. /status still works on demand."
        )

    log.info("[bot] MentahanPOV bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
