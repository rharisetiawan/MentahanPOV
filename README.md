# MentahanPOV — End-to-End Video Pipeline

A local Python pipeline for **MentahanPOV** raw video archive. One command takes a local video file and:

1. Reads ground-truth facts straight from the file: recording date, duration, resolution, and GPS (if the phone embedded it — no screenshot needed).
2. Reverse-geocodes the GPS into a street address.
3. Asks Gemini to produce SOP V3 metadata (title, vibes caption, Drive category, and a standardized filename) as strict JSON — Gemini never invents the facts, only the creative bits.
4. Uploads the **untouched master** to the matching Drive category folder, renamed to the generated filename.
5. Renders a watermarked, feed-sized **posting copy** (capped at 1080p, thin logo in the corner) — this is what actually goes out to platforms, never the raw master.
6. Cross-posts the posting copy to **YouTube Shorts**, **Facebook Page**, **Instagram Reels**, and **TikTok**.
7. Persists per-platform status to a JSON state file so reruns only retry the failed ones.

```text
video file ─► ffprobe facts ─► reverse geocode ─► Gemini SOP V3 (JSON) ─► GDrive upload (master, renamed, correct folder)
                                                                        └► watermark render (posting copy) ─► fan-out to 4 platforms ─► state log
```

## Project layout

```
mentahanpov/
├── main.py                  # CLI entry point
├── telegram_bot.py          # optional Telegram front-end, runs main.py per video
├── config.py                # env loader + typed config
├── core/
│   ├── video_facts.py       # ffprobe: date, duration, resolution, GPS
│   ├── geocode.py           # GPS -> street address (OpenStreetMap Nominatim, free)
│   ├── gdrive.py            # Drive upload + folder resolution + share + URL
│   ├── google_auth.py       # shared OAuth2 flow (used by gdrive.py + distributors/youtube.py)
│   ├── gemini.py            # SOP V3 caption/filename/folder generator (JSON)
│   ├── watermark.py         # master -> watermarked posting copy + story cut
│   └── state.py             # idempotent JSON state log
├── assets/                  # watermark-logo.png lives here (auto-placeholder if missing)
├── distributors/
│   ├── youtube.py           # YouTube Data API v3
│   ├── meta_graph.py        # shared Graph API helpers (used by the 4 modules below)
│   ├── facebook.py          # Graph API /{page}/videos
│   ├── facebook_story.py    # Graph API /{page}/video_stories (3-phase upload)
│   ├── instagram.py         # Graph API Reels
│   ├── instagram_story.py   # Graph API media_type=STORIES
│   └── tiktok.py            # Playwright (login + post subcommands)
├── prompts/sop_v3.txt       # Gemini prompt template
├── requirements.txt
├── .env.example
├── CONTRIBUTING.md          # architecture + how to add a platform/account
└── .gitignore
```

> New here? `CONTRIBUTING.md` has the full pipeline walkthrough and the
> steps for adding a new platform or a second account.

## Prerequisites

- Python 3.10+
- `ffmpeg` / `ffprobe` (**required** — used to read date/duration/resolution/GPS straight from the video file)
- A Google Cloud project (free tier is fine)
- Meta for Developers account
- TikTok account
- A YouTube channel

## Quick start

```bash
git clone <this-repo>
cd mentahanpov

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env                # then fill it in (see "Setup guide" below)
mkdir -p credentials state
# drop credential JSONs into ./credentials/

# one-time TikTok login (opens a real Chrome window)
python -m distributors.tiktok login

# dry run (gdrive + gemini only, no posting)
python main.py /path/to/video.mp4 --dry-run

# full run
python main.py /path/to/video.mp4

# only some platforms
python main.py /path/to/video.mp4 --platforms youtube,instagram
```

---

## Setup guide — credentials & tokens

Each section below ends with the `.env` keys it populates.

### 1. Google Drive (OAuth2 installed-app)

Service accounts have **zero storage quota** on regular "My Drive" folders
— Google only lets them write into paid Shared Drives — so this uploads
as your own account instead, via the same OAuth client used for YouTube
(section 4 below creates it; do that first if starting from scratch).

