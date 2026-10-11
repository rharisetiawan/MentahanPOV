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
2. On the dev machine, with this repo's deps installed, run the helper
   (it replaces the hand-pasted snippet this step used to be — same flow,
   fewer ways to trip):
   ```bash
   python scripts/reauth_google.py            # Drive, then YouTube
   python scripts/reauth_google.py --only drive
   ```
   What it fixes over the old snippet: it redirects to `127.0.0.1`
   instead of `localhost` (on Windows `localhost` often resolves to IPv6
   and the redirect died with `ERR_CONNECTION_REFUSED` *after* you'd
   clicked Allow, with no second chance); it keeps going to the second
   service if the first is cancelled; and it records when consent was
   granted, which is what lets `/status` count down to expiry (below).
3. For each service a browser window opens (or a
   `https://accounts.google.com/o/oauth2/auth?...` URL is printed with
   `--no-browser`) — **log in as the account that actually owns the Drive
   folder and YouTube channel** (not just whichever Google account happens
   to be signed in; the two are usually different accounts) and click
   Allow. Pick the right account on the first try: the local listener
   serves exactly one request, so cancelling or choosing wrong ends that
   attempt and you simply re-run the script.
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

### Why it keeps recurring, and what to do about it

The weekly death is Google's Testing-mode rule, not a bug here: a refresh
token issued to an app whose OAuth consent screen is in **Testing**
publishing status expires 7 days after consent, used or not. Two things
are in place now:

1. **A countdown instead of a surprise.** `scripts/reauth_google.py`
   writes `granted_at` next to each token, and `/status` + the dashboard
   use it: "Token is 2.1 days old (~4.9 days left if the app is still in
   Testing)", turning into a ⚠️ warning with ~1.5 days left. Tokens
   created before this existed show no countdown until their next
   re-auth. `GOOGLE_TOKEN_MAX_AGE_DAYS` (default 7) sets the window.
2. **The actual fix is a console action only you can do:** Google Cloud
   Console → APIs & Services → OAuth consent screen (the page may be
   labelled "Audience") → **Publish app**, then run
   `scripts/reauth_google.py` once more so the new token is issued under
   Production rules, and set `GOOGLE_TOKEN_MAX_AGE_DAYS=0`. Per Google's
   docs, Production refresh tokens don't carry the fixed 7-day expiry.
   Caveats, so they aren't discovered at 11pm: the Drive scope this
   pipeline uses (`.../auth/drive`) is a *restricted* scope, so Google may
   show an "unverified app" warning on consent (acceptable — you are the
   only user: Advanced → Go to app) and may ask for verification for it;
   whether publishing alone is enough for restricted scopes without
   verification was **not confirmed** (sources disagree), so treat the
   first week after publishing as the test. If Google insists on
   verification, fall back to the countdown above.

Narrowing the scope to `drive.file` would avoid the restricted-scope
question, but doesn't work here: `core/gdrive.py` looks up the category
folders you created by hand under `GDRIVE_FOLDER_ID`, and `drive.file`
only sees files this app itself created. It would require the app to own
the whole archive tree, i.e. a migration of the folder structure.

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

Google Drive/YouTube checks can't ask Google for a days-until-expiry
(there's no such API for Testing-mode refresh tokens, unlike Meta's
`debug_token`). Instead they compute one: the token dies a fixed number
of days after consent and `scripts/reauth_google.py` records when that
was — see "Why it keeps recurring" above. Tokens without a recorded date
fall back to "works right now" vs "broken right now", which a daily check
still catches within 24 hours.

The Disk space line now also shows what's using the disk and how much
cleanup could reclaim (see "Disk space cleanup").

## Disk space cleanup

Added 2026-10-01 — `incoming/` (master videos downloaded by the Telegram
bot) and `state/posting_copies/` (watermarked copies rendered by
`core/watermark.py`) only ever grow. Nothing in `main.py` or
`telegram_bot.py` deletes either one; `core/health.py`'s `check_disk_space`
has warned about this since it was written, but on the HG680's small
eMMC it's the quiet way a run eventually fails with `ENOSPC`.

