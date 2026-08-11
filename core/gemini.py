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
import time
from dataclasses import dataclass
from pathlib import Path

import google.generativeai as genai
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
    *, date: str, address: str, coordinates: str, duration_s: int, resolution: str
) -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"SOP V3 prompt not found at {PROMPT_PATH}")
    template = PROMPT_PATH.read_text(encoding="utf-8")
    folders_list = "\n".join(f"- {f}" for f in config.drive_categories)
    return (
        template.replace("{{DATE}}", date)
        .replace("{{ADDRESS}}", address)
        .replace("{{COORDINATES}}", coordinates)
        .replace("{{DURATION_S}}", str(duration_s))
        .replace("{{RESOLUTION}}", resolution)
        .replace("{{FOLDERS_LIST}}", folders_list)
    )


def _wait_active(file: "genai.types.File", timeout: int = 300) -> None:
    """Poll until uploaded video is ACTIVE (Gemini File API processes async)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = genai.get_file(file.name)
        if f.state.name == "ACTIVE":
            return
        if f.state.name == "FAILED":
            raise RuntimeError(f"Gemini file {file.name} failed processing")
        time.sleep(3)
    raise TimeoutError(f"Gemini file {file.name} not ACTIVE after {timeout}s")


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
    genai.configure(api_key=config.gemini_api_key)

    log.info("[gemini] uploading %s to File API", video_path.name)
    file = genai.upload_file(path=str(video_path), display_name=video_path.name)
    _wait_active(file)

    prompt = _load_prompt(
        date=date,
        address=address,
        coordinates=coordinates,
        duration_s=duration_s,
        resolution=resolution,
    )
    model = genai.GenerativeModel(config.gemini_model)
    log.info("[gemini] generating SOP V3 metadata with %s", config.gemini_model)

    response = model.generate_content(
        [file, prompt],
        generation_config={
            "temperature": 0.85,
            "top_p": 0.95,
            "response_mime_type": "application/json",
        },
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
        caption=data["caption"],
        file_name=data["file_name"],
        folder=data["folder"],
        raw=raw,
    )
