# MentahanPOV — End-to-End Video Pipeline

A local Python pipeline for **MentahanPOV** raw video archive. One command takes a local video file and:

1. Uploads it to Google Drive (anyone-with-link readable).
2. Asks Gemini to produce SOP V3 metadata (visual analysis + vibes-only caption + cloud storage ref).
3. Cross-posts to **YouTube Shorts**, **Facebook Page**, **Instagram Reels**, and **TikTok**.
4. Persists per-platform status to a JSON state file so reruns only retry the failed ones.

```text
video file ─► GDrive upload ─► Gemini SOP V3 ─► fan-out to 4 platforms ─► state log
```

## Project layout

```
mentahanpov/
├── main.py                  # CLI entry point
├── config.py                # env loader + typed config
├── core/
│   ├── gdrive.py            # Drive upload + share + URL
│   ├── gemini.py            # SOP V3 caption generator
│   └── state.py             # idempotent JSON state log
├── distributors/
│   ├── youtube.py           # YouTube Data API v3
│   ├── facebook.py          # Graph API /{page}/videos
│   ├── instagram.py         # Graph API Reels
│   └── tiktok.py            # Playwright (login + post subcommands)
├── prompts/sop_v3.txt       # Gemini prompt template
├── requirements.txt
├── .env.example
└── .gitignore
```

## Prerequisites

- Python 3.10+
- `ffmpeg` (optional, only if you want to pre-validate videos)
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

### 1. Google Drive (Service Account)

1. Open <https://console.cloud.google.com/>, create or pick a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **IAM & Admin → Service Accounts → + CREATE SERVICE ACCOUNT**. Skip the optional roles step.
4. Open the service account → **Keys → Add Key → JSON**. Save the file as `credentials/gdrive-sa.json`.
5. Note the service-account email (looks like `mentahan-uploader@<project>.iam.gserviceaccount.com`).
6. In Drive (web), create a folder e.g. `MentahanPOV/uploads`. Right-click → **Share** → paste the service-account email, role **Editor**.
7. Open the folder; copy the ID from the URL: `drive.google.com/drive/folders/<THIS_IS_THE_ID>`.

```env
GDRIVE_SERVICE_ACCOUNT_JSON=./credentials/gdrive-sa.json
GDRIVE_FOLDER_ID=<paste folder id>
```

### 2. Gemini (Google AI Studio)

1. Go to <https://aistudio.google.com/app/apikey> and click **Create API key**.
2. Choose the same GCP project as above (or any project).

```env
GEMINI_API_KEY=<paste key>
GEMINI_MODEL=gemini-2.0-flash-exp
```

> The pipeline uploads the video to the Gemini File API, waits for it to become `ACTIVE`, then asks the model for SOP V3 metadata. Files in the File API expire after 48 hours — that's fine for one-shot runs.

### 3. YouTube Data API v3 (OAuth2 installed-app)

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

### 4. Facebook Page + Instagram Reels (Graph API)

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
GRAPH_API_VERSION=v19.0
```

> **Important for Instagram:** the API needs a **public HTTPS** URL pointing at the raw video. The pipeline passes the GDrive direct-download URL — works fine for short Reels (<300 MB). For larger files or higher reliability, replace `_drive_direct()` in `distributors/instagram.py` with an upload to S3/R2/Cloudinary.

### 5. TikTok (Playwright fallback)

The official **Content Posting API** requires app-review approval. While you wait (or if you don't want to apply), this pipeline drives TikTok Studio with Playwright.

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

---

## CLI reference

```text
python main.py VIDEO [--platforms list] [--dry-run] [--skip-gdrive] [--skip-gemini] [--force] [-v]
```

| Flag | Effect |
|---|---|
| `--platforms`     | Subset of `youtube,facebook,instagram,tiktok`. Default = `PLATFORMS` env. |
| `--dry-run`       | Run GDrive + Gemini, **skip** all distribution. |
| `--skip-gdrive`   | Reuse `gdrive_url` from state (re-run after fixing a single platform). |
| `--skip-gemini`   | Reuse caption from state. |
| `--force`         | Repost even if a platform already succeeded. |
| `-v / --verbose`  | DEBUG-level logging. |

### State file

`state/posts.json` keyed by a hash of the absolute video path:

```json
{
  "a1b2c3d4e5f6": {
    "video_path": "/abs/path/clip01.mp4",
    "gdrive_id": "1xyz...",
    "gdrive_url": "https://drive.google.com/file/d/1xyz/view",
    "caption": "...",
    "gemini": { "visual_analysis": "...", "draft_caption": "...", "cloud_storage": "..." },
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

- Drop a new file into `distributors/` exposing `post(video, caption, *, gdrive_url=None) -> dict`, then register it in `PLATFORM_REGISTRY` in `main.py`. That's it.
- Swap the TikTok Playwright module for an HTTP client once your Content Posting API app is approved.
- For larger Reels, upload first to S3/R2 and pass that URL to `instagram.post(..., gdrive_url=<s3_url>)`.

## Security notes

- `.env`, `credentials/`, and `state/` are git-ignored. Never commit them.
- The GDrive folder ID is *not* a secret, but the service-account JSON is — treat it like a password.
- The Page Access Token is long-lived (~60 days). Rotate it on a schedule.
