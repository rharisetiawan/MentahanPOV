# TikTok (archived 2026-10-21)

TikTok posting is parked here, not deleted. The automated hop (HG680 ->
campus worker -> Playwright on TikTok Studio) kept timing out, and it had
already been switched off with `TIKTOK_ENABLED=false` since early October.
Archiving it also drops Playwright from the bot image (`requirements.txt`).

## What's here (same relative paths as before)

| File | Was |
|---|---|
| `distributors/tiktok.py` | local Playwright uploader (+ `login`) |
| `distributors/tiktok_remote.py` | HTTP client to the campus worker |
| `worker_service.py` | the campus worker (fails closed without a shared secret) |
| `requirements-worker.txt` | worker deps (Playwright) |
| `core/debug_artifacts.py` | parks a failure screenshot for the bot to send |
| `tests/` | the tests for the above |

Removed from the live code at the same time (look at the archiving commit's
diff to get them back):

- `main.py`: the `"tiktok"` registry entry, `MAIN_THREAD_ONLY_PLATFORMS`,
  `_park_failure_screenshot`, the TikTok config-check.
- `telegram_bot.py`: the `[tiktok]` progress line, `_send_tiktok_debug`
  (failure screenshot) and `_send_tiktok_kit` (watermarked file + caption
  for posting by hand), plus the `debug_artifacts.prune()` calls.
- `config.py`: `TIKTOK_*` settings; `tiktok` is now in `ARCHIVED_PLATFORMS`,
  which silently drops it from `PLATFORMS`/`TELEGRAM_PLATFORMS`.
- `.env.example`: the TikTok sections. Old `TIKTOK_*` lines in a real `.env`
  are harmless (the admin dashboard keeps them under "Custom").

## Bringing it back

1. `git mv` each file above back to its old path.
2. Revert the wiring with `git show b33b700 -- main.py telegram_bot.py config.py .env.example requirements.txt`
   as a guide (or `git revert` that commit if nothing else changed there).
3. Put `playwright` back in `requirements.txt` if the bot box should run it
   locally; the campus worker only needs `requirements-worker.txt`.
4. Set `TIKTOK_WORKER_SHARED_SECRET` on both boxes (required now).
