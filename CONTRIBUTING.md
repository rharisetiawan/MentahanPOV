# Contributing to MentahanPOV

This is a personal content pipeline, not a public framework — but it's grown
past the size where "just read main.py" is enough to get oriented. This doc
is the map. Read it before touching `main.py` or `distributors/`.

## How a video moves through the pipeline

```
video file ─► ffprobe facts ─► reverse geocode ─► Gemini SOP V3 (JSON) ─► GDrive upload (untouched master)
                                                                          └► watermark render (posting copy)
                                                                             └► story trim (if any *_story platform requested)
                                                                                └► fan-out to PLATFORM_REGISTRY ─► state log
```

`main.py` is a linear sequence of `step_*` functions, each one wrapping a
`core/` module and persisting its result to `state/posts.json` via
`core/state.py` before moving on. That persistence is what makes reruns
idempotent — `--skip-gdrive`, `--skip-gemini`, `--skip-watermark` reuse a
prior step's output instead of redoing it, and `step_distribute` skips any
platform already marked `"status": "ok"` unless you pass `--force`.

`telegram_bot.py` doesn't reimplement any of this — it just runs
`python main.py <video>` as a subprocess and streams the log output back
into a chat message. If you're changing pipeline behavior, change `main.py`
or the module it delegates to; the bot only needs updates for
bot-specific concerns (timeouts, status messages, Telegram file handling).

## Module map