1. Open <https://console.cloud.google.com/>, create or pick a project, and enable **Google Drive API** (**APIs & Services → Library**).
2. Follow section 4 below to create the **Desktop** OAuth client if you haven't yet — Drive reuses that same `credentials/youtube-oauth.json`.
3. Open your **MentahanPOV project root folder** in Drive (the one containing `01 - Suasana Jalan & Perjalanan`, `02 - Cuaca & Hujan`, etc); copy the ID from the URL: `drive.google.com/drive/folders/<THIS_IS_THE_ID>`.

```env
GDRIVE_TOKEN_FILE=./credentials/gdrive-token.json
GDRIVE_FOLDER_ID=<paste root folder id>
DRIVE_CATEGORIES=01 - Suasana Jalan & Perjalanan|02 - Cuaca & Hujan|03 - Alam, Hewan & ASMR|04 - Raw Photos & Textures|05 - Timelapse Assets
```

> First run opens a browser for consent (separately from the YouTube one, since it's a different scope) — click Allow, then the token is cached in `GDRIVE_TOKEN_FILE` and refreshes automatically after that.

### 2. Gemini (Google AI Studio)

1. Go to <https://aistudio.google.com/app/apikey> and click **Create API key**.
2. Choose the same GCP project as above (or any project).

```env
GEMINI_API_KEY=<paste key>
GEMINI_MODEL=gemini-2.0-flash-exp
```

> The pipeline uploads the video to the Gemini File API, waits for it to become `ACTIVE`, then asks the model for SOP V3 metadata as strict JSON (`caption`, `file_name`, `folder`). Files in the File API expire after 48 hours — that's fine for one-shot runs. Gemini is only asked for the creative parts (title, vibe, hashtags, folder pick); date/address/duration/resolution are supplied as ground truth so it can't hallucinate them.

### 3. Reverse geocoding (GPS → address)

Nothing to configure. `core/geocode.py` uses OpenStreetMap's free Nominatim
service — no API key, no billing account (unlike Google's Geocoding API,
which requires a billing account attached even within its free tier).

> If a video has no GPS in its metadata (phone location was off, or the
> file was re-encoded), the caption falls back to raw coordinates /
> "Lokasi tidak tersedia" instead of a street address.

### 4. YouTube Data API v3 (OAuth2 installed-app)

1. In the **same** GCP project, **APIs & Services → Library** → enable **YouTube Data API v3**.
2. **OAuth consent screen** → External → fill in app name, support email, dev contact. Add yourself as a Test User.
3. **Credentials → + CREATE CREDENTIALS → OAuth client ID → Desktop app**. Download the JSON; save as `credentials/youtube-oauth.json`.
4. First run of `main.py` opens a browser for consent; the resulting token is cached in `credentials/youtube-token.json` and refreshes automatically.

```env
YOUTUBE_CLIENT_SECRETS=./credentials/youtube-oauth.json
YOUTUBE_TOKEN_FILE=./credentials/youtube-token.json
YOUTUBE_PRIVACY=public            # or unlisted / private
YOUTUBE_CATEGORY_ID=22
```

> The uploader auto-appends `#Shorts` so vertical ≤60s videos are recognised as Shorts.

### 5. Facebook Page + Instagram Reels (Graph API)

You need:
- A Facebook Page (not a personal profile).
- An Instagram **Business** or **Creator** account.
- The IG account linked to the FB Page (Meta Business Suite → Settings → Linked accounts).

Steps:

1. Go to <https://developers.facebook.com/apps> → **Create App** → use case **Other → Business**.
2. Add products: **Facebook Login for Business**, **Instagram Graph API**, **Pages API**.
3. **Tools → Graph API Explorer**:
   - Pick your app, click **Get User Access Token**.
   - Grant scopes: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`, `business_management`.
   - Copy the short-lived **User Access Token**.
4. Convert it to a long-lived Page token (run locally — replace placeholders):

   ```bash
   curl -s "https://graph.facebook.com/v19.0/oauth/access_token?grant_type=fb_exchange_token&client_id=<APP_ID>&client_secret=<APP_SECRET>&fb_exchange_token=<USER_TOKEN>"
   # → returns long-lived USER token
   curl -s "https://graph.facebook.com/v19.0/me/accounts?access_token=<LONG_LIVED_USER_TOKEN>"
   # → returns each Page with its own long-lived Page token
   ```

   The long-lived **Page token** is what you paste into `.env`. It lasts ~60 days; refresh by repeating the second curl any time before expiry.
5. Find your IG Business User ID:

   ```bash
   curl -s "https://graph.facebook.com/v19.0/<PAGE_ID>?fields=instagram_business_account&access_token=<PAGE_TOKEN>"
   ```

```env
FB_PAGE_ID=<page id>
FB_PAGE_ACCESS_TOKEN=<long-lived page token>
IG_USER_ID=<instagram_business_account.id>
IG_USERNAME=<handle without @>
GRAPH_API_VERSION=v19.0
```

> **How Instagram gets the video.** Unlike YouTube and Facebook — which
> accept an upload — Instagram only takes a **public HTTPS URL** and
> fetches the bytes itself. Whatever that URL serves is what gets
> published, so the pipeline uploads the *watermarked posting copy* to a
> private `_posting-temp` folder on Drive, hands Instagram that link, and
> deletes it once publishing finishes. Pointing it at the master's Drive
> link instead would quietly publish un-watermarked footage.
>
> The URL uses the `drive.usercontent.google.com/...&confirm=t` form on
> purpose: the older `drive.google.com/uc?export=download` link stops
> returning video past roughly **100 MB** and serves an HTML "Google
> Drive can't scan this file for viruses" page instead, which Instagram
> fails on. `confirm=t` skips that interstitial at any size.

#### Stories (Facebook + Instagram)

Both are enabled by default via `PLATFORMS` and need no extra permissions
beyond the scopes above:

```env
PLATFORMS=youtube,facebook,instagram,facebook_story,instagram_story
```

Stories expire after 24h — they're a reach booster on top of the
permanent Reel/video, not a replacement. Two things differ from feed posts:

- **Instagram** uses the same container flow with `media_type=STORIES`,
  and carries **no caption** (the API accepts none).
- **Facebook** doesn't use `/{page}/videos` at all; it uses the
  three-phase `/{page}/video_stories` protocol (`start` → byte upload to
  an `rupload.facebook.com` host → `finish`).

Instagram rejects story videos longer than 60s, so anything longer is
trimmed to its first 60 seconds with a stream copy (no re-encode). The
feed post still gets the full-length clip.

> Stories show the same frame as the feed post — centred logo, nothing
> else. Don't be tempted to burn a call-to-action banner into the video
> to work around the API accepting no caption: it reads as a dialog box
> pasted over the footage and undoes the restraint the watermark is going
> for. Add a native sticker in the Instagram app instead.

### 6. TikTok (manual by design)

**TikTok is deliberately left out of `PLATFORMS`.** Every other platform
publishes over an API; TikTok has none available here, so posting means
driving TikTok Studio in a real browser. That rules out the always-on box
the bot runs on: Playwright ships no official Chromium for ARM64 Linux,
and the board has ~800MB of free RAM against a heavy single-page app.

Instead the Telegram bot replies with the watermarked file and the
caption in a one-tap-copy block, so posting is save-and-upload on the
phone that sent the video — a few seconds, and nothing to break.

The Playwright path below still exists and works **on a desktop**, if you
want TikTok automated for runs you start from the Mac. It can't serve
bot-triggered runs.

1. `playwright install chromium` (one-time, already in quick-start).
2. Run the login helper:

   ```bash
   python -m distributors.tiktok login
   ```

   A real Chromium window opens. Log in with your TikTok account (email/Google/Apple — whatever you use). When you can see your avatar in the top-right, switch to the terminal and press **Enter**. Cookies are saved to `credentials/tiktok-storage.json`.
3. After login is saved you can run headless:

   ```env
   TIKTOK_STORAGE_STATE=./credentials/tiktok-storage.json
   TIKTOK_HEADLESS=true
   ```

> TikTok occasionally redesigns the upload page, which can break selectors. If `post()` raises "Could not find file input / caption box", run the login flow again and watch how the page renders — the selectors at the top of `distributors/tiktok.py` are easy to update.

### 7. Watermark logo

Nothing to configure — the first run auto-generates a placeholder
"MentahanPOV" text logo at `WATERMARK_LOGO_PATH` (default
`./assets/watermark-logo.png`) using Pillow. Whenever your real logo is
ready, just overwrite that exact file with a transparent PNG; the next run
picks it up automatically.

> Watermarking always uses ffmpeg's `overlay` filter on this PNG, never
> `drawtext` — plenty of ffmpeg builds (including a stock Homebrew one)
> ship without freetype/fontconfig, which makes `drawtext` silently
> unavailable. Check yours with `ffmpeg -hide_banner -filters | grep drawtext`;
> if that prints nothing, the placeholder-PNG approach here is exactly why
> it still works.

```env
WATERMARK_LOGO_PATH=./assets/watermark-logo.png
WATERMARK_OPACITY=0.35
# Logo width as a fraction of video width (0.18 = 18%), NOT pixels — a
# fixed px size reads as huge on a 720p clip and vanishes on 4K.
WATERMARK_WIDTH_PCT=0.18
POSTING_COPY_DIR=./state/posting_copies
```

The logo itself is a solid-white recolour of the brand mark (alpha
channel kept, RGB flattened to white) — a multi-colour logo at low
opacity reads as a smudge, white stays legible as a single clean shape.

The logo is centred in the frame. Centre placement is harder to crop out
than a corner, which is the point of a watermark on freely-redistributed
footage — the trade-off is that it sits over the subject, so keep the
opacity low (0.35 reads clearly without fighting the video).

> **The watermark only ever touches the social copy.** The file uploaded
> to Drive is the original master — no logo, no re-encode, no downscale.
> That's the whole promise of the archive, so the pipeline renders a
> separate posting copy rather than modifying the file it uploads.

### 8. Telegram bot (optional — trigger from your phone)

Skip this if you're happy running `python main.py` from a terminal. This
is only for not having to touch a terminal at all: send a video to your
bot from Telegram, get the result links back in chat.

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts → copy the token it gives you.
2. Message **@userinfobot** to find your own numeric Telegram user id (so randoms who find your bot's link can't trigger it).

```env
TELEGRAM_BOT_TOKEN=<paste token from BotFather>
TELEGRAM_ALLOWED_USER_IDS=<your numeric id>
```

```bash
python telegram_bot.py
```

Leave that running (see "Keeping the bot running" below), then open your
bot in Telegram and send it a video file. It downloads to `./incoming/`,
runs the same pipeline as the CLI (`main.py`) as a subprocess, and replies
with the per-platform links or errors once done — no Vercel/hosting
needed, this just needs to stay running somewhere with your credentials.

> **Always send as 📎 → File, never from the Gallery.** Telegram
> re-encodes anything sent as a "Video": a 64 MB 1080p clip arrives as a
> 23 MB 720p one with every metadata tag stripped — including the
> ISO-6709 `location` tag the caption's 📍 line is built from. Sent as a
> **File** (Document), the bytes arrive untouched, so GPS and full
> quality survive. The bot probes each upload and warns you in chat when
> GPS is missing, but it can't recover what Telegram already discarded.

> Long-running (ffmpeg watermarking, multi-platform uploads, browser-based
> Playwright for TikTok) doesn't fit serverless platforms like Vercel —
> execution-time limits are usually 10-60s, this pipeline routinely takes
> 3-5 minutes per video. Run the bot on your own machine or any small
> always-on box (even a Raspberry Pi) instead.

#### Removing the 20MB download cap

By default the bot talks to the public `api.telegram.org`, which refuses to
hand over any file over 20MB — raw phone footage routinely exceeds that.
Fix it by running your own **Local Bot API Server** (Telegram's official
`telegram-bot-api` binary/Docker image) alongside the bot, which downloads
files locally instead with no size limit:

1. Go to <https://my.telegram.org> → **API development tools** → create an
   app → copy the **api_id** and **api_hash** it gives you.
2. Run the server (Docker is simplest):
   ```bash
   docker run -d --name telegram-bot-api -p 8081:8081 \
     -e TELEGRAM_API_ID=<api_id> -e TELEGRAM_API_HASH=<api_hash> \
     -v telegram-bot-api-data:/var/lib/telegram-bot-api \
     aiogram/telegram-bot-api:latest --local
   ```
3. Add to `.env`:
   ```env
   TELEGRAM_API_ID=<api_id>
   TELEGRAM_API_HASH=<api_hash>
   TELEGRAM_LOCAL_API_URL=http://localhost:8081
   ```

`telegram_bot.py` picks this up automatically on startup (see the "using
Local Bot API Server" log line) — no code changes needed. Leave
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` empty to keep using the public API
(fine as long as your source videos stay under 20MB).

#### Keeping the bot running

Simplest: a dedicated terminal tab, or `screen`/`tmux` so it survives you
closing the terminal:

```bash
screen -S mentahanpov-bot
source .venv/bin/activate && python telegram_bot.py
# Ctrl-A then D to detach; `screen -r mentahanpov-bot` to reattach
```

On macOS, `launchd` (or `pm2` via Node) will also auto-restart it on
crash/reboot — out of scope here, but worth setting up once this is part
of your daily routine.

---

## CLI reference

```text
python main.py VIDEO [--platforms list] [--dry-run] [--skip-gdrive] [--skip-gemini] [--skip-watermark] [--force] [-v]
```

| Flag | Effect |
|---|---|
| `--platforms`      | Subset of `youtube,facebook,instagram,tiktok`. Default = `PLATFORMS` env. |
| `--dry-run`        | Run facts + geocode + Gemini + GDrive, **skip** watermark render + all distribution. |
| `--skip-gdrive`    | Reuse `gdrive_url` from state (re-run after fixing a single platform). |
| `--skip-gemini`    | Reuse caption/file_name/folder from state. |
| `--skip-watermark` | Reuse the cached posting copy from state instead of re-rendering it. |
| `--force`          | Repost even if a platform already succeeded. |
| `-v / --verbose`   | DEBUG-level logging. |

### State file

`state/posts.json` keyed by a hash of the absolute video path:

```json
{
  "a1b2c3d4e5f6": {
    "video_path": "/abs/path/clip01.mp4",
    "gdrive_id": "1xyz...",
    "gdrive_url": "https://drive.google.com/file/d/1xyz/view",
    "caption": "...",
    "gemini": { "file_name": "20260403_MALANG_MOSQUE_JUMATAN_VIBE_32s_1080p.mp4", "folder": "01 - Suasana Jalan & Perjalanan" },
    "posting_copy_path": "./state/posting_copies/a1b2c3d4e5f6_post.mp4",
    "platforms": {
      "youtube":   { "status": "ok",    "url": "https://youtube.com/shorts/abc", "error": null },
      "facebook":  { "status": "ok",    "url": "...", "error": null },
      "instagram": { "status": "error", "url": null, "error": "RuntimeError: container EXPIRED" },
      "tiktok":    { "status": "ok",    "url": "https://www.tiktok.com/tiktokstudio/content" }
    }
  }
}
```

Re-running `main.py` on the same file will skip every platform whose status is `ok`. Use `--force` to override.

## Error handling philosophy

- Each distributor wraps its primary call in `tenacity.retry` (exponential backoff, 2-3 attempts).
- The orchestrator catches `Exception` per platform — one failure does **not** abort the others.
- Failures are recorded in the state file with type + message; the next run picks them up automatically.
- The process exit code is `1` if any platform ended in `error`, `0` if all `ok`.

## Roadmap / extensibility

Adding a new platform (or a new destination on an existing one, e.g. a
second account) means dropping a new file into `distributors/` and wiring
it into `main.py`. See `CONTRIBUTING.md` for the full walkthrough — short
version:

1. Expose `post(video_path, caption, *, gdrive_url=None, post_url=None, story_url=None, **_) -> {"id": str, "url": str}` in the new module. Accept `**_` so it silently ignores kwargs meant for other platforms.
2. Register it in `PLATFORM_REGISTRY` in `main.py`.
3. If it needs a **public URL** rather than raw bytes (like Instagram — see `distributors/instagram.py`'s docstring), add its name to `NEEDS_POST_URL` or `NEEDS_STORY_URL` in `main.py` so `step_publish_urls` stages a Drive link for it automatically.
4. If it's a Story-type destination (24h expiry, no permanent caption), add its name to `STORY_PLATFORMS` so it gets the trimmed/story copy instead of the feed copy.
5. Facebook/Instagram distributors share Graph API plumbing — see `distributors/meta_graph.py` (`graph_url`, `wait_container_finished`) before writing new Graph API calls from scratch.

- Swap the TikTok Playwright module for an HTTP client once your Content Posting API app is approved.
- For larger Reels, upload first to S3/R2 instead of Drive and pass that URL through `extra_urls` in `main.py`'s `step_publish_urls`.

## Security notes

- `.env`, `credentials/`, and `state/` are git-ignored. Never commit them.
- The GDrive folder ID is *not* a secret, but the OAuth client secret and cached tokens (`credentials/*.json`) are — treat them like passwords.
- The Page Access Token is long-lived (~60 days). Rotate it on a schedule.
