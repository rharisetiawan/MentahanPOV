"""Doctor Idol Season IV catalogue: who sang what, and where their files live.

The data is `doctor_idol/finalists.toml` (tracked in git, edited by hand).
The video files are not in git: they live in `media/doctor-idol-s4/` at the
root of the main checkout, so every git worktree of this repo sees the same
recordings without copying gigabytes around:

    media/doctor-idol-s4/
      raw/<slug>.mp4            the full live recording, untouched
      reels/<slug>/reels.mp4    the vertical clip for IG/TikTok/Shorts
      reels/<slug>/caption.txt  captions for each platform

TOML rather than YAML: `tomllib` ships with Python 3.11+, so this needs no
new dependency.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CATALOG_PATH = PACKAGE_DIR / "finalists.toml"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}

# Matches the slug rules below: lower-case words joined by single dashes.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
STATUSES = ("draft", "approved", "posted")
KINDS = ("performance", "announcement")


class CatalogError(ValueError):
    """finalists.toml has a mistake a person needs to fix."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Finalist:
    slug: str
    episode: int | None = None  # "POV Kursi Penonton #<episode>": posting order, not stage order
    name: str = ""
    city: str = ""
    song: str = ""
    artist: str = ""
    segments: tuple[Segment, ...] = ()
    instagram: str = ""
    tiktok: str = ""
    status: str = "draft"
    notes: str = ""
    kind: str = "performance"  # or "announcement" (pengumuman juara)
    hook: str = ""  # overrides settings.hook_text for this reel
    card: tuple[str, ...] = ()  # announcement card lines, e.g. "Juara 1 · dr. X"
    unconfirmed: tuple[str, ...] = ()  # other unverified details for the disclaimer, e.g. "gelar"
    full: Segment | None = None  # YouTube version's range; None = the whole recording

    def uncertain(self) -> list[str]:
        """What the caption's disclaimer must admit: empty or "(?)" details, plus `unconfirmed`."""
        items: list[str] = []
        if self.kind != "announcement":
            if not self.city or "(?)" in self.city:
                items.append("kota")
            if not self.song or "(?)" in self.song:
                items.append("judul lagu")
            elif not self.artist or "(?)" in self.artist:
                items.append("penyanyi asli")
        return items + list(self.unconfirmed)

    @property
    def label(self) -> str:
        """"dr. Kartika Widyasari · Malang" for the on-video card."""
        return " · ".join(part for part in (self.name, self.city) if part)

    @property
    def clip_length(self) -> float:
        return sum(s.length for s in self.segments)

    def reel_length(self, transition_s: float) -> float:
        """Length after the crossfades, which overlap neighbouring segments."""
        overlaps = max(0, len(self.segments) - 1) if transition_s > 0 else 0
        return self.clip_length - overlaps * transition_s

    def missing(self) -> list[str]:
        """Fields still empty that a finished reel needs."""
        if self.kind == "announcement":
            wanted = {"name": self.name, "card": " ".join(self.card)}
        else:
            wanted = {"name": self.name, "city": self.city, "song": self.song, "artist": self.artist}
        gaps = [key for key, value in wanted.items() if not value]
        if not self.segments:
            gaps.append("segments")
        return gaps


@dataclass(frozen=True)
class Settings:
    season: str = "Season IV"
    event: str = "Doctor Idol Competition"
    series: str = "POV Kursi Penonton"
    transition_s: float = 0.3
    hook_text: str = "POV: kamu di kursi penonton"
    hook_s: float = 2.5
    name_card: bool = True
    name_card_s: float = 4.0
    score_text: str = "Skor kamu berapa? 1-10"
    verdict_text: str = "Setuju sama juri?"  # the closing prompt on announcement reels
    score_s: float = 4.0
    progress_bar: bool = True
    logo_width_px: int = 216
    logo_opacity: float = 0.92
    target_clip_s: float = 30.0


@dataclass(frozen=True)
class Catalog:
    settings: Settings
    finalists: tuple[Finalist, ...] = field(default_factory=tuple)

    def get(self, slug: str) -> Finalist:
        for finalist in self.finalists:
            if finalist.slug == slug:
                return finalist
        raise CatalogError(f"tidak ada finalis dengan slug '{slug}' di {CATALOG_PATH.name}")


def _segment(raw, slug: str) -> Segment:
    if not isinstance(raw, list) or len(raw) != 2:
        raise CatalogError(f"{slug}: setiap segmen harus [mulai, selesai] dalam detik, dapat {raw!r}")
    start, end = (float(x) for x in raw)
    if start < 0 or end <= start:
        raise CatalogError(f"{slug}: segmen [{start:g}, {end:g}] tidak valid")
    return Segment(start, end)


