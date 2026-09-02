# Deployment (production: HG680P / Armbian)

The Telegram bot runs unattended via Docker Compose on an HG680P TV box
reflashed to Armbian — not from a bare `python telegram_bot.py` like the
README's quick-start describes. This doc is the map for that specific
deployment: where things live, how to update them, and what's already
been debugged so it doesn't need re-debugging.

## Layout on the box

```
/www/apps/mentahanpov/     # this repo, checked out on devin/1777731695-init-mentahanpov
├── .env                   # real credentials — never in git, see README setup guide
├── Dockerfile
├── docker-compose.yml
└── .dockerignore
```

Two services:

- **`telegram-bot-api`** — Telegram's official Local Bot API Server. Lets
  the bot receive files over Telegram's 20MB `api.telegram.org` limit (see
  README → "Telegram bot" → "Removing the 20MB download cap").
- **`mentahanpov-bot`** — this repo, built from the local `Dockerfile`,
  running `telegram_bot.py`. `credentials/`, `state/`, and `incoming/` are
  bind-mounted from the host rather than baked into the image, so they
  survive rebuilds and don't bloat the build context (see `.dockerignore`).

## Deploying an update

```bash
cd /www/apps/mentahanpov
git pull origin devin/1777731695-init-mentahanpov
docker compose build mentahanpov-bot   # only rebuilds if code/deps changed
docker compose up -d                   # recreates the container with the new image + current .env
```

`docker compose up -d` is what actually applies `.env` changes too —
`env_file` is read at container *creation*, not hot-reloaded, so editing
`.env` alone does nothing until this runs.

If a build is started over SSH, run it detached
(`nohup docker compose build mentahanpov-bot > build.log 2>&1 &`) — home
network SSH sessions here disconnect often enough that a foreground build
gets killed mid-way more often than not.

## Incident log

### 2026-08-19 — IPv6 breaking Gemini/Drive/Graph API calls intermittently

