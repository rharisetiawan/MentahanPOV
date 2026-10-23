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

Beyond that, the bot also owns the box's housekeeping, because it's the one
long-running process there: a heartbeat + disk-pressure tick
(housekeeping_job), automatic cleanup of finished/abandoned videos
(core/cleanup.py; /cleanup), scheduled posts that survive a restart
(📅 Jadwalkan, /jadwal), post performance collection (/insights), and a
"back online after X" message on startup. Commands: /status, /dashboard,
/cleanup, /jadwal, /insights.
"""

from __future__ import annotations

import ast
import asyncio
import datetime
import html
import logging
import os
import re
import signal
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
from core import (
    cleanup,
    health,
    heartbeat,
    insights,
    logsafe,
    threads_token,
    video_facts,
)
from core import schedule as schedule_lib
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
# main.py logs one of these when a platform has real credentials configured
# but wasn't in this run's platform list — the exact "set up but nothing
# ever posts there" symptom, previously silent. Surfaced here so it reaches
# the chat instead of only sitting in the run log file.
_CONFIG_CHECK_RE = re.compile(r"\[config-check\]\s*(.+)$")

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
# Same idea for "✍️ Ketik jam": chat_id -> state key, so the next plain-text
# message from that chat is parsed as a posting time (core/schedule.py).
_PENDING_SCHEDULE: dict[int, str] = {}


# Only one main.py subprocess runs at a time, across every chat — the
# HG680 this normally runs on is weak enough that two pipelines fighting
# over it (watermark render, uploads) would likely make both slower than
# running back-to-back, not faster. asyncio.Lock is FIFO, so a second
# caller just waits its turn with no extra queue bookkeeping needed.
_PIPELINE_LOCK = asyncio.Lock()

# The single in-flight run's subprocess + video_key, so a "🛑 Stop" tap
# (a *different* update, handled concurrently by handle_confirm_callback)
# can find and kill it. None when idle. Safe unlocked: only the coroutine
# inside _run_pipeline ever reassigns this name; a stop tap only mutates
# the dict it points at ("stopped"), which asyncio's single-threaded
# cooperative scheduling makes safe without an explicit lock.
_ACTIVE_RUN: dict[str, object] | None = None


def _config_check_notes(text: str) -> list[str]:
    """Pull out main.py's "configured but not selected" warnings, deduped."""
    seen: list[str] = []
    for line in text.splitlines():
        m = _CONFIG_CHECK_RE.search(line)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    # main.py's logging.basicConfig() defaults to stderr, not stdout — the
    # "Final state: {...}" summary line lives there, so both must be
    # searched (or it's silently "not found" even on a full success).
    full = stdout + "\n" + stderr
    match = None
    for line in full.splitlines():
        m = _FINAL_STATE_RE.search(line)
        if m:
            match = m
    notes = _config_check_notes(full)
    notes_block = (
        "\n\n⚠️ Catatan konfigurasi:\n" + "\n".join(f"- {n}" for n in notes)
        if notes
        else ""
    )
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
            return header + "\n" + "\n".join(lines) + notes_block

    # Fallback: couldn't find/parse the summary line — dump the tail.
    tail = (stdout[-1500:] + "\n" + stderr[-1000:]).strip()
    status = "selesai" if returncode == 0 else f"gagal (exit {returncode})"
    return (
        f"Proses {status}, tapi gak nemu ringkasan hasil. Log terakhir:\n"
        f"```\n{tail}\n```{notes_block}"
    )


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
    try:
        notes = _config_check_notes(log_path.read_text(encoding="utf-8"))
    except OSError:
        notes = []
    if notes:
        lines.append("\n⚠️ Catatan konfigurasi:")
        lines.extend(f"- {n}" for n in notes)
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

    def __init__(
        self,
        message,
        stage: str,
        *,
        timeout_s: int = PIPELINE_TIMEOUT_S,
        stop_key: str | None = None,
    ) -> None:
        self._message = message
        self._stage = stage
        self._note = ""
        self._start = time.monotonic()
        self._tick = 0
        self._timeout_s = timeout_s
        self._last_checkpoint = 0.0
        self._task: asyncio.Task | None = None
        self._stop_key = stop_key

    def set(self, stage: str, note: str = "") -> None:
        self._stage = stage
        self._note = note

    def set_stop_key(self, key: str) -> None:
        """Attach the 🛑 Stop button once the video_key is known — handle_video
        doesn't have one yet when its LiveStatus is first constructed, since
        that's only derivable from local_path after the Telegram download."""
        self._stop_key = key

    def _markup(self) -> InlineKeyboardMarkup | None:
        if not self._stop_key:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 Stop", callback_data=f"stop:{self._stop_key}")]]
        )

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
                # reply_markup passed explicitly every tick (not just set
                # once) so the Stop button can't silently disappear on a
                # later edit — safer than relying on exactly how Telegram
                # treats an omitted reply_markup on an edit call.
                await self._message.edit_text(self._render(), reply_markup=self._markup())
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
            # reply_markup=None here (not self._markup()) so the Stop
            # button is removed once there's nothing left to stop.
            await self._message.edit_text(
                text, disable_web_page_preview=True, reply_markup=None
            )
        except Exception:  # noqa: BLE001
            log.exception("[bot] failed to edit final status, sending fresh one")
            await self._message.reply_text(text, disable_web_page_preview=True)


def _kill_run(proc: asyncio.subprocess.Process) -> None:
    """Kill main.py *and* what it spawned (ffmpeg renders).

    A plain proc.kill() only hits main.py, leaving an orphaned ffmpeg
    eating the box's CPU for minutes. On POSIX the run gets its own
    session (see _run_pipeline), so its whole process group goes at once.
    """
    if proc.returncode is not None:
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            return
        except ProcessLookupError:
            return
        except OSError:
            pass  # fall back to killing just main.py
    proc.kill()