`core/cleanup.py` is the fix. It deletes a video's local master +
watermarked copy once every platform in its `state/posts.json` entry
succeeded (so the master's Drive backup from `step_gdrive` is the only
remaining copy, by design) and its `updated_at` is older than
`CLEANUP_RETENTION_DAYS` (default 3). It never touches:

- a video with any platform still `"error"` or not yet recorded (the
  files are what a retry uses — the bot lists these as "unfinished");
- a video with a pending scheduled post;
- a video without a `gdrive_url` (no Drive backup yet);
- anything outside `incoming/` and the posting-copy dir — a video you
  ran by hand straight from your own footage folder is *your original*,
  and is never deleted whatever path the state recorded.

Two more things are deleted that the finished-video rule alone would
have left growing forever:

- **Videos nobody confirmed — opt-in, off by default.** The bot's first
  step uploads the master to Drive (`--dry-run`) and asks "Post /
  Batal?". If neither is ever tapped, the local copy sits in
  `incoming/` for good. With `ABANDONED_RETENTION_DAYS=7` (1 day under
  disk pressure) it's deleted — exactly what ❌ Batal does, with the
  master still on Drive. It ships **off** (`0`): replaying the box's
  real `posts.json` showed it would delete a file the 2026-10-04
  cleanup had deliberately held back for the owner to decide on, and
  those show up under "unfinished" in `/cleanup` until you opt in. Tapping ✅ Post on one later says "file sudah gak ada,
  kirim ulang". Only entries that have a `gdrive_url`, no platform
  result at all, and no schedule qualify; anything that was tried and
  failed is *unfinished*, not abandoned, and is kept for retry.
- **The trimmed story copy** (`<key>_story.mp4`), which state never
  recorded and so was never cleaned.

And one thing that is only *reported*: files in `incoming/` or the
posting-copy dir that no state entry points at (an interrupted
download, leftovers from before state tracked them), older than a day.
Nothing proves their master is on Drive, so `/cleanup`, `/status` and
the CLI list them as "untracked" and leave the decision to you.

**The bot does this by itself — no cron needed.** It runs the cleanup
after every posting run, daily at `CLEANUP_HOUR` (04:00 WIB), and the
moment free disk drops under `DISK_PRESSURE_FREE_GB` (default 2 GB) —
under pressure the retention window is skipped, but every rule above
still applies (plus a 30-minute floor so it can never race a run still
writing its state). Because it runs *inside* the container, it also sees
the paths `state/posts.json` actually contains (`/app/incoming/...`),
which a host-side cron run only sees thanks to a path-mapping fallback.
You get a Telegram message only when something was actually deleted, or
when disk is low and nothing safe is left to delete.

Before downloading a new video the bot also checks there's room
(`size × 1.15 + UPLOAD_MIN_FREE_MB`). If not, it tries a pressure
cleanup, and if that isn't enough it refuses with the numbers instead of
filling the disk and leaving a corrupt half-file in `incoming/`.

By hand:

```bash
# In Telegram: preview with a button to delete.
/cleanup

# On the box (inside the container), preview only:
docker exec mentahanpov-mentahanpov-bot-1 python scripts/cleanup_storage.py
# ...and actually delete:
docker exec mentahanpov-mentahanpov-bot-1 python scripts/cleanup_storage.py --apply
```

`AUTO_CLEANUP=false` turns the automatic part off. The old host cron
entry (`scripts/cleanup_storage.py --apply` from `/www/apps/mentahanpov`)
still works but is redundant; if you installed it, remove it so two
things aren't racing.

### Platforms ignored by cleanup, the disk alert, and what is NOT covered

(Merged 2026-10-11 with the cleanup that was already deployed on the box as
`b81febb`; where the two disagreed, that deployed behaviour was kept.)

