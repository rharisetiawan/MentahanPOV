"""Parses `.env.example` into an ordered schema the dashboard renders as a
form.

The schema is read from `.env.example` itself — key, section, help text,
and field kind (bool / path / secret / text) are all inferred from that
file's own structure and naming conventions — rather than hardcoded here.
That's deliberate: it's what lets this same dashboard work unmodified if
this project is copied for something else. Point `ENV_EXAMPLE` (in
`dashboard/app.py`) at the new project's own `.env.example` and the form
follows it, no code changes needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SECTION_RE = re.compile(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
_BORDER_RE = re.compile(r"^#\s*=+\s*$")
_KV_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")

# Field-kind heuristics. A field is classified "path" before "secret" —
# YOUTUBE_CLIENT_SECRETS, for instance, ends in SECRETS but holds a file
# path, not a literal secret string, so path wins.
_SECRET_KEY_RE = re.compile(r"(TOKEN|API_KEY|SECRET|PASSWORD|HASH)$")
_PATH_KEY_RE = re.compile(r"(_FILE|_PATH|_DIR|_STATE|_SECRETS)$")


@dataclass
class Field:
    key: str
    default: str
    comment: str
    kind: str  # "bool" | "path" | "secret" | "text"


@dataclass
class Section:
    title: str
    fields: list[Field] = field(default_factory=list)


def _classify(key: str, default: str) -> str:
    if default.lower() in ("true", "false"):
        return "bool"
    if _PATH_KEY_RE.search(key) or default.startswith("./"):
        return "path"
    if _SECRET_KEY_RE.search(key):
        return "secret"
    return "text"


def parse(example_path: Path) -> list[Section]:
    """Read an `.env.example`-shaped file into ordered, non-empty sections."""
    sections: list[Section] = [Section(title="General")]
    pending_comment: list[str] = []

    for raw_line in example_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()

        if not line:
            pending_comment = []
            continue
        if _BORDER_RE.match(line):
            continue

        m = _SECTION_RE.match(line)
        if m:
            sections.append(Section(title=m.group(1)))
            pending_comment = []
            continue

        if line.startswith("#"):
            pending_comment.append(line.lstrip("#").strip())
            continue

        m = _KV_RE.match(line)
        if m:
            key, default = m.group(1), m.group(2)
            sections[-1].fields.append(
                Field(
                    key=key,
                    default=default,
                    comment=" ".join(c for c in pending_comment if c),
                    kind=_classify(key, default),
                )
            )
            pending_comment = []

    return [s for s in sections if s.fields]


def all_fields(sections: list[Section]) -> list[Field]:
    return [f for s in sections for f in s.fields]


def find_field(sections: list[Section], key: str) -> Field | None:
    return next((f for f in all_fields(sections) if f.key == key), None)
