"""Captions for each platform, written next to the reel as caption.txt.

These are fan recordings from the audience, not the organiser's content,
so every caption says so plainly (FANCAM_NOTE). That keeps the account
honest with viewers and with the organiser, and makes it clear we are not
posting on their behalf. Name, city and song come from finalists.toml;
anything missing shows up as a [bracketed placeholder] so it can't slip
out unnoticed.
"""

from __future__ import annotations

import re

from doctor_idol.catalog import Finalist, Settings

FANCAM_NOTE = "🎥 Fancam dari kursi penonton, bukan akun resmi Doctor Idol."


def _city_tag(city: str) -> str:
    word = re.sub(r"[^0-9A-Za-z]", "", city.title())
    return f"#Dokter{word}" if word else ""


def _hashtags(finalist: Finalist, *, extra: tuple[str, ...] = ()) -> str:
    tags = ["#POVKursiPenonton", "#MentahanPOV", "#DoctorIdol", "#DokterIndonesia",
            _city_tag(finalist.city), *extra]
    return " ".join(t for t in tags if t)


def _or(value: str, placeholder: str) -> str:
    return value or f"[{placeholder}]"


def build_captions(finalist: Finalist, settings: Settings) -> str:
    name = _or(finalist.name, "nama finalis")
    city = _or(finalist.city, "kota")
    song = _or(finalist.song, "judul lagu")
    artist = _or(finalist.artist, "penyanyi asli")
    ig = f"@{finalist.instagram}" if finalist.instagram else "[akun IG finalis]"
    tiktok = f"@{finalist.tiktok}" if finalist.tiktok else ig
    who = f"{name} dari {city}, finalis {settings.event} {settings.season}"
    series = f"{settings.series} #{finalist.episode}" if finalist.episode else settings.series

    instagram = "\n".join([
        f"POV: kamu di kursi penonton, dokter dari {city} ini naik panggung 🎤✨",
        f"{series}",
        "",
        f"{who}, membawakan \"{song}\" ({artist}). {ig}",
        "",
        "Kalau kamu jurinya, skornya berapa dari 10? Tulis di komentar 👇",
        "",
        FANCAM_NOTE,
        "",
        _hashtags(finalist, extra=("#Fancam",)),
        "",
        f"Collab: undang {ig} sebagai collaborator (opsional, setelah izin).",
    ])
    shorts = "\n".join([
        f"Judul: {series}: dokter dari {city} naik panggung, begini suaranya 🎤 #Shorts #DoctorIdol",
        f"Deskripsi: {who}, membawakan \"{song}\" ({artist}).",
        FANCAM_NOTE,
    ])
    tiktok_text = (
        f"POV: kamu di kursi penonton, dokter dari {city} ini naik panggung 🎤 {tiktok} "
        f"Skornya berapa dari 10? {FANCAM_NOTE} "
        + _hashtags(finalist, extra=("#Fancam", "#fyp"))
    )
    return (
        "=== INSTAGRAM REELS / FACEBOOK ===\n" + instagram
        + "\n\n=== YOUTUBE SHORTS ===\n" + shorts
        + "\n\n=== TIKTOK ===\n" + tiktok_text + "\n"
    )