- **`CLEANUP_IGNORE_PLATFORMS`** (default `tiktok`): a platform listed here
  never blocks cleanup, whatever its status — TikTok is a manual hand-off,
  so its "error" doesn't mean "needs a retry". A video still needs at least
  one *non-ignored* platform result, all `ok`, to be deleted.
- **`/cleanup`** previews (with buttons); **`/cleanup apply`** deletes
  straight away, as before. Both refuse while a posting run is in progress.
- **Disk alert**: every 6 h the bot messages the allowlisted users when free
  space is under `DISK_WARN_FREE_PCT` (25%) / `DISK_ERROR_FREE_PCT` (12%);
  silent while OK. `/status` shows the same check, plus what's using the
  disk and how much cleanup could reclaim.
- **Why it matters:** on 2026-10-02 the HG680's `/www` USB partition hit 0
  bytes free, which stopped Docker — and with it the bot — until it was
  restarted by hand on 2026-10-04.

**Not covered by cleanup** (measured on the box 2026-10-11, `/www` 14 GB,
77% used): `incoming/` + posting copies were only ~0.86 GB at that point.
The big items are outside this project's folders: `/www/docker/containers`
~3.0 GB, of which **one container log (`gmma-btc-signal`, a different
project) is ~2.9 GB** — Docker's default `json-file` driver has no rotation
here (no `/etc/docker/daemon.json` log options). Unused images and build
cache are the other lever (`docker system df`, `docker builder prune -f`),
and `telegram-bot-api-data` keeps its own copy of every download. The
lasting fixes are log rotation for every container (`max-size` / `max-file`
under `log-opts`) and a bigger disk.

This buys headroom but doesn't change the fact that the HG680's internal
storage is small to begin with (it's a repurposed TV box, not a server
with real disk). If `incoming/`/`state/posting_copies/` are still tight
even with cleanup running, the actual fix is more storage, not a smaller
retention window: plug a USB drive or microSD into the box, copy the
existing `incoming/` and `state/posting_copies/` onto it, then replace
each original directory with a symlink to the external one (both paths
are read via `Path` everywhere in this codebase, so a symlink is
transparent to it). `POSTING_COPY_DIR` in `.env` can also point directly
at the external mount instead of symlinking that one.

## Liveness & outage alerts

Added 2026-10-11, after the box sat offline ~19 days without anyone
noticing. Everything that watches the bot from *inside* the box (the
daily report, `/status`) dies with it, so this layers signals that don't:

1. **Startup message (free, always on).** Every time the bot starts it
   says so, and — using the heartbeat file below — how long it was down
   (example: "🟢 Bot online lagi. Sempat mati ± 19 hari 3 jam (terakhir
   hidup 22 Sep 14:05 WIB)"), plus free disk and any resumed scheduled
   posts. It can't
   tell you *during* an outage, but it ends the "how long was it down?"
   guessing the moment the box comes back.
2. **Heartbeat.** The bot rewrites `state/heartbeat.json` every
   `HEARTBEAT_INTERVAL_MIN` (5) minutes. The `dashboard` container serves
   its age, unauthenticated, at `http://<box>:8090/heartbeat` →
   `{"heartbeat_age_s": 42.0}` (one number; nothing about what the bot
   holds or does).
3. **`scripts/watchdog.py` on a *different* machine** — polls that URL
   over Tailscale and messages you on Telegram when the box is
   unreachable, or reachable but its heartbeat is stale (box alive, bot
   dead), and again on recovery. Standard library only. The campus worker
   is the natural host — but on 2026-10-01 *both* the HG680 and the
   campus worker showed offline on Tailscale at the same time, so a
   watcher that shares a failure cause with the thing it watches (power,
   the same network, a Tailscale outage) goes dark together with it.
   That's what option 4 below is for.
   ```cron
   */5 * * * * WATCHDOG_URL=http://100.97.108.67:8090/heartbeat WATCHDOG_TELEGRAM_TOKEN=<bot token> WATCHDOG_CHAT_ID=<your numeric id> /usr/bin/python3 /path/to/watchdog.py >> /tmp/watchdog.log 2>&1
   ```
   It alerts after 3 consecutive bad checks (~15 min) and only once per
   outage.