**Symptom:** `httpx.ConnectError: [Errno 101] Network is unreachable`
during Gemini calls (also possible on any other outbound HTTPS call —
Gemini's was just the one that got hit). Failed 3x through `tenacity`
retries and still errored out, so it wasn't a single blip.

**Root cause:** this box's IPv6 route is broken — it has IPv6 addresses
configured (`getaddrinfo` returns real AAAA records for e.g.
`generativelanguage.googleapis.com`) but no actual working route to the
internet over it. Confirmed by directly connecting a raw socket to a
resolved IPv6 address from inside the container: `Network is unreachable`,
100% of the time. The equivalent IPv4 connect worked every time. Since
httpx doesn't consistently prefer one address family over the other, the
failure rate looked intermittent even though the underlying cause was
constant.

**Fix:** `sysctls: [net.ipv6.conf.all.disable_ipv6=1]` on the
`mentahanpov-bot` service in `docker-compose.yml` — disables IPv6 inside
just that container's network namespace, so every connection attempt goes
straight to IPv4 instead of sometimes trying (and failing) IPv6 first.
Verified after the fix: the same raw-socket IPv6 connect now fails
immediately with `Cannot assign requested address` (no IPv6 stack at all)
instead of hanging/timing out on a route that doesn't exist — a cheaper,
faster failure that never gets picked over the working IPv4 path.

**If this resurfaces on a different box:** check with a raw socket
connect first (`socket.socket(socket.AF_INET6, ...).connect((ipv6_addr, 443))`),
not just `curl -6`/`curl -4` from the shell — the container's network
namespace can differ from the host's, and the bot's actual failures happen
inside the container, not on the host directly.

### 2026-08-19 — Gemini `503 UNAVAILABLE` ("high demand")

Separate from the above — this is Google's Gemini API reporting the model
itself is over capacity, not a network problem. Recurred twice in one day.
Fixed in `core/gemini.py` by splitting the file upload from the
generate-content call so only the latter retries on failure (no point
re-uploading a multi-hundred-MB video that already succeeded), and giving
that retry more patience — 6 attempts over roughly 2-3 minutes instead of
3 attempts over ~30s. See the commit for `core/gemini.py` for specifics.

### 2026-08-19/20 — Gemini `429 Too Many Requests` + a retired fallback model

The 503 fix above wasn't the end of it: the *next* run hit `429 Too Many
Requests` instead — a quota/rate-limit error, not a capacity one, and a
strong sign the Gemini API key is on the free tier (Google cut
gemini-2.5-flash's free daily quota from ~250 to ~20 requests in late
2025). Added `GEMINI_FALLBACK_MODEL`, tried automatically once the
primary model exhausts its own retries.

The first fallback picked (`gemini-2.0-flash`) turned out to be **retired**
— 404 "no longer available". Worse, `gemini-2.5-flash-lite` also 404s
despite still showing up in `client.models.list()` — that endpoint lists
models "existing users" can still technically reach, not what's actually
live for this API key. Replaced with `gemini-3.5-flash-lite`, confirmed
by an actual `generate_content` call (text, then real video
upload+generate) before trusting it — see the comment above
`gemini_fallback_model` in `config.py` for the exact verification
commands to run next time a model name needs replacing. Don't pick a
replacement from memory or docs; both can be stale.

Real end-to-end proof this was fixed: ran `core.gemini.generate_metadata`
directly against the actual video that had been failing, inside the live
container. It hit six 429s on `gemini-2.5-flash`, fell back to
`gemini-3.5-flash-lite`, and succeeded on the first attempt — 318.7s
total, real caption/filename/folder returned.

**Underlying, not-fully-fixed risk:** Google retires Gemini model names
with essentially no notice, and `models.list()` can't be trusted to only
list what's actually callable. `core/health.py`'s Gemini checks now make
a real (cheap) `generate_content` call against both `GEMINI_MODEL` and
`GEMINI_FALLBACK_MODEL` daily — see "Status monitoring" below — so a
retirement surfaces as a same-day alert instead of a failed run days
later.

### 2026-08-20 — Google OAuth (`invalid_grant: Token has been expired or revoked`)

`gdrive-token.json` (and, it turned out, `youtube-token.json` too — same
underlying cause) stopped refreshing. Root cause: the Google Cloud OAuth
consent screen backing `credentials/youtube-oauth.json` is in **Testing**
publishing status, not Production. Google expires refresh tokens issued
to Testing-mode apps after a fixed ~7 days *from the original grant* —
this does **not** reset just because the token gets successfully
refreshed for its access-token in between, so "it worked yesterday" is no
guarantee. Expect this to recur roughly weekly unless the app is moved to
Production (which, for the `drive` scope, likely needs Google's
verification process — not attempted yet).

**Fix (temporary, repeats every ~7 days):** re-run the OAuth consent flow
and drop the fresh token files in — see "Re-authenticating Google OAuth"
below. Both `gdrive-token.json` and `youtube-token.json` come from the
same OAuth client (`credentials/youtube-oauth.json`), so redo both at the
same time even if only one has started failing yet.

**Verified, not just assumed fixed:** after generating fresh tokens,
called `gdrive.resolve_category_folder_id(...)` and `youtube._get_creds()`
for real against the live account inside the running container, rather
than trusting that "the OAuth flow completed without an error" was
sufficient proof.

## Re-authenticating Google OAuth

Needed whenever `core/gdrive.py` or `distributors/youtube.py` raises
`google.auth.exceptions.RefreshError` (`invalid_grant`), or `/status` /
the dashboard flags Google Drive or YouTube as broken. The OAuth
consent flow (`InstalledAppFlow.run_local_server()`) opens a local HTTP
server and needs a real browser to hit it — the HG680 is headless, so do
this from a machine that has both Python and a browser (this project's
dev laptop), then copy the resulting token files over. Trying to run the
flow directly on the HG680 would mean port-forwarding a browser on
another device back to it, which is unnecessary extra work when the
token files themselves aren't tied to any particular machine.

1. Grab the shared client secrets file from the box (same file backs both
   Drive and YouTube tokens):
   ```bash
   scp root@<box-ip>:/www/apps/mentahanpov/credentials/youtube-oauth.json ./credentials/
   ```
2. On the dev machine, with this repo's deps installed, run the OAuth
   flow for each scope that needs refreshing (drive, youtube, or both):
   ```python
   from pathlib import Path
   from google_auth_oauthlib.flow import InstalledAppFlow

   SECRETS = Path("credentials/youtube-oauth.json")
   for name, scopes, out in [
       ("drive", ["https://www.googleapis.com/auth/drive"], Path("credentials/gdrive-token.json")),
       ("youtube", ["https://www.googleapis.com/auth/youtube.upload"], Path("credentials/youtube-token.json")),
   ]:
       flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS), scopes)
       creds = flow.run_local_server(port=0, prompt="select_account", open_browser=False)
       out.write_text(creds.to_json(), encoding="utf-8")
   ```
3. Each call prints a `https://accounts.google.com/o/oauth2/auth?...` URL
   — open it in a real browser, **log in as the account that actually owns
   the Drive folder and YouTube channel** (not just whichever Google
   account happens to be signed in), and click Allow. The page will land
   on a bare `localhost` URL after — that's expected, the script already
   has what it needs.