| Module | Owns |
|---|---|
| `config.py` | Every environment variable, typed, with a sane default. Nothing else should call `os.getenv` directly. |
| `core/video_facts.py` | ffprobe: date, duration, resolution, GPS from the file itself. |
| `core/geocode.py` | GPS → street address (Nominatim). |
| `core/gemini.py` | Caption/filename/folder generation (strict JSON contract). |
| `core/gdrive.py` | Master upload, category folder resolution, temp-file staging for Meta's URL-fetch requirement. |
| `core/google_auth.py` | The one OAuth2 installed-app flow shared by `gdrive.py` and `distributors/youtube.py`. |
| `core/watermark.py` | Master → watermarked posting copy, and posting copy → trimmed story copy. |
| `core/state.py` | The only code that reads/writes `state/posts.json`. Atomic writes (`.tmp` + `replace`). |
| `core/cleanup.py` | What's safe to delete from `incoming/` + `state/posting_copies/`, and nothing else. One definition shared by the bot (after every run / daily / on disk pressure / `/cleanup`) and `scripts/cleanup_storage.py`. `plan()`/`apply()` are the original contract that `scripts/mentahan.sh` (`mentahan cleanup`) calls on the box — keep their signatures; the richer engine is `assess()`/`execute()`/`run()` (pinned by `tests/test_cleanup_legacy_api.py`). Read its docstring before changing a rule — a mistake here deletes footage. |
| `core/heartbeat.py` | `state/heartbeat.json` liveness file (startup "was down for X" message, dashboard `/heartbeat`, optional dead-man's-switch ping). |
| `core/insights.py` | Per-platform views/likes ~24h and ~72h after posting, ranking, and the top-hooks handed to `core/gemini.py`. |
| `core/schedule.py` | Pure time parsing/formatting for the 📅 Jadwalkan button (no Telegram imports, so it's unit-testable). |
| `scripts/` | Operator tools, not imported by the pipeline: `cleanup_storage.py`, `reauth_google.py` (laptop-side Google re-consent), `watchdog.py` (run from a *different* machine; stdlib-only on purpose). |
| `distributors/*.py` | One file per publish target — see below. |
| `distributors/meta_graph.py` | Graph API URL-building + Instagram container polling, shared by every FB/IG distributor. |
| `admin/` | Read-write admin dashboard (`python admin.py`) for `.env`/credential editing and `docker compose`-driven bot/run control — see DEPLOYMENT.md → "Admin dashboard". `admin/env_schema.py` parses `.env.example` at request time instead of hardcoding field names, so the form always matches whatever's actually in `.env.example`. Runs as a separate host process from `dashboard.py` (which stays read-only and containerized) — it needs write access and `docker compose`, neither of which that sandboxed container has on purpose. |

## The distributor contract

Every file in `distributors/` exposes:

```python
def post(
    video_path: Path,
    caption: str,
    *,
    gdrive_url: str | None = None,
    post_url: str | None = None,
    story_url: str | None = None,
    **_: Any,
) -> dict[str, str]:  # {"id": ..., "url": ...}
```

`main.py` calls every registered platform with the *same* kwargs — `**_`
lets each distributor ignore whatever it doesn't need. This is deliberate:
adding a new shared parameter later (e.g. a second account's token) never
requires touching every existing distributor's signature.

### Adding a new platform (or a new destination on an existing one)

1. Write `distributors/<name>.py` with a `post()` matching the contract above.
   - Uploading bytes directly? Take `video_path` and post the file (see `facebook.py`).
   - Platform fetches the video itself from a URL (like Meta's does)? Take `post_url` (feed) or `story_url` (story) instead — see `instagram.py` / `instagram_story.py`'s docstrings for why the master is never handed over directly.
   - Talking to the Graph API? Use `distributors/meta_graph.py`'s `graph_url()` / `wait_container_finished()` rather than rebuilding that plumbing.
2. Register it: `PLATFORM_REGISTRY["<name>"] = <name>.post` in `main.py`.
3. If it needs a public URL (step 1's second case), add `"<name>"` to `NEEDS_POST_URL` or `NEEDS_STORY_URL` in `main.py` — `step_publish_urls` will stage a temporary Drive link automatically and clean it up after the run (`step_cleanup`), success or failure.
4. If it's a 24h-expiry Story-type post rather than a permanent feed post, add `"<name>"` to `STORY_PLATFORMS` so `step_distribute` hands it the trimmed story copy (`STORY_MAX_S` in `core/watermark.py`) instead of the full feed copy.
5. Add `"<name>"` to the `PLATFORMS` default / comment in `.env.example` and the platform table in `README.md`.

### A second account on the same platform

The current `config.py` holds exactly one credential set per platform
(`FB_PAGE_ACCESS_TOKEN`, `IG_USER_ID`, etc.) — there's no per-call account
selection yet. The straightforward way to add one without a bigger refactor:
give the new distributor module its own env vars (e.g.
`IG_PERSONAL_USER_ID`, `IG_PERSONAL_ACCESS_TOKEN`) and its own `post()`,
registered under a distinct platform name (e.g. `instagram_story_personal`).
Don't overload the existing `instagram_story.post()` with an account
parameter — the registry pattern already handles "same shape, different
target" via separate names, which is easier to reason about than a
branching implementation.

Note Meta's Graph API only ever authorizes **Business or Creator**
accounts — a true personal profile has no publishing API at all, automated
or otherwise.

## Error handling philosophy

- Every distributor call is wrapped in `tenacity.retry` (exponential
  backoff, 2-3 attempts) — put it on the network call, not the whole function.
- `step_distribute` catches `Exception` per platform. One platform failing
  must never take down the others in the same run.
- Every failure is written to the state file with its type + message so the
  *next* run can retry just that platform — don't swallow exceptions
  silently, and don't add your own retry loop outside `tenacity`.
- `noqa: BLE001` on a broad `except Exception` is used deliberately in a few
  places (cleanup code, error-reporting code) where the alternative is
  crashing the process over a problem in the code whose job is to *report*
  problems. Keep that pattern narrow — it's not a license to swallow errors
  generally.

## Code style notes

- Comments in this codebase explain **why**, not what — a fixed bug, a
  non-obvious API quirk, a constraint that isn't visible from the code
  alone. If you can't explain why a comment needs to exist beyond
  restating the next line, cut it.
- `from __future__ import annotations` + type hints throughout; keep new
  code consistent.
- All configuration goes through `config.py`'s `Config` dataclass, backed
  by `.env`. Don't read `os.environ` anywhere else. (One deliberate
  exception: `scripts/watchdog.py` reads its own `WATCHDOG_*` variables,
  because it runs on a different machine without this repo's
  dependencies and must stay standard-library only.)
- Tests live in `tests/` and use only `unittest` (no new dependency):
  `python -m unittest discover -s tests -t .`. They cover the parts where
  a bug is both silent and expensive — what cleanup may delete
  (`test_cleanup.py`), schedule parsing and the bot's schedule/disk-guard
  flow driven with fake Telegram objects, insights, and the watchdog against a local HTTP server. They need the
  project's `requirements.txt` installed (the bot tests import
  python-telegram-bot). Distributors still have **no** tests — each talks
  to a real, stateful external API (posting a real video), so `--dry-run`
  and the `--skip-*` flags are the practical way to exercise a change
  without reposting; mocking `requests`/the Google clients at the
  distributor boundary is the natural seam if you add them.
- When a test patches `config`, patch the name *in the module under test*
  (`mock.patch.object(cleanup, "config", ...)`) — `Config` is a frozen
  dataclass, so setting attributes on the shared instance raises.

## Where this actually runs

The Telegram bot (`telegram_bot.py`) is designed to run unattended on a
small always-on ARM box (originally an HG680P TV box reflashed as a Linux
server) — see the comment on `PIPELINE_TIMEOUT_S` for the measured
end-to-end timing that number is based on. If you're tuning timeouts or
adding CPU/memory-heavy steps (a second ffmpeg pass, a bigger model),
budget for that class of hardware, not a dev laptop.
