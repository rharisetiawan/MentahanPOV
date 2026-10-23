"""Gemini wrapper that produces SOP V3 caption/filename/folder metadata.

Ground-truth facts (date, address, coordinates, duration, resolution) are
extracted locally (see core/video_facts.py + core/geocode.py) and fed into
the prompt so Gemini only has to be creative about the vibe/title/caption
and pick the right Drive folder — it never invents the facts themselves.
Output is strict JSON: {"caption", "file_name", "folder"} plus the optional
per-platform "youtube_title" and "threads_text".
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "sop_v3.txt"


@dataclass
class GeminiOutput:
    caption: str
    file_name: str
    folder: str
    raw: str
    # Per-platform rewrites (main.caption_for): "youtube_title", "threads_text".
    variants: dict[str, str] = field(default_factory=dict)


# Hard limits the variants are trimmed to (YouTube caps titles at 100 and
# _shortsify may add nothing to the title; Threads' API caps posts at 500).
VARIANT_LIMITS = {"youtube_title": 95, "threads_text": 500}


def _variants(data: dict) -> dict[str, str]:
    """The optional per-platform texts, cleaned; anything odd is dropped.

    Optional on purpose: a model that skips them must not fail the run —
    each platform then just gets the full caption, as before.
    """
    out: dict[str, str] = {}
    for key, limit in VARIANT_LIMITS.items():
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if key == "youtube_title":
            value = " ".join(value.split())  # a title is one line
        else:
            value = _tidy_caption(value)
        if len(value) > limit:
            value = value[: limit - 1].rstrip() + "…"
        out[key] = value
    return out


def _load_prompt(
    *,
    date: str,
    address: str | None,
    coordinates: str | None,
    duration_s: int,
    resolution: str,
    top_hooks: list[str] | None = None,
) -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"SOP V3 prompt not found at {PROMPT_PATH}")
    template = PROMPT_PATH.read_text(encoding="utf-8")
    folders_list = "\n".join(f"- {f}" for f in config.drive_categories)

    # Style reference drawn from this account's own best performers (see
    # core/insights.py). Hooks only — never whole captions — and framed as
    # rhythm/shape reference so the model doesn't parrot or carry over any
    # detail. The placeholder swallows its trailing blank line when there's
    # nothing to show, so an account with no data yet gets a byte-identical
    # prompt to the one that existed before this feature.
    if top_hooks:
        hooks_block = (
            "PROVEN HOOKS — opening lines that performed best on this account so\n"
            "far. Use them ONLY as a reference for rhythm and tone. Never reuse\n"
            "their wording and never carry over any detail from them (place,\n"
            "sound, object): your hook must be about THIS video.\n"
            + "\n".join(f"- {h}" for h in top_hooks)
            + "\n\n"
        )
    else:
        hooks_block = ""
    template = template.replace("{{TOP_HOOKS_BLOCK}}\n\n", hooks_block)
    template = template.replace("{{TOP_HOOKS_BLOCK}}", hooks_block.rstrip("\n"))

    # With no GPS in the file there is nothing honest to print, so the whole
    # location line — and the location half of the file name — is dropped
    # rather than filled with "not available", which previously leaked into
    # captions and produced names like 20260813_NO_LOCATION_....
    # Deriving <LOCATION> is left to Gemini rather than parsed here: picking
    # the city out of a Nominatim address ("…Sukun, Kota Malang, Klojen,
    # Jawa Timur, 65147, Indonesia") by position gets the province wrong as
    # often as not.
    if address and coordinates:
        address_fact = f"- Address: {address}"
        location_line = f"📍 Location: {address} ({coordinates})"
        filename_pattern = f"{date}_<LOCATION>_<VIBE>_{duration_s}s_{resolution}.mp4"
    else:
        address_fact = "- Address: (none — this file carries no GPS)"
        location_line = ""
        filename_pattern = f"{date}_<VIBE>_{duration_s}s_{resolution}.mp4"

    return (
        template.replace("{{DATE}}", date)
        .replace("{{ADDRESS_FACT}}", address_fact)
        .replace("{{LOCATION_LINE}}", location_line)
        .replace("{{FILENAME_PATTERN}}", filename_pattern)
        .replace("{{DURATION_S}}", str(duration_s))
        .replace("{{RESOLUTION}}", resolution)
        .replace("{{FOLDERS_LIST}}", folders_list)
    )


def _wait_active(
    client: genai.Client, file: genai_types.File, timeout: int = 300
) -> None:
    """Poll until uploaded video is ACTIVE (Gemini File API processes async)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = client.files.get(name=file.name)
        if f.state == "ACTIVE":
            return
        if f.state == "FAILED":
            raise RuntimeError(f"Gemini file {file.name} failed processing")
        time.sleep(3)
    raise TimeoutError(f"Gemini file {file.name} not ACTIVE after {timeout}s")


