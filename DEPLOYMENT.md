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