4. **Dead-man's switch (best for "whole box or its internet is gone").**
   Make a free check at healthchecks.io (period 5 min, grace 15 min),
   point its alert at Telegram/email, and put its ping URL in
   `HEARTBEAT_PING_URL`. The bot pings on every beat; you're alerted when
   pings *stop*. This is the only signal that survives the watcher itself
   being down.

Use (4) if you only do one: it needs no second machine.

## Scheduled posting

The confirmation screen has a **📅 Jadwalkan** button next to ✅ Post:
`+1 jam`, `+3 jam`, the next two slots from `SCHEDULE_SLOT_HOURS`
(default 07:00/12:00/19:00 WIB), or **✍️ Ketik jam** — type `19:30`,
`besok 07:00`, `lusa 18:00`, `+90m`, `+2j`. `/jadwal` lists what's
pending with a cancel button each; a scheduled video can still be posted
immediately (🚀) or cancelled (❌, which brings the confirmation screen
back so the caption can be edited first).

The schedule is stored in `state/posts.json` (`scheduled_at`), not just in
memory, and re-registered on every bot start — so a reboot between "set"
and "due" loses nothing. If the bot was down at the due time, the post
runs ~45 seconds after it's back, with a note saying how late. The file is
exempt from cleanup while scheduled. Posting uses the caption as it is at
that moment, so an edit after scheduling still applies (cancel → edit →
reschedule).

## Post performance (insights)

Posting used to be fire-and-forget. Now:

- `main.py` records each platform's own post id (`post_id` in
  `state/posts.json`). The permalink isn't enough for Instagram/Threads.
- The bot collects views/likes/comments ~24h and ~72h after posting
  (hourly job; one attempt per window, retried up to 3 times on failure),
  for YouTube, Facebook, Instagram and Threads. Not Stories (they expire;
  numbers aren't comparable) and not TikTok (the Studio automation gets no
  post id back).
- **`/insights`** shows the best posts of the last 30 days and per-platform
  averages; `/insights refresh` collects now. The dashboard has the same
  table.
- **Feedback into captions:** once ≥ `INSIGHTS_MIN_SAMPLES` (5) posts have
  numbers, the opening *hook line* (never the whole caption) of the top
  `CAPTION_TOP_HOOKS` (3) is added to Gemini's prompt as a style/rhythm
  reference, with an instruction not to reuse wording or details. With
  fewer samples the prompt is byte-identical to before. `CAPTION_TOP_HOOKS=0`
  turns it off; you still review every caption before it posts.

What it needs, and what to expect:

| Platform | Needs |
|---|---|
| YouTube | `YOUTUBE_API_KEY` in `.env` (Google Cloud → enable *YouTube Data API v3* → API key). The upload-only OAuth token can't read statistics. |
| Facebook | Page token with `read_insights` for video views; likes/comments come without it. |
| Instagram | Token whose app has `instagram_manage_insights`. |
| Threads | Token with `threads_manage_insights`. |

Platforms with no key/permission are skipped; a permission error is stored
on that post's snapshot (visible in `/insights refresh` output), not
swallowed. **This has not been run against the live APIs from a dev
machine** — Meta renames insight metrics now and then, so the Facebook/
Instagram fetchers ask for candidates one at a time and keep what answers.
Run `/insights refresh` after the first day and read what comes back.

## TikTok failures now show their screenshot

When the TikTok worker fails, it ships its page screenshot back with the
error (`worker_service.py` → `distributors/tiktok_remote.py` →
`core/debug_artifacts.py`), and the bot posts it in the chat next to the
failure message, before the manual-posting kit. So "which popup stole
focus" no longer needs an SSH session to the campus box and a `scp` of
`/tmp/...png`. **The worker side must be redeployed for this** (below);
until then failures look exactly as before.