def _tidy_caption(caption: str) -> str:
    """Collapse the gap left behind when the location line is omitted.

    The template has a blank line on either side of the location line, so
    dropping it leaves three consecutive newlines. Asking the model to
    handle that itself is unreliable, so it's normalised here instead.
    """
    return re.sub(r"\n{3,}", "\n\n", caption).strip()


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def _upload_and_wait(client: genai.Client, video_path: Path) -> genai_types.File:
    log.info("[gemini] uploading %s to File API", video_path.name)
    file = client.files.upload(file=str(video_path))
    _wait_active(client, file)
    return file


# Gemini's "high demand" 503s are Google-side and have outlasted a short
# backoff window in practice (seen repeatedly in one day against
# gemini-2.5-flash, one run spending 7+ minutes straight 503ing). This
# retries only the generation+parse step, not the upload above — redoing
# a multi-hundred-MB upload on every retry would waste real minutes on
# slow hardware for no reason once the bytes are already sitting in the
# File API. ~5+10+20+40+60s across 5 waits gives a demand spike roughly
# 2-3 minutes to clear before giving up on this model.
@retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=5, min=5, max=60))
def _generate_and_parse(
    client: genai.Client, file: genai_types.File, prompt: str, model: str
) -> tuple[dict, str]:
    log.info("[gemini] generating SOP V3 metadata with %s", model)
    response = client.models.generate_content(
        model=model,
        contents=[file, prompt],
        config=genai_types.GenerateContentConfig(
            temperature=0.85,
            top_p=0.95,
            response_mime_type="application/json",
        ),
    )
    raw = response.text or ""
    log.debug("[gemini] raw response:\n%s", raw)

    data = _parse_json(raw)
    for key in ("caption", "file_name", "folder"):
        if not data.get(key):
            raise RuntimeError(f"Gemini response missing '{key}' key: {raw}")
    if data["folder"] not in config.drive_categories:
        raise RuntimeError(
            f"Gemini picked an unknown folder {data['folder']!r}; "
            f"expected one of {config.drive_categories}"
        )
    return data, raw


def _delete_upload(client: genai.Client, file: genai_types.File) -> None:
    """Don't leave the raw master on Google's servers for the File API's 48h
    (it also counts against the project's 20 GB File API quota)."""
    try:
        client.files.delete(name=file.name)
    except Exception:  # noqa: BLE001 — it expires on its own anyway
        log.warning("[gemini] couldn't delete uploaded file %s", file.name, exc_info=True)


def generate_metadata(
    video_path: Path,
    *,
    date: str,
    address: str,
    coordinates: str,
    duration_s: int,
    resolution: str,
) -> GeminiOutput:
    if not config.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is empty. See README → 'Gemini setup'.")
    client = genai.Client(api_key=config.gemini_api_key)

    file = _upload_and_wait(client, video_path)
    try:
        try:
            # Imported here, not at module top: insights pulls in requests and
            # the Graph helpers, which a caption run shouldn't hard-depend on —
            # and a bug in the feedback loop must never block captioning.
            from core import insights

            top_hooks = insights.top_hooks()
        except Exception:  # noqa: BLE001 — optional enrichment, never fatal
            log.exception("[gemini] couldn't load top hooks; continuing without")
            top_hooks = []
        if top_hooks:
            log.info("[gemini] using %d proven hook(s) as style reference", len(top_hooks))

        prompt = _load_prompt(
            date=date,
            address=address,
            coordinates=coordinates,
            duration_s=duration_s,
            resolution=resolution,
            top_hooks=top_hooks,
        )

        # A "high demand" 503 tends to be specific to one model's capacity
        # pool — often the newest release, which is where demand spikes
        # concentrate — so a model that's still fully capable but not the one
        # everyone's currently hammering is a free way to ride it out instead
        # of just waiting. Only kicks in once GEMINI_MODEL has exhausted its
        # own retries above; same file, no re-upload either way.
        models = [config.gemini_model]
        if config.gemini_fallback_model and config.gemini_fallback_model not in models:
            models.append(config.gemini_fallback_model)

        last_exc: Exception | None = None
        for model in models:
            try:
                data, raw = _generate_and_parse(client, file, prompt, model)
                return GeminiOutput(
                    caption=_tidy_caption(data["caption"]),
                    file_name=data["file_name"],
                    folder=data["folder"],
                    raw=raw,
                    variants=_variants(data),
                )
            except Exception as exc:  # noqa: BLE001 — try the next model, if any
                last_exc = exc
                log.warning("[gemini] %s exhausted its retries: %s", model, exc)

        assert last_exc is not None  # models is never empty
        raise last_exc
    finally:
        _delete_upload(client, file)