async def _run_pipeline(
    video_path: Path, status: LiveStatus, *, extra_args: tuple[str, ...] = ()
) -> str:
    global _ACTIVE_RUN
    key = pipeline_state.video_key(video_path)

    if _PIPELINE_LOCK.locked():
        status.set("⏳ Antre — ada proses lain lagi jalan, ini otomatis lanjut begitu selesai...")

    async with _PIPELINE_LOCK:
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
            # Own process group, so _kill_run can take ffmpeg down with it.
            start_new_session=os.name == "posix",
        )
        # Lets a "🛑 Stop" tap on a *different* update find this process —
        # see handle_confirm_callback's "stop" branch. run_info is kept as
        # a local alias so we can still read the "stopped" flag below even
        # after clearing the module-level _ACTIVE_RUN.
        run_info: dict[str, object] = {"key": key, "proc": proc, "stopped": False}
        _ACTIVE_RUN = run_info

        # main.py's own stdout/stderr only ever lived in an in-memory list
        # before this — invisible to `docker compose logs` and gone forever if
        # the process got killed on timeout. Persisting it as it streams means
        # a run that goes wrong can actually be cross-checked afterward instead
        # of guessed at.
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = RUN_LOG_DIR / f"{key}.log"

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
            _kill_run(proc)
            await proc.wait()
            minutes = PIPELINE_TIMEOUT_S // 60
            return _format_partial_result(
                video_path,
                f"⏱️ Lewat {minutes} menit, aku hentiin paksa. Yang sempat kepublish:",
                log_path,
            )
        finally:
            _ACTIVE_RUN = None

        if run_info["stopped"]:
            return _format_partial_result(
                video_path, "🛑 Dihentikan manual. Yang sempat kepublish:", log_path
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

    # Refuse up front, with a reason, rather than let the download fill the
    # disk and die half-written (which also leaves a corrupt file in
    # incoming/ that nothing would ever clean up).
    if not await _ensure_disk_room(update.message, size_mb):
        return

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
            raw_filename = (
                getattr(tg_video, "file_name", None) or f"{tg_video.file_unique_id}.mp4"
            )
            # .name strips any directory components (including "../" and
            # absolute/drive-rooted paths) from the attacker-controlled
            # Telegram filename, so it can't escape INCOMING_DIR. It's not
            # quite enough on its own though: Path("..").name == ".."
            # (pathlib doesn't special-case a bare ".." down to "" the way
            # it does "."), which would still resolve one directory up —
            # so that exact value has to be rejected too.
            filename = Path(raw_filename).name
            if not filename or filename == "..":
                filename = f"{tg_video.file_unique_id}.mp4"
            local_path = INCOMING_DIR / filename
            # Only derivable once local_path exists — attaches the 🛑 Stop
            # button (see LiveStatus) for the rest of this dry-run.
            status.set_stop_key(pipeline_state.video_key(local_path))
            status.set(f"Simpan {filename}...")
            await tg_file.download_to_drive(str(local_path), read_timeout=900)

            await _warn_if_metadata_stripped(
                local_path, sent_as_document, update.message
            )

            status.set(
                "🔍 Cek metadata + siapin caption (dry-run)...",
                note="Biasanya 1-3 menit: metadata → Gemini → upload master ke Drive"
                + (" (watermark dirender sekalian)." if config.prerender_posting_copy else "."),
            )
            dry_run_args = ("--dry-run",)
            if config.prerender_posting_copy:
                dry_run_args += ("--prerender",)
            result = await _run_pipeline(local_path, status, extra_args=dry_run_args)
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


def _selected_platforms(entry: dict) -> set[str]:
    """Which platforms this specific pending post will go to.

    Stored per-video in state.json ("selected_platforms") the first time
    someone taps a toggle button; until then it falls back to the same
    default main.py itself would use, so an untouched confirmation screen
    still matches what "✅ Post" has always done.
    """
    stored = entry.get("selected_platforms")
    if stored is not None:
        return set(stored)
    return set(config.telegram_platforms or config.platforms)


def _confirmation_text(entry: dict, selected: set[str]) -> str:
    gemini = entry.get("gemini", {})
    platforms_line = ", ".join(sorted(selected)) if selected else "(belum pilih platform)"
    return (
        f"📁 Folder: {gemini.get('folder', '?')}\n"
        f"📄 Nama file: {gemini.get('file_name', '?')}\n\n"
        f"📝 Caption:\n{entry.get('caption', '(kosong)')}\n\n"
        f"{_variants_text(entry)}"
        f"📤 Platform: {platforms_line}\n"
        "(ketuk buat centang/uncentang)\n\n"
        "Lanjut posting?"
    )


def _variants_text(entry: dict) -> str:
    """The per-platform rewrites (main.caption_for), shown before posting."""
    variants = entry.get("caption_variants") or {}
    parts = []
    if variants.get("youtube_title"):
        parts.append(f"🎬 Judul YouTube: {variants['youtube_title']}\n\n")
    if variants.get("threads_text"):
        parts.append(f"🧵 Versi Threads:\n{variants['threads_text']}\n\n")
    return "".join(parts)


def _confirmation_keyboard(key: str, selected: set[str]) -> InlineKeyboardMarkup:
    # Local import, same reasoning as admin/app.py's run_page(): PLATFORM_REGISTRY
    # lives in main.py, which this module otherwise only ever shells out to
    # (never imports at module scope) — see _run_pipeline.
    from main import PLATFORM_REGISTRY

    toggle_buttons = [
        InlineKeyboardButton(
            f"{'☑️' if name in selected else '⬜'} {name}",
            callback_data=f"toggle:{key}:{name}",
        )
        for name in sorted(PLATFORM_REGISTRY)
    ]
    rows = [toggle_buttons[i : i + 2] for i in range(0, len(toggle_buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton("✅ Post", callback_data=f"post:{key}"),
            InlineKeyboardButton("📅 Jadwalkan", callback_data=f"sched:{key}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("✏️ Edit caption", callback_data=f"edit:{key}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"cancel:{key}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _send_confirmation(message, video_path: Path) -> None:
    entry = pipeline_state.get(config.state_file, video_path)
    key = pipeline_state.video_key(video_path)
    selected = _selected_platforms(entry)
    await message.reply_text(
        _confirmation_text(entry, selected),
        reply_markup=_confirmation_keyboard(key, selected),
    )


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
        _PENDING_SCHEDULE.pop(query.message.chat_id, None)
        await _clear_schedule(context.application, key, video_path)
        try:
            if video_path.exists():
                video_path.unlink()
        except OSError:
            log.exception("[bot] failed to delete cancelled video: %s", video_path)
        await query.edit_message_text("❌ Dibatalkan, video dihapus.")
        return

    if action == "edit":
        _PENDING_SCHEDULE.pop(query.message.chat_id, None)
        _PENDING_EDIT[query.message.chat_id] = key
        await query.edit_message_text(
            "✏️ Kirim caption baru sebagai balasan (teks biasa)."
        )
        return

    if action == "toggle":
        if not platform:
            return
        entry = pipeline_state.get(config.state_file, video_path)
        selected = _selected_platforms(entry)
        if platform in selected:
            selected.discard(platform)
        else:
            selected.add(platform)
        entry = pipeline_state.update(
            config.state_file, video_path, {"selected_platforms": sorted(selected)}
        )
        try:
            await query.edit_message_text(
                _confirmation_text(entry, selected),
                reply_markup=_confirmation_keyboard(key, selected),
            )
        except Exception:  # noqa: BLE001 — Telegram 400s on a no-op edit; harmless
            pass
        return

    if action == "stop":
        # query.answer() already happened, bare, right above — Telegram
        # only accepts one answer per callback query, so feedback here has
        # to go through a message, not a second answer(..., show_alert=True).
        active = _ACTIVE_RUN
        if active and active.get("key") == key:
            active["stopped"] = True
            _kill_run(active["proc"])  # type: ignore[arg-type]
        else:
            await query.message.reply_text(
                "Udah kelar, atau bukan proses yang lagi jalan."
            )
        return

    if action == "post":
        entry = pipeline_state.get(config.state_file, video_path)
        selected = _selected_platforms(entry)
        if not selected:
            await query.message.reply_text(
                "⚠️ Pilih minimal satu platform dulu (ketuk salah satu tombol di atas)."
            )
            return

        if not video_path.exists():
            await query.edit_message_text(_missing_video_text())
            return

        # Posting now supersedes any schedule this video had.
        await _clear_schedule(context.application, key, video_path)
        await query.edit_message_text("▶️ Lanjut posting...")
        await _execute_post(query.message, key, video_path, selected)
        return

    if action == "retry":
        if not platform:
            return
        if not video_path.exists():
            await query.message.reply_text(_missing_video_text())
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
        async with LiveStatus(status_msg, f"🔁 Retry {platform}...", stop_key=key) as status:
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
        await _send_caption_recap(query.message, entry.get("caption", ""))
        await _send_retry_keyboard(query.message, video_path, entry)
        await _after_run()
        return


def _missing_video_text() -> str:
    days = getattr(config, "abandoned_retention_days", 0)
    why = (
        f"kemungkinan dibersihkan otomatis karena lebih dari {days} hari gak "
        "dikonfirmasi"
        if days > 0
        else "kemungkinan sudah dibatalkan atau dihapus"
    )
    return (
        f"⚠️ File video-nya sudah gak ada di server — {why} "
        "(master-nya tetap aman di Google Drive). Kirim ulang videonya."
    )


async def _execute_post(
    message, key: str, video_path: Path, selected: set[str]
) -> None:
    """Run the confirmed post and report back, as replies to `message`.

    One implementation for every way a post starts — the ✅ Post button and
    a scheduled job (which has no callback query, only a chat to write to).
    """
    # Record the choice explicitly: until someone toggles a platform the
    # selection exists only as a *default* computed on the fly, and
    # core/cleanup.py needs the real list to tell "finished" from "still
    # running" (a parallel run records platforms one at a time).
    pipeline_state.update(
        config.state_file, video_path, {"selected_platforms": sorted(selected)}
    )

    status_msg = await message.reply_text("⚙️ Mulai posting...")
    async with LiveStatus(status_msg, "Bersiap posting...", stop_key=key) as status:
        status.set(
            "⚙️ Mulai posting...",
            note=(
                "Posting ke platform, biasanya 1-3 menit."
                if config.prerender_posting_copy
                else "Watermark → posting ke platform, biasanya 2-4 menit."
            ),
        )
        extra_args: tuple[str, ...] = (
            "--skip-gdrive",
            "--skip-gemini",
            "--platforms",
            ",".join(sorted(selected)),
        )
        if config.prerender_posting_copy:
            # The dry-run already rendered it; a missing file still re-renders.
            extra_args += ("--skip-watermark",)
        try:
            result = await _run_pipeline(video_path, status, extra_args=extra_args)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
            log.exception("[bot] confirmed pipeline run crashed")
            result = f"💥 Error gak terduga: {type(exc).__name__}: {exc}"
        await status.finish(result)

    entry = pipeline_state.get(config.state_file, video_path)
    await _send_caption_recap(message, entry.get("caption", ""))
    await _send_retry_keyboard(message, video_path, entry)
    await _after_run()


async def _after_run() -> None:
    """Housekeeping once a run is over: let cleanup reclaim whatever that run
    just made safe to delete. Never raises — a failure here must not look
    like the post itself failed."""
    try:
        if config.auto_cleanup and not _PIPELINE_LOCK.locked():
            report = await asyncio.to_thread(cleanup.run, apply_changes=True)
            if report.deleted:
                log.info(
                    "[bot] post-run cleanup freed %s (%d file(s))",
                    cleanup.fmt_bytes(report.freed_bytes),
                    report.deleted,
                )
    except Exception:  # noqa: BLE001
        log.exception("[bot] post-run housekeeping failed")


async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Routes a plain-text message to whichever ✏️ Edit caption request is
    pending for this chat, if any — otherwise falls through to
    handle_other's generic reply, same as any other non-video message."""
    if update.message is None or update.effective_chat is None:
        return
    if update.effective_chat.id in _PENDING_SCHEDULE:
        await _handle_schedule_time_reply(update, context)
        return
    key = _PENDING_EDIT.get(update.effective_chat.id)
    if key is None:
        await handle_other(update, context)
        return
    if update.effective_user is None or not _allowed(update.effective_user.id):
        return

    _PENDING_EDIT.pop(update.effective_chat.id, None)
    # (a typed caption can't also be a typed time — see the start of this
    # function — so there is nothing pending in _PENDING_SCHEDULE here.)
    video_path = pipeline_state.video_path_for_key(config.state_file, key)
    if video_path is None:
        await update.message.reply_text(
            "⚠️ Sesi ini sudah gak valid, kirim ulang videonya."
        )
        return

    # A hand-written caption replaces Gemini's per-platform rewrites too:
    # they were derived from the old text and would contradict the edit.
    pipeline_state.update(
        config.state_file,
        video_path,
        {"caption": update.message.text, "caption_variants": {}},
    )
    await update.message.reply_text(
        "✅ Caption diupdate (dipakai juga buat YouTube & Threads)."
    )
    await _send_confirmation(update.message, video_path)


async def _send_caption_recap(message, caption: str) -> None:
    """Always resend the caption as a one-tap-copy block after a post or a
    retry, success or not.

    Not conditional on a visible failure — the (now archived) TikTok
    empty-caption bug (2026-09-05) posted successfully with no caption and *no error
    anywhere*, so gating this on "something looked wrong" would have
    missed exactly the case it exists for. Having the real caption in the
    chat every time means a silent miss on any platform is still just a
    copy-paste away from fixed by hand, not a trip to the server.
    """
    if not caption:
        return
    await message.reply_text(
        f"📝 Caption yang dipakai (buat jaga-jaga kalau ada yang perlu diposting/dibetulin manual):\n"
        f"<pre>{html.escape(caption)}</pre>",
        parse_mode="HTML",
    )


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


async def disk_watch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every few hours: speak up only when the disk is getting tight."""
    result = await asyncio.to_thread(health.check_disk_space)
    if result.status == "ok":
        return
    text = (
        f"{result.emoji} Disk server: {result.detail}\n"
        "Kirim /cleanup buat hapus video lama yang sudah selesai."
    )
    for user_id in config.telegram_allowed_user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception:  # noqa: BLE001
            log.exception("[bot] failed to send disk alert to %s", user_id)


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


# --- scheduled posting ------------------------------------------------------
#
# A schedule lives in state/posts.json ("scheduled_at", "schedule_chat_id"),
# not only in the job queue: the HG680 is exactly the kind of box that
# reboots or loses power, and an in-memory-only schedule would silently
# evaporate. _restore_schedules re-registers every pending one on startup
# (an overdue one runs shortly after the bot comes back, with a note saying
# how late it is — posting late beats never posting).


def _now_local() -> datetime.datetime:
    return datetime.datetime.now(_REPORT_TZ)


def _scheduled_when(entry: dict) -> datetime.datetime | None:
    raw = entry.get("scheduled_at")
    if not raw:
        return None
    try:
        ts = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(_REPORT_TZ)


def _remove_schedule_jobs(application: Application, key: str) -> None:
    if application.job_queue is None:
        return
    for job in application.job_queue.get_jobs_by_name(f"sched:{key}"):
        job.schedule_removal()


def _register_schedule_job(
    application: Application, key: str, when: datetime.datetime
) -> None:
    _remove_schedule_jobs(application, key)
    if application.job_queue is None:
        log.error("[bot] no job queue — scheduled post for %s won't fire", key)
        return
    application.job_queue.run_once(
        _scheduled_post_job, when=when, name=f"sched:{key}", data={"key": key}
    )


async def _clear_schedule(
    application: Application, key: str, video_path: Path
) -> None:
    _remove_schedule_jobs(application, key)
    entry = pipeline_state.get(config.state_file, video_path)
    if entry.get("scheduled_at"):
        pipeline_state.update(
            config.state_file,
            video_path,
            {"scheduled_at": None, "schedule_chat_id": None},
        )


async def _set_schedule(
    application: Application,
    key: str,
    video_path: Path,
    when: datetime.datetime,
    chat_id: int,
) -> None:
    pipeline_state.update(
        config.state_file,
        video_path,
        {
            "scheduled_at": when.astimezone(datetime.timezone.utc).isoformat(),
            "schedule_chat_id": chat_id,
        },
    )
    _register_schedule_job(application, key, when)


def _scheduled_text(entry: dict, when: datetime.datetime) -> str:
    gemini = entry.get("gemini", {})
    platforms = ", ".join(sorted(_selected_platforms(entry))) or "-"
    return (
        f"📅 Dijadwalkan: {schedule_lib.format_when(when, _now_local())} WIB\n"
        f"📄 {gemini.get('file_name', '?')}\n"
        f"📤 {platforms}\n\n"
        f"📝 Caption:\n{entry.get('caption', '(kosong)')}\n\n"
        "Bot yang posting otomatis pas jamnya. Kalau bot sempat mati lewat "
        "jam itu, posting jalan begitu bot hidup lagi."
    )


def _scheduled_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 Post sekarang", callback_data=f"post:{key}"),
                InlineKeyboardButton("❌ Batalkan jadwal", callback_data=f"schedx:{key}"),
            ]
        ]
    )


def _schedule_keyboard(key: str, now: datetime.datetime) -> InlineKeyboardMarkup:
    options: list[tuple[str, datetime.datetime]] = []
    for label, delta in (("+1 jam", 1), ("+3 jam", 3)):
        at = (now + datetime.timedelta(hours=delta)).replace(second=0, microsecond=0)
        options.append((f"⏱ {label}", at))
    for slot in schedule_lib.upcoming_slots(now, config.schedule_slot_hours, 2):
        options.append((f"🕐 {schedule_lib.format_when(slot, now)}", slot))
    buttons = [
        InlineKeyboardButton(label, callback_data=f"sched:{key}:{int(at.timestamp())}")
        for label, at in options
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton("✍️ Ketik jam", callback_data=f"sched:{key}:custom"),
            InlineKeyboardButton("⬅️ Kembali", callback_data=f"sched:{key}:back"),
        ]
    )
    return InlineKeyboardMarkup(rows)


_SCHEDULE_PROMPT = "📅 Mau diposting kapan? (waktu WIB)"


async def handle_schedule_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """📅 Jadwalkan flow: `sched:{key}` opens the picker, `sched:{key}:{epoch}`
    picks a slot, `sched:{key}:custom|back`, and `schedx:{key}` cancels."""
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    if update.effective_user is None or not _allowed(update.effective_user.id):
        await query.answer("Belum diizinkan.", show_alert=True)
        return
    await query.answer()

    action, _, rest = query.data.partition(":")
    key, _, arg = rest.partition(":")
    video_path = pipeline_state.video_path_for_key(config.state_file, key)
    if video_path is None:
        await query.edit_message_text(
            "⚠️ Sesi ini sudah gak valid (bot mungkin sempat restart). "
            "Kirim ulang videonya."
        )
        return
    chat_id = query.message.chat_id
    now = _now_local()

    if action == "schedx":
        _PENDING_SCHEDULE.pop(chat_id, None)
        await _clear_schedule(context.application, key, video_path)
        await query.edit_message_text("❌ Jadwal dibatalkan.")
        await _send_confirmation(query.message, video_path)
        return

    entry = pipeline_state.get(config.state_file, video_path)
    selected = _selected_platforms(entry)

    if not arg:
        if not selected:
            await query.message.reply_text(
                "⚠️ Pilih minimal satu platform dulu (ketuk salah satu tombol di atas)."
            )
            return
        if not video_path.exists():
            await query.edit_message_text(_missing_video_text())
            return
        _PENDING_SCHEDULE.pop(chat_id, None)
        await query.edit_message_text(
            _SCHEDULE_PROMPT, reply_markup=_schedule_keyboard(key, now)
        )
        return

    if arg == "back":
        _PENDING_SCHEDULE.pop(chat_id, None)
        await query.edit_message_text(
            _confirmation_text(entry, selected),
            reply_markup=_confirmation_keyboard(key, selected),
        )
        return

    if arg == "custom":
        _PENDING_EDIT.pop(chat_id, None)
        _PENDING_SCHEDULE[chat_id] = key
        await query.edit_message_text(
            "✍️ Ketik jamnya sebagai balasan (WIB).\n"
            "Contoh: 19:30 · besok 07:00 · lusa 18:00 · +90m · +2j"
        )
        return

    if arg.isdigit():
        when = datetime.datetime.fromtimestamp(int(arg), tz=_REPORT_TZ)
        if when - now < schedule_lib.MIN_LEAD:
            await query.edit_message_text(
                "⚠️ Jam itu sudah lewat/terlalu dekat — pilih lagi.\n\n"
                + _SCHEDULE_PROMPT,
                reply_markup=_schedule_keyboard(key, now),
            )
            return
        await _set_schedule(context.application, key, video_path, when, chat_id)
        entry = pipeline_state.get(config.state_file, video_path)
        await query.edit_message_text(
            _scheduled_text(entry, when), reply_markup=_scheduled_keyboard(key)
        )


async def _handle_schedule_time_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """The plain-text reply after "✍️ Ketik jam"."""
    assert update.message is not None and update.effective_chat is not None
    if update.effective_user is None or not _allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    key = _PENDING_SCHEDULE.get(chat_id)
    if key is None:
        return
    video_path = pipeline_state.video_path_for_key(config.state_file, key)
    if video_path is None:
        _PENDING_SCHEDULE.pop(chat_id, None)
        await update.message.reply_text(
            "⚠️ Sesi ini sudah gak valid, kirim ulang videonya."
        )
        return

    when = schedule_lib.parse_when(update.message.text or "", _now_local())
    if when is None:
        # Stay in "waiting for a time" — a typo shouldn't dump the chat out
        # of the flow and into the generic "kirim video" reply.
        await update.message.reply_text(
            "Gak kebaca / sudah lewat. Coba lagi: 19:30 · besok 07:00 · "
            "lusa 18:00 · +90m · +2j"
        )
        return

    _PENDING_SCHEDULE.pop(chat_id, None)
    await _set_schedule(context.application, key, video_path, when, chat_id)
    entry = pipeline_state.get(config.state_file, video_path)
    await update.message.reply_text(
        _scheduled_text(entry, when), reply_markup=_scheduled_keyboard(key)
    )


async def _scheduled_post_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    key = context.job.data["key"]  # type: ignore[index,union-attr]
    video_path = pipeline_state.video_path_for_key(config.state_file, key)
    if video_path is None:
        return
    entry = pipeline_state.get(config.state_file, video_path)
    when = _scheduled_when(entry)
    chat_id = entry.get("schedule_chat_id")
    if when is None or chat_id is None:
        return  # cancelled (or already fired) since this job was registered

    # Claim it before doing anything slow, so a restart mid-run can't make
    # _restore_schedules post the same video a second time.
    pipeline_state.update(
        config.state_file,
        video_path,
        {"scheduled_at": None, "schedule_chat_id": None},
    )
    if not video_path.exists():
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Posting terjadwal dibatalkan: file videonya sudah gak ada "
                f"({video_path.name}). Kirim ulang videonya."
            ),
        )
        return

    selected = _selected_platforms(entry)
    file_name = entry.get("gemini", {}).get("file_name") or video_path.name
    late_s = (_now_local() - when).total_seconds()
    note = ""
    if late_s > 600:
        note = (
            f"\n(telat {heartbeat.humanize(late_s)} dari jadwal "
            f"{schedule_lib.format_when(when)} — bot sempat mati)"
        )
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Posting terjadwal jalan sekarang.\n📄 {file_name}{note}",
    )
    await _execute_post(msg, key, video_path, selected)


def _restore_schedules(app: Application) -> int:
    """Re-register every pending schedule from state.json (see the header
    comment). Staggered so several overdue posts don't all start at once —
    they'd queue on _PIPELINE_LOCK anyway, this just keeps the chat orderly."""
    rows = pipeline_state.scheduled_entries(config.state_file)
    now = datetime.datetime.now(datetime.timezone.utc)
    restored = 0
    for i, (key, entry) in enumerate(rows):
        when = _scheduled_when(entry)
        if when is None:
            continue
        earliest = now + datetime.timedelta(seconds=45 + 10 * i)
        _register_schedule_job(app, key, max(when, earliest))
        restored += 1
    return restored


async def handle_jadwal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not _allowed(update.effective_user.id):
        return
    rows = pipeline_state.scheduled_entries(config.state_file)
    if not rows:
        await update.message.reply_text(
            "Belum ada posting terjadwal. Pakai tombol 📅 Jadwalkan di layar "
            "konfirmasi video."
        )
        return
    now = _now_local()
    lines = ["📅 Posting terjadwal (WIB):"]
    buttons = []
    for i, (key, entry) in enumerate(rows, 1):
        when = _scheduled_when(entry)
        name = entry.get("gemini", {}).get("file_name") or Path(
            entry.get("video_path", "?")
        ).name
        label = schedule_lib.format_when(when, now) if when else "?"
        lines.append(f"{i}. {label} — {name}")
        buttons.append(
            [InlineKeyboardButton(f"❌ Batalkan #{i}", callback_data=f"schedx:{key}")]
        )
    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
    )


# --- disk space: guard, /cleanup, background jobs ----------------------------


async def _notify(bot, text: str) -> None:
    for user_id in config.telegram_allowed_user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception:  # noqa: BLE001 — one recipient failing shouldn't skip the rest
            log.exception("[bot] failed to notify %s", user_id)


async def _ensure_disk_room(message, size_mb: float) -> bool:
    """True if there's room for this video; otherwise reply why and say no.

    Tries a pressure cleanup first (it may well free enough), but never
    while a run is in flight — see core/cleanup.py for why that matters.
    """
    need = int(size_mb * 1048576 * 1.15) + config.upload_min_free_mb * 1048576
    if cleanup.disk_free_bytes() >= need:
        return True

    note = ""
    if config.auto_cleanup and not _PIPELINE_LOCK.locked():
        try:
            report = await asyncio.to_thread(
                cleanup.run, apply_changes=True, pressure=True
            )
            if report.deleted:
                note = (
                    f"Sudah dibersihkan {cleanup.fmt_bytes(report.freed_bytes)} "
                    f"({report.deleted} file). "
                )
        except Exception:  # noqa: BLE001
            log.exception("[bot] pressure cleanup before download failed")

    free = cleanup.disk_free_bytes()
    if free >= need:
        return True
    await message.reply_text(
        "💾 Disk gak cukup buat video ini, jadi belum aku download.\n"
        f"{note}Sisa {cleanup.fmt_bytes(free)}, butuh ± {cleanup.fmt_bytes(need)}.\n\n"
        "Cek /cleanup — kalau yang numpuk video belum selesai (ada platform "
        "gagal), retry atau batalkan dulu. Kalau memang kapasitasnya kecil, "
        "pindahkan incoming/ ke USB/microSD (DEPLOYMENT.md → Disk space cleanup)."
    )
    return False


async def handle_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not _allowed(update.effective_user.id):
        return

    if context.args and context.args[0].lower() == "apply":
        # The original `/cleanup apply` contract: delete right away.
        if _PIPELINE_LOCK.locked():
            await update.message.reply_text(
                "⏳ Lagi ada proses posting yang jalan — coba /cleanup apply "
                "lagi sebentar lagi."
            )
            return
        done = await asyncio.to_thread(cleanup.run, apply_changes=True)
        text = (
            f"🧹 {done.deleted} file dihapus, "
            f"{cleanup.fmt_bytes(done.freed_bytes)} bebas. "
            f"Sisa disk {cleanup.fmt_bytes(cleanup.disk_free_bytes())}."
        )
        if done.errors:
            text += "\n⚠️ Gagal hapus: " + "; ".join(done.errors[:3])
        await update.message.reply_text(text)
        return

    report = await asyncio.to_thread(cleanup.assess)
    free = cleanup.fmt_bytes(cleanup.disk_free_bytes())
    lines = [
        f"💾 Sisa disk: {free}" + (" ⚠️ (di bawah ambang)" if report.pressure else "")
    ]
    if report.items:
        lines.append(
            f"🗑 Bisa dihapus sekarang: {len(report.items)} file, "
            f"{cleanup.fmt_bytes(report.total_bytes)}"
        )
        if report.abandoned_videos:
            lines.append(
                f"    ↳ {report.abandoned_videos} di antaranya video yang gak "
                f"pernah dikonfirmasi/dibatalkan "
                f"({cleanup.fmt_bytes(report.abandoned_bytes)}) — sama kayak "
                "❌ Batal, master-nya tetap di Drive."
            )
    else:
        lines.append("🗑 Belum ada file yang bisa dihapus.")
    if report.orphan_files:
        lines.append(
            f"🧩 {report.orphan_files} file ({cleanup.fmt_bytes(report.orphan_bytes)}) "
            "di incoming/ atau posting_copies/ yang gak tercatat di state — "
            "gak dihapus otomatis (gak ada bukti master-nya sudah di Drive). "
            "Cek manual di server."
        )
    if report.soon_videos:
        lines.append(
            f"⏳ {report.soon_videos} video sudah selesai tapi masih dalam masa "
            f"tunggu {config.cleanup_retention_days} hari "
            f"({cleanup.fmt_bytes(report.soon_bytes)})."
        )
    if report.held_back_videos:
        lines.append(
            f"⚠️ {report.held_back_videos} video belum selesai "
            f"({cleanup.fmt_bytes(report.held_back_bytes)}) ditahan: ada platform "
            "yang gagal/belum jalan, filenya masih dibutuhkan buat retry."
        )
    lines.append("\nMaster-nya aman di Google Drive; yang dihapus cuma salinan lokal.")

    buttons = []
    if report.items:
        buttons.append(
            [InlineKeyboardButton("🗑 Hapus sekarang", callback_data="cleanup:apply")]
        )
    if report.soon_videos:
        buttons.append(
            [
                InlineKeyboardButton(
                    "🗑 Hapus juga yang masih masa tunggu",
                    callback_data="cleanup:force",
                )
            ]
        )
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def handle_cleanup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    if update.effective_user is None or not _allowed(update.effective_user.id):
        await query.answer("Belum diizinkan.", show_alert=True)
        return
    await query.answer()
    mode = query.data.partition(":")[2]

    if _PIPELINE_LOCK.locked():
        await query.edit_message_text(
            "⏳ Lagi ada proses posting yang jalan — hapus file sekarang bisa "
            "mengganggunya. Coba /cleanup lagi sebentar lagi."
        )
        return
    # Re-planned from scratch at tap time: the preview above may be minutes
    # old, and a run could have started or finished since.
    report = await asyncio.to_thread(
        cleanup.run, apply_changes=True, pressure=True if mode == "force" else None
    )
    text = (
        f"✅ {report.deleted} file dihapus, "
        f"{cleanup.fmt_bytes(report.freed_bytes)} dibebaskan.\n"
        f"💾 Sisa disk: {cleanup.fmt_bytes(cleanup.disk_free_bytes())}"
    )
    if report.errors:
        text += "\n⚠️ Gagal hapus: " + "; ".join(report.errors[:3])
    await query.edit_message_text(text)


async def housekeeping_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every HEARTBEAT_INTERVAL_MIN: prove we're alive, and relieve disk pressure.

    Runs even with TELEGRAM_ALLOWED_USER_IDS empty — the heartbeat doesn't
    need anyone to message; only the notifications below do.
    """
    heartbeat.beat()
    if config.heartbeat_ping_url:
        await asyncio.to_thread(heartbeat.ping)

    try:
        if not config.auto_cleanup or _PIPELINE_LOCK.locked():
            return
        if not await asyncio.to_thread(cleanup.under_pressure):
            return
        report = await asyncio.to_thread(cleanup.run, apply_changes=True, pressure=True)
        # Only says something when it actually freed space. "Still low and
        # nothing left to delete" is deliberately NOT announced here: the
        # 6-hourly disk_watch_job (DISK_WARN/ERROR_FREE_PCT) already does,
        # and two bots-in-one pinging about the same disk is just noise.
        if report.deleted:
            await _notify(
                context.bot,
                f"🧹 Disk menipis, otomatis dihapus {report.deleted} file "
                f"({cleanup.fmt_bytes(report.freed_bytes)}). "
                f"Sisa disk {cleanup.fmt_bytes(cleanup.disk_free_bytes())}.",
            )
    except Exception:  # noqa: BLE001 — the heartbeat above already went out
        log.exception("[bot] housekeeping failed")


async def threads_token_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily: renew the Threads token well before it lapses (core/threads_token.py).

    Silent unless it fails — a failure is worth a message, because left
    alone every Threads post starts failing once the old token expires.
    """
    if not await asyncio.to_thread(threads_token.needs_refresh):
        return
    try:
        left = await asyncio.to_thread(threads_token.refresh)
    except Exception as exc:  # noqa: BLE001 — refresh() messages are token-free
        log.warning("[bot] Threads token refresh failed: %s", exc)
        await _notify(
            context.bot,
            f"⚠️ Token Threads gagal diperpanjang otomatis ({exc}). Kalau "
            "token lama habis, posting ke Threads bakal gagal — perpanjang "
            "manual (README → Threads). Dicoba lagi besok.",
        )
        return
    log.info(
        "[bot] Threads token renewed (%s)",
        "no expiry given" if left is None else f"{left:.0f} days left",
    )


async def daily_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if _PIPELINE_LOCK.locked():
        log.info("[bot] daily cleanup skipped: a run is in progress")
        return
    try:
        report = await asyncio.to_thread(cleanup.run, apply_changes=True)
    except Exception:  # noqa: BLE001
        log.exception("[bot] daily cleanup failed")
        return
    log.info(
        "[bot] daily cleanup: %d file(s), %s freed",
        report.deleted,
        cleanup.fmt_bytes(report.freed_bytes),
    )
    if report.deleted:
        await _notify(
            context.bot,
            f"🧹 Bersih-bersih harian: {report.deleted} file dihapus, "
            f"{cleanup.fmt_bytes(report.freed_bytes)} dibebaskan. "
            f"Sisa disk {cleanup.fmt_bytes(cleanup.disk_free_bytes())}.",
        )


# --- post performance --------------------------------------------------------

_PLATFORM_SHORT = {"youtube": "YT", "facebook": "FB", "instagram": "IG", "threads": "TH"}


def _fmt_count(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


async def handle_insights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not _allowed(update.effective_user.id):
        return

    prefix = ""
    if context.args and context.args[0].lower() in ("refresh", "update"):
        summary = await asyncio.to_thread(insights.collect)
        prefix = f"🔄 Ambil angka terbaru: {summary.ok} berhasil, {summary.failed} gagal."
        if summary.errors:
            prefix += "\n" + "\n".join(f"• {e}" for e in summary.errors[:4])
        prefix += "\n\n"

    rows = await asyncio.to_thread(insights.ranked, days=30)
    if not rows:
        await update.message.reply_text(
            prefix + "Belum ada data performa.\n\n"
            "Angka diambil otomatis ±24 jam dan ±72 jam setelah posting "
            "(YouTube, Facebook, Instagram, Threads). Butuh YOUTUBE_API_KEY "
            "di .env untuk YouTube, dan token Meta/Threads yang punya izin "
            "insights. `/insights refresh` buat coba ambil sekarang."
        )
        return

    lines = ["📊 Performa 30 hari terakhir (views)"]
    for i, row in enumerate(rows[:5], 1):
        detail = " · ".join(
            f"{_PLATFORM_SHORT.get(p, p)} {_fmt_count(v)}"
            for p, v in sorted(row.per_platform.items(), key=lambda kv: -kv[1])
        )
        hook = (row.hook[:70] + "…") if len(row.hook) > 70 else row.hook
        lines.append(
            f"{i}. {_fmt_count(row.total_views)} — {hook or '(tanpa caption)'}\n"
            f"    {detail}"
        )
    averages = insights.platform_averages(days=30)
    if averages:
        lines.append(
            "\nRata-rata per post: "
            + " · ".join(
                f"{_PLATFORM_SHORT.get(p, p)} {_fmt_count(avg)} ({n})"
                for p, (n, avg) in sorted(averages.items(), key=lambda kv: -kv[1][1])
            )
        )
    hooks_note = (
        f"\n\nCaption berikutnya memakai {config.caption_top_hooks} hook terbaik "
        "sebagai referensi gaya."
        if insights.top_hooks()
        else f"\n\nHook terbaik baru dipakai Gemini setelah ≥{config.insights_min_samples} "
        f"post punya data (sekarang {len(rows)})."
    )
    await update.message.reply_text(prefix + "\n".join(lines) + hooks_note)


async def insights_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.to_thread(insights.collect)
    except Exception:  # noqa: BLE001
        log.exception("[bot] insights collection failed")


_BOT_COMMANDS = [
    BotCommand("status", "Cek status integrasi (Gemini, Drive, YouTube, Meta, Threads)"),
    BotCommand("dashboard", "Link ke admin dashboard (edit token, kontrol bot)"),
    BotCommand("cleanup", "Cek & bersihkan disk (tambah 'apply' buat langsung hapus)"),
    BotCommand("jadwal", "Daftar posting terjadwal"),
    BotCommand("insights", "Performa post (views per platform)"),
]


async def _announce_startup(app: Application) -> None:
    """Say the bot is up — and, if it wasn't for a while, for how long.

    This is the cheap half of noticing outages: the box was once dark for
    ~19 days with nobody the wiser. Everything that *runs on* the box can't
    report its own absence, but the first thing it can do when it returns is
    say so, with the gap read back from the last heartbeat on disk.
    """
    previous_age = heartbeat.age_seconds()
    heartbeat.beat()

    gap_threshold = max(10 * 60, 3 * config.heartbeat_interval_min * 60)
    if previous_age is None:
        head = "🟢 Bot MentahanPOV nyala (belum ada catatan hidup sebelumnya)."
    elif previous_age > gap_threshold:
        last = heartbeat.last_beat()
        when = (
            last.astimezone(_REPORT_TZ).strftime("%d %b %H:%M WIB") if last else "?"
        )
        head = (
            f"🟢 Bot online lagi. Sempat mati ± {heartbeat.humanize(previous_age)} "
            f"(terakhir hidup {when})."
        )
    else:
        head = "🟢 Bot restart (downtime singkat)."

    lines = [head, f"💾 Sisa disk {cleanup.fmt_bytes(cleanup.disk_free_bytes())}"]
    pending = len(pipeline_state.scheduled_entries(config.state_file))
    if pending:
        lines.append(f"📅 {pending} posting terjadwal dilanjutkan (cek /jadwal).")
    await _notify(app.bot, "\n".join(lines))


async def _post_init(app: Application) -> None:
    """Registers the / command menu Telegram shows in the chat's own UI
    (the "/" or menu-icon picker) — without this, /status and /dashboard
    still work, they just aren't listed anywhere; you'd have to already
    know to type them. Safe to call on every startup: Telegram just
    overwrites the list with the same values."""
    await app.bot.set_my_commands(_BOT_COMMANDS)
    try:
        await _announce_startup(app)
    except Exception:  # noqa: BLE001 — never block startup on a courtesy message
        log.exception("[bot] startup announcement failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO and Telegram's contains the bot
    # token — without this it lands in `docker logs` on every poll.
    logsafe.quiet_http_loggers()
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
        # before that and the video is silently dropped.
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
    app.add_handler(CommandHandler("cleanup", handle_cleanup))
    app.add_handler(CommandHandler("jadwal", handle_jadwal))
    app.add_handler(CommandHandler("insights", handle_insights))
    app.add_handler(
        CallbackQueryHandler(
            handle_confirm_callback, pattern=r"^(post|cancel|edit|retry|toggle|stop):"
        )
    )
    app.add_handler(
        CallbackQueryHandler(handle_schedule_callback, pattern=r"^(sched|schedx):")
    )
    app.add_handler(CallbackQueryHandler(handle_cleanup_callback, pattern=r"^cleanup:"))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )
    # Catches the caption text typed after "✏️ Edit caption"; falls through
    # to handle_other itself when no edit is pending for that chat.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_reply))
    app.add_handler(MessageHandler(filters.ALL, handle_other))
    app.add_error_handler(handle_error)

    if config.telegram_allowed_user_ids:
        app.job_queue.run_repeating(disk_watch_job, interval=6 * 3600, first=300)
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

    # Registered regardless of the allowlist above: the heartbeat/housekeeping
    # tick doesn't need anyone to message — only its notifications do, and
    # _notify just has no recipients then.
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            housekeeping_job,
            interval=datetime.timedelta(minutes=max(1, config.heartbeat_interval_min)),
            first=10,
            name="housekeeping",
        )
        if config.auto_cleanup:
            app.job_queue.run_daily(
                daily_cleanup_job,
                time=datetime.time(hour=config.cleanup_hour, tzinfo=_REPORT_TZ),
            )
            log.info(
                "[bot] daily cleanup scheduled for %02d:00 WIB", config.cleanup_hour
            )
        if config.insights_enabled:
            app.job_queue.run_repeating(insights_job, interval=3600, first=300)
        if getattr(config, "threads_user_id", "") and getattr(
            config, "threads_access_token", ""
        ):
            # Interval, not a fixed hour: this box is off for days at a
            # time, and a run_daily slot it happens to miss never catches up.
            app.job_queue.run_repeating(
                threads_token_job, interval=24 * 3600, first=600, name="threads_token"
            )
        restored = _restore_schedules(app)
        if restored:
            log.info("[bot] restored %d scheduled post(s)", restored)
    else:
        log.error(
            "[bot] no job queue (python-telegram-bot[job-queue] missing?) — "
            "heartbeat, auto-cleanup, insights and scheduled posts are disabled"
        )

    log.info("[bot] MentahanPOV bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