4. Copy the fresh token files back to the box and confirm they're
   world-readable by the container's user, then verify for real (bind
   mounts mean no restart is needed — the container reads the file fresh
   on its next call):
   ```bash
   scp credentials/gdrive-token.json credentials/youtube-token.json root@<box-ip>:/www/apps/mentahanpov/credentials/
   ssh root@<box-ip> 'docker exec mentahanpov-mentahanpov-bot-1 python -c "
   from core import gdrive
   print(gdrive.resolve_category_folder_id(\"01 - Suasana Jalan & Perjalanan\"))
   "'
   ```
   A folder ID back (not a traceback) means it worked.

## Status monitoring

Added 2026-08-20, directly in response to the incidents above repeatedly
only surfacing as a failed run instead of something checkable ahead of
time. All three surfaces below share `core/health.py` — one place that
knows what "healthy" means per integration, so they can't drift out of
sync with each other.

- **Telegram `/status`** — on-demand check of Google Drive, YouTube,
  Facebook/Instagram, Threads, both configured Gemini models, and disk
  space. Works whether or not `TELEGRAM_ALLOWED_USER_IDS` is set.
- **Daily Telegram report** — the same check, sent automatically every
  day at `DAILY_STATUS_HOUR` (default 8 WIB) to everyone in
  `TELEGRAM_ALLOWED_USER_IDS`. Skipped if that list is empty — there's
  nowhere to send it.
- **`dashboard.py`** (docker-compose service `dashboard`) — the same
  check as a web page at `http://<box-ip>:${DASHBOARD_PORT:-8090}`,
  password-protected (`DASHBOARD_USER`/`DASHBOARD_PASSWORD`). LAN-only by
  design — not port-forwarded to the internet, since a home-network
  threat model doesn't need that and it avoids having to reason about
  brute-force protection on a Basic Auth page over plain HTTP.

None of the three ever display actual token/credential values — only
ok/warning/error plus a short reason — so exposing the dashboard on the
LAN carries no more risk than exposing "which service is down" would.

Google Drive/YouTube checks can only report "works right now" vs
"broken right now" — Google doesn't expose a days-until-expiry API for
Testing-mode refresh tokens, so there's no way to give those the same
"expires in N days" countdown Facebook/Instagram/Threads get (Meta's
Graph API `debug_token` endpoint actually reports real expiry). A daily
check still catches a dead Google token within 24 hours instead of only
whenever the next real video happens to be sent — a real improvement,
just not a precise warning window.

## TikTok remote worker

Added 2026-09-02. The HG680P is an ARM board with limited RAM — it can't
run Playwright/Chromium (the only working TikTok posting method; TikTok's
official Content Posting API needs an app-review approval that can take
weeks). Rather than skip TikTok entirely, posting is delegated to a
second, more capable machine (the campus server, x86, more RAM) over a
channel both boxes can already reach even though they're never on the
same network and neither exposes a port to the internet: the Telegram
Bot API.

**Why not full mirroring / full auto-failover.** Two options were
considered and rejected in favor of this "middle ground":
1. Running the entire pipeline twice (once per box) — doubles Google
   Drive/YouTube/Gemini API usage and posting-account risk for no
   benefit, since only the TikTok step actually needs the second
   machine.
