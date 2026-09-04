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
