"""Gemini wrapper that produces SOP V3 metadata for a video.

Output is plain text formatted to match the SOP V3 spec:
    [VISUAL ANALYSIS]
    ...
    [DRAFT CAPTION]
    ...
    [CLOUD STORAGE]
    <gdrive_url>
"""

from __future__ import annotations

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
    visual_analysis: str
    draft_caption: str
    cloud_storage: str
    raw: str

    def to_caption(self, max_len: int = 2200) -> str:
        """Caption used for posting (just the vibes part, no headers)."""
        return self.draft_caption[:max_len].strip()


def _load_prompt(gdrive_url: str) -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"SOP V3 prompt not found at {PROMPT_PATH}")
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{{GDRIVE_URL}}", gdrive_url)


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


def _parse(raw: str, gdrive_url: str) -> GeminiOutput:
    """Best-effort parser for the [HEADER] sections."""
    sections = {"VISUAL ANALYSIS": "", "DRAFT CAPTION": "", "CLOUD STORAGE": ""}
    current = None
    buffer: list[str] = []

    def flush() -> None:
        if current:
            sections[current] = "\n".join(buffer).strip()

    for line in raw.splitlines():
        stripped = line.strip()
        upper = stripped.upper().strip("[]: ").strip()
        if upper in sections:
            flush()
            current = upper
            buffer = []
            continue
        if current:
            buffer.append(line)
    flush()

    cloud = sections["CLOUD STORAGE"] or gdrive_url
    return GeminiOutput(
        visual_analysis=sections["VISUAL ANALYSIS"],
        draft_caption=sections["DRAFT CAPTION"],
        cloud_storage=cloud,
        raw=raw,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def generate_metadata(video_path: Path, gdrive_url: str) -> GeminiOutput:
    if not config.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is empty. See README → 'Gemini setup'.")
    genai.configure(api_key=config.gemini_api_key)

    log.info("[gemini] uploading %s to File API", video_path.name)
    file = genai.upload_file(path=str(video_path), display_name=video_path.name)
    _wait_active(file)

    prompt = _load_prompt(gdrive_url)
    model = genai.GenerativeModel(config.gemini_model)
    log.info("[gemini] generating SOP V3 metadata with %s", config.gemini_model)

    response = model.generate_content(
        [file, prompt],
        generation_config={"temperature": 0.85, "top_p": 0.95},
    )
    raw = response.text or ""
    log.debug("[gemini] raw response:\n%s", raw)

    parsed = _parse(raw, gdrive_url)
    if not parsed.draft_caption:
        raise RuntimeError("Gemini response missing [DRAFT CAPTION] section")
    return parsed