2. Automatic failover (campus server takes over the *whole* pipeline if
   the HG680 goes down) — real value, but meaningfully more complex
   (health checks between boxes, split-brain avoidance, state
   synchronization) for a problem that hasn't actually happened yet.

Splitting off just the TikTok step keeps each box doing what it's
actually suited for, with no new failure modes for the four platforms
that already work fine on the HG680.

**Architecture.**

```
HG680 (main.py)                Telegram                Campus server
──────────────────         "MentahanPOV Jobs"          ───────────────
distributors/                  group chat                worker_service.py
tiktok_remote.py  ──sendMessage──▶  (both bots  ◀──getUpdates── (polls with
  sends:                            are members,          TIKTOK_WORKER_
  "TIKTOK_JOB <id>                  Group Privacy         BOT_TOKEN)
   <video_url>                      OFF for both)
   <caption>"                                             on TIKTOK_JOB:
  via main bot's                                          downloads video,
  TELEGRAM_BOT_TOKEN                                      runs distributors/
                                                           tiktok.py Playwright
telegram_bot.py's                                         flow UNCHANGED,
handle_tiktok_job_                ◀──sendMessage───────── replies
result() writes the                "TIKTOK_DONE <id> <url>"
reply to                           or "TIKTOK_FAILED <id> <err>"
state/tiktok_jobs/<id>.json        via worker bot's own token

tiktok_remote.py polls that
file (not Telegram) until it
appears or TIKTOK_REMOTE_
TIMEOUT_S elapses
```

Two separate bot tokens, not one, because a single Telegram bot token can
only be long-polled (`getUpdates`) by one process at a time — the HG680's
`telegram_bot.py` already owns that stream for the main bot, so the
worker needs its own bot to poll independently. `tiktok_remote.py` itself
never polls anything; it only ever calls `sendMessage` and then watches a
local file, so it can't collide with `telegram_bot.py`'s own poller
either.

`main.py` picks the local-Playwright path or this remote one for
`PLATFORM_REGISTRY["tiktok"]` automatically, based on whether
`TIKTOK_WORKER_GROUP_CHAT_ID` is set — no separate flag to remember.

**One-time setup.**

1. Create a second bot via @BotFather (`/newbot`) — this project used
   `@MentahanPOV_TikTok_bot`. Save its token as `TIKTOK_WORKER_BOT_TOKEN`.
2. Create a Telegram group (any name — this project used "MentahanPOV
   Jobs") and add BOTH the main bot and the new worker bot to it.
3. For the worker bot specifically: BotFather -> `/mybots` -> select the
   worker bot -> Bot Settings -> Group Privacy -> **Turn off**. Without
   this, the bot only receives messages that literally mention it, and
   would never see the main bot's `TIKTOK_JOB` messages.
4. Find the group's numeric `chat_id`: send any message in the group,
   then `curl "https://api.telegram.org/bot<WORKER_TOKEN>/getUpdates"`
   and read `message.chat.id` (negative number for groups). Set as
   `TIKTOK_WORKER_GROUP_CHAT_ID` in `.env` on **both** boxes.
5. On the campus server: clone this repo, then
   ```bash
   pip install -r requirements-worker.txt
   playwright install --with-deps chromium
   python -m distributors.tiktok login   # headed browser, log in once
   ```
   The resulting `credentials/tiktok-storage.json` only needs to exist
   on the campus server — the HG680 never touches Playwright at all once
   this is set up.
6. Run `python worker_service.py` continuously on the campus server
   (systemd unit, `tmux`/`screen`, or a Docker container — pick whatever
   matches how the rest of that box's services are already run).
7. Add `tiktok` to `PLATFORMS` (or `TELEGRAM_PLATFORMS`) on the HG680
   once the above is confirmed working end-to-end with a real video.

**Recovering a stuck job.** If `tiktok_remote.post()` times out
(`TIKTOK_REMOTE_TIMEOUT_S`, default 600s), the worker may still be
mid-upload — check the "MentahanPOV Jobs" group for a late
`TIKTOK_DONE`/`TIKTOK_FAILED` reply, or `worker_service.py`'s own log
output on the campus server, before assuming it's lost.