## Deploying this batch of changes

Nothing here is deployed by committing. The box already runs `b81febb`
(auto cleanup, disk alert, `TIKTOK_ENABLED`, `scripts/mentahan.sh`); this
batch is built on top of it, so it is a plain `git pull` away.

Before pulling, on the box:

- `scripts/mentahan.sh` has a local mode change (`chmod +x`) there. The
  repo now records mode 755 for it, so the pull applies cleanly; if git
  still refuses, `git stash` (or `git checkout -- scripts/mentahan.sh`)
  first — the only local difference is the executable bit.
- `.venv-admin/` is untracked on the box and untouched by any of this.
- **Do not start the `tailscale-hg680` container.** It was stopped with
  `restart=no` on 2026-10-11; Tailscale is the host service now. The updated
  `mentahan.sh` no longer starts it (the old one's `fix` and `watchdog-run`
  did, which would have brought the outage back).

Then, on the HG680:

```
cd /www/apps/mentahanpov
git pull
docker compose build mentahanpov-bot
docker compose up -d
```

The campus worker (`msi-server`) only matters for the TikTok screenshot and
is currently offline; with TikTok switched off (`TIKTOK_ENABLED=false`)
nothing there needs deploying.

New `.env` variables are all optional with safe defaults (see `.env.example`:
`AUTO_CLEANUP`, `CLEANUP_HOUR`, `ABANDONED_RETENTION_DAYS`,
`DISK_PRESSURE_FREE_GB`, `UPLOAD_MIN_FREE_MB`, `HEARTBEAT_*`,
`GOOGLE_TOKEN_MAX_AGE_DAYS`, `YOUTUBE_API_KEY`, `CAPTION_TOP_HOOKS`,
`SCHEDULE_SLOT_HOURS`). Behaviour that changes on its own once deployed:

- the bot sends a "🟢 Bot online" message at every start;
- captions get up to 3 "proven hooks" as style reference — but only after
  5 posts have performance data, which needs `YOUTUBE_API_KEY` /
  Meta insights permissions, so by default this stays inert.

After the bot restarts, confirm: the "🟢 Bot online" message arrives,
`/cleanup` shows real numbers, `/status` Disk line lists what's using the
disk, and `http://<box>:8090/heartbeat` returns a small age.

## Admin dashboard

Added 2026-09-04 — `dashboard.py` above is deliberately read-only, so
rotating a token or fixing a broken credential still meant SSH + a text
editor. `admin.py` is the read-write sibling: edit every `.env` value,
upload/replace/delete the credential files, and start/stop/apply the bot
from a browser.

**Why it's not just another docker-compose service** — `dashboard.py`'s
container is intentionally sandboxed (`credentials/`/`state/` mounted
`:ro`, no `.env` file inside the container, no Docker socket) specifically
so a compromised or buggy read-only page can't touch anything. Giving
that same container write access plus a Docker socket to support editing
would mean either undoing that sandboxing or building a second, riskier
container — both worse than just running a second small process. So
`admin.py` runs directly on the host instead:

```bash
cd /www/apps/mentahanpov
python3 -m venv .venv-admin
.venv-admin/bin/pip install flask python-dotenv
```

Then as a systemd unit (`/etc/systemd/system/mentahanpov-admin.service`):

```ini
[Unit]
Description=MentahanPOV admin dashboard
After=network.target docker.service

[Service]
WorkingDirectory=/www/apps/mentahanpov
ExecStart=/www/apps/mentahanpov/.venv-admin/bin/python admin.py
Restart=on-failure
# Root, not the deploy user: /www/apps/mentahanpov (.env, credentials/)
# is root-owned like the rest of this deployment, and the Config/
# Credentials tabs need write access to it.
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mentahanpov-admin
```

`admin.py` shells out to `docker compose` for bot start/stop/apply and
for triggering a manual run (`docker compose run --rm mentahanpov-bot
python main.py ...` — same image, deps, and mounted credentials as the
real bot, no second Python environment needed for the heavy stuff);
running the unit as root sidesteps needing the deploy user in the
`docker` group just for this.

