"""Gemini wrapper that produces SOP V3 caption/filename/folder metadata.

Ground-truth facts (date, address, coordinates, duration, resolution) are
extracted locally (see core/video_facts.py + core/geocode.py) and fed into
the prompt so Gemini only has to be creative about the vibe/title/caption
and pick the right Drive folder — it never invents the facts themselves.
Output is strict JSON: {"caption", "file_name", "folder"}.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
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


def _load_prompt(
    *,
    date: str,
    address: str | None,
    coordinates: str | None,
    duration_s: int,
    resolution: str,
) -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"SOP V3 prompt not found at {PROMPT_PATH}")
    template = PROMPT_PATH.read_text(encoding="utf-8")
    folders_list = "\n".join(f"- {f}" for f in config.drive_categories)

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

    log.info("[gemini] uploading %s to File API", video_path.name)
    file = client.files.upload(file=str(video_path))
    _wait_active(client, file)

    prompt = _load_prompt(
        date=date,
        address=address,
        coordinates=coordinates,
        duration_s=duration_s,
        resolution=resolution,
    )
    log.info("[gemini] generating SOP V3 metadata with %s", config.gemini_model)

    response = client.models.generate_content(
        model=config.gemini_model,
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

    return GeminiOutput(
        caption=_tidy_caption(data["caption"]),
        file_name=data["file_name"],
        folder=data["folder"],
        raw=raw,
    )