def parse_catalog(text: str) -> Catalog:
    data = tomllib.loads(text)
    try:
        settings = Settings(**data.get("settings", {}))
    except TypeError as exc:
        raise CatalogError(f"[settings]: {exc}") from exc
    finalists: list[Finalist] = []
    seen: set[str] = set()
    for row in data.get("finalist", []):
        slug = str(row.get("slug", ""))
        if not _SLUG_RE.match(slug):
            raise CatalogError(f"slug '{slug}' harus huruf kecil, angka dan tanda -")
        if slug in seen:
            raise CatalogError(f"slug '{slug}' dipakai dua kali")
        seen.add(slug)
        segments = tuple(_segment(s, slug) for s in row.get("segments", []))
        for a, b in zip(segments, segments[1:]):
            if b.start < a.end:
                raise CatalogError(f"{slug}: segmen harus urut dan tidak tumpang tindih")
        status = row.get("status", "draft")
        if status not in STATUSES:
            raise CatalogError(f"{slug}: status '{status}' harus salah satu dari {', '.join(STATUSES)}")
        episode = row.get("episode")
        if episode is not None and (not isinstance(episode, int) or episode < 1):
            raise CatalogError(f"{slug}: episode harus angka bulat >= 1")
        kind = row.get("kind", "performance")
        if kind not in KINDS:
            raise CatalogError(f"{slug}: kind '{kind}' harus salah satu dari {', '.join(KINDS)}")
        card = row.get("card", [])
        if not isinstance(card, list) or not all(isinstance(x, str) for x in card):
            raise CatalogError(f"{slug}: card harus daftar teks")
        unconfirmed = row.get("unconfirmed", [])
        if not isinstance(unconfirmed, list) or not all(isinstance(x, str) for x in unconfirmed):
            raise CatalogError(f"{slug}: unconfirmed harus daftar teks")
        known = {"slug", "episode", "name", "city", "song", "artist", "segments", "instagram",
                 "tiktok", "status", "notes", "kind", "hook", "card", "unconfirmed", "full"}
        unknown = set(row) - known
        if unknown:
            raise CatalogError(f"{slug}: kolom tidak dikenal {sorted(unknown)}")
        finalists.append(
            Finalist(
                slug=slug,
                episode=episode,
                name=row.get("name", ""),
                city=row.get("city", ""),
                song=row.get("song", ""),
                artist=row.get("artist", ""),
                segments=segments,
                instagram=row.get("instagram", "").lstrip("@"),
                tiktok=row.get("tiktok", "").lstrip("@"),
                status=status,
                notes=row.get("notes", ""),
                kind=kind,
                hook=row.get("hook", ""),
                card=tuple(card),
                unconfirmed=tuple(unconfirmed),
                full=_segment(row["full"], slug) if "full" in row else None,
            )
        )
    return Catalog(settings=settings, finalists=tuple(finalists))


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    return parse_catalog(path.read_text(encoding="utf-8"))


def slugify(text: str) -> str:
    """"Kartika Idol (1).MP4" stem -> "kartika-idol-1"."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main_checkout() -> Path:
    """Root of the main checkout, also when running inside a git worktree.

    `git rev-parse --git-common-dir` points at the main repo's `.git` from
    any worktree, so media stays in one place for all of them.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=PACKAGE_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(out).parent
    except (OSError, subprocess.CalledProcessError):
        return PACKAGE_DIR.parent


def media_root() -> Path:
    """DOCTOR_IDOL_MEDIA overrides; default is media/doctor-idol-s4 in the main checkout."""
    override = os.environ.get("DOCTOR_IDOL_MEDIA")
    return Path(override) if override else main_checkout() / "media" / "doctor-idol-s4"


def raw_dir(root: Path | None = None) -> Path:
    return (root or media_root()) / "raw"


def reels_dir(slug: str, root: Path | None = None) -> Path:
    return (root or media_root()) / "reels" / slug


def raw_video(slug: str, root: Path | None = None) -> Path | None:
    """The recording for `slug`: raw/<slug>.<any video extension>."""
    folder = raw_dir(root)
    for ext in sorted(VIDEO_EXTENSIONS):
        candidate = folder / f"{slug}{ext}"
        if candidate.exists():
            return candidate
    return None


def unlisted_videos(catalog: Catalog, root: Path | None = None) -> list[Path]:
    """Recordings in raw/ that finalists.toml doesn't mention yet."""
    folder = raw_dir(root)
    if not folder.is_dir():
        return []
    known = {f.slug for f in catalog.finalists}
    return sorted(
        p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS and p.stem not in known
    )


def stub_entry(slug: str) -> str:
    """A [[finalist]] block to append to finalists.toml for a new recording."""
    return (
        "\n[[finalist]]\n"
        f'slug = "{slug}"\n'
        "# episode = 2   # nomor urut posting seri POV Kursi Penonton\n"
        'name = ""        # persis seperti di layar panggung, mis. "dr. Kartika Widyasari"\n'
        'city = ""\n'
        'song = ""\n'
        'artist = ""\n'
        "segments = []    # isi dari: python scripts/doctor_idol.py suggest " + slug + "\n"
        'instagram = ""\n'
        'status = "draft"\n'
    )