Reachable at `http://<box-ip>:${ADMIN_PORT:-8091}`, same
`DASHBOARD_USER`/`DASHBOARD_PASSWORD` Basic Auth as the read-only
dashboard. **Unlike that one, this page can rewrite every credential the
pipeline uses** — treat its URL like a root password. LAN/Tailscale-only,
same as the status dashboard; don't port-forward it to the internet.

Saving `.env` here only rewrites the file — it does **not** restart
anything (`docker compose up -d` recreates whichever service's config
changed, and `env_file` is only read at container *creation*). The Bot
tab has a "Terapkan" button that runs exactly that.

**Telegram `/dashboard`** replies with the link (set `ADMIN_DASHBOARD_URL`
in `.env` to this box's Tailscale IP, e.g. `http://100.73.125.7:8091`) —
same allowlist as `/status` and video uploads, since whoever can reach it
can rewrite every credential the pipeline uses. Requires a
`docker compose build mentahanpov-bot` + `docker compose up -d` to pick up
(it's a code change to `telegram_bot.py`, baked into the image like
everything else that isn't bind-mounted).

## TikTok remote worker

Added 2026-09-02, redesigned 2026-09-04. The HG680P is an ARM board with
limited RAM — it can't run Playwright/Chromium (the only working TikTok
posting method; TikTok's official Content Posting API needs an
app-review approval that can take weeks). Rather than skip TikTok
entirely, posting is delegated to a second, more capable machine (the
campus server, x86, more RAM).

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

### The Telegram-relay version never actually worked

The original design (2026-09-02) hands the job off through a shared
"MentahanPOV Jobs" Telegram group: the main bot posts `TIKTOK_JOB <id>
<url> <caption>` into the group, and a second, dedicated worker bot
long-polls that same group for it.

**This never worked, in either direction.** Telegram bots silently do
not receive `message` updates for messages sent by *other bots* — this
is a platform-level restriction (anti bot-loop), independent of Group
Privacy, chat membership, or anything either bot's code does. Confirmed
2026-09-04 by direct testing: `getUpdates` on the worker bot's own token
returned nothing for a `TIKTOK_JOB` message posted by the main bot into
a group both bots were confirmed to be members of (`getChat` succeeded,
`getMe.can_read_all_group_messages: true`), yet the *exact same text*,
typed by a human directly into the group, was picked up by the worker
immediately. The 2026-09-02 setup's own "verification" step had used a
manually-typed human test message, which is why it looked like it
worked — it tested the wrong thing.

Since replies would have gone through the same mechanism in the opposite
direction, `TIKTOK_DONE`/`TIKTOK_FAILED` replies from the worker bot back
to the main bot's chat were equally undeliverable. Every TikTok post
attempted through this design silently failed after its
`TIKTOK_REMOTE_TIMEOUT_S` (600s) with no diagnostic beyond "worker didn't
reply" — because the worker never received the job in the first place,
not because it was slow or broken.

### Current design: direct HTTP over Tailscale

Both boxes join the same Tailscale (tailscale.com) mesh network, giving
each a stable private IP reachable from the other without any port
forwarding or public exposure — replacing the Telegram relay with a
plain HTTP request:

```
HG680 (main.py)                    Tailscale mesh              Campus server
──────────────────                 (100.x.x.x)                 ───────────────
distributors/          POST /tiktok-job                        worker_service.py
tiktok_remote.py  ─────{video_url, caption,──────────────────▶ ThreadingHTTPServer
  builds the request    secret}                                  on TIKTOK_WORKER_PORT
  with the watermarked                                            (default 8790)
  copy's public URL                                             on request:
  (post_url, same as                                              downloads video,
  Instagram's flow)                                                runs distributors/
                                                                    tiktok.py Playwright
  waits up to                    ◀────{"status": "ok",             flow UNCHANGED,
  TIKTOK_REMOTE_                       "url": "..."}                responds inline —
  TIMEOUT_S for the                or {"status": "error",          no separate reply
  HTTP response                        "error": "..."}}             channel needed
```

One request, one response — no polling, no shared group, no second bot
token. `main.py` picks the local-Playwright path or this remote one for
`PLATFORM_REGISTRY["tiktok"]` automatically, based on whether
`TIKTOK_WORKER_HOST` is set.

**One-time setup.**

1. Install Tailscale on both boxes and log both into the same tailnet
   (same Tailscale account). On a box where installing a system package
   isn't convenient, running it in a container works fine and needs no
   `sudo`:
   ```bash
   docker run -d --name=tailscale --hostname=<name> \
     -v ~/tailscale-state:/var/lib/tailscale \
     -v /dev/net/tun:/dev/net/tun \
     --cap-add=NET_ADMIN --cap-add=NET_RAW --net=host \
     --restart=unless-stopped tailscale/tailscale tailscaled
   docker exec <container> tailscale up --hostname=<name>
   ```
   The `tailscale up` command prints a `https://login.tailscale.com/a/...`
   URL — open it and approve. Check `tailscale status` (or the
   Tailscale admin console) for the resulting IP.
2. Note the campus server's Tailscale IP (or its MagicDNS name, if
   enabled) and set on **both** boxes' `.env`:
   `TIKTOK_WORKER_HOST=<that IP or name>`,
   `TIKTOK_WORKER_PORT=8790` (or any free port),
   `TIKTOK_WORKER_SHARED_SECRET=<any random string, same value both
   sides>` — Tailscale already restricts *who* can reach the port at
   all; the secret just stops another device on the same tailnet from
   queuing jobs.
3. On the campus server: clone this repo, then
   ```bash
   pip install -r requirements-worker.txt
   playwright install --with-deps chromium
   python -m distributors.tiktok login   # headed browser, log in once
   ```
   The resulting `credentials/tiktok-storage.json` only needs to exist
   on the campus server — the HG680 never touches Playwright at all once
   this is set up.
4. Run `worker_service.py` continuously on the campus server as a
   systemd **user** unit (survives reboots and crashes without needing
   root):
   ```bash
   mkdir -p ~/.config/systemd/user
   cat > ~/.config/systemd/user/mentahanpov-tiktok-worker.service <<'EOF'
   [Unit]
   Description=MentahanPOV TikTok remote worker
   After=network-online.target

   [Service]
   WorkingDirectory=%h/mentahanpov-tiktok
   ExecStart=/usr/bin/xvfb-run -a %h/mentahanpov-tiktok/.venv/bin/python worker_service.py
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=default.target
   EOF
   systemctl --user daemon-reload
   systemctl --user enable --now mentahanpov-tiktok-worker
   ```
   `xvfb-run` gives Playwright's non-headless Chromium a virtual display
   on a box with no monitor attached — same as the original setup, this
   part is unchanged. Check status/logs with
   `systemctl --user status mentahanpov-tiktok-worker` and
   `journalctl --user -u mentahanpov-tiktok-worker -f`.
5. Add `tiktok` to `PLATFORMS` (or `TELEGRAM_PLATFORMS`) on the HG680
   once the above is confirmed working end-to-end with a real video —
   `curl -X POST http://<worker-tailscale-ip>:8790/tiktok-job -H
   'Content-Type: application/json' -d
   '{"video_url":"<any public mp4 url>","caption":"test","secret":"<TIKTOK_WORKER_SHARED_SECRET>"}'`
   is a quick way to test the worker directly without running the whole
   pipeline.

**Recovering a stuck job.** Since the worker now responds synchronously
in the same HTTP request, a timeout from `distributors/tiktok_remote.py`
means the connection was actually lost or the worker box is unreachable
— there's no separate "it might still finish, check the group" case
anymore. Check `journalctl --user -u mentahanpov-tiktok-worker -f` on
the campus server and confirm both boxes still show each other in
`tailscale status`.
