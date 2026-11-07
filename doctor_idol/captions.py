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


def announcement_captions(finalist: Finalist, settings: Settings) -> str:
    series = f"{settings.series} #{finalist.episode}" if finalist.episode else settings.series
    winners = "\n".join(f"🏆 {line}" for line in finalist.card) or "[daftar juara]"
    tags = "#POVKursiPenonton #MentahanPOV #DoctorIdol #DokterIndonesia #Fancam"
    title = finalist.name or "Pengumuman juara"
    instagram = "\n".join([
        f"POV: kamu di kursi penonton waktu juara {settings.event} {settings.season} diumumkan 🏆",
        series,
        "",
        winners,
        "",
        "Setuju sama juri? Siapa jagoanmu? Tulis di komentar 👇",
        "",
        FANCAM_NOTE,
        "",
        tags,
    ])
    shorts = "\n".join([
        f"Judul: {series}: {title} {settings.event} {settings.season} 🏆 #Shorts #DoctorIdol",
        "Deskripsi: " + "; ".join(finalist.card),
        FANCAM_NOTE,
    ])
    tiktok_text = (
        f"POV: kamu di kursi penonton waktu juara Doctor Idol diumumkan 🏆 Setuju sama juri? "
        f"{FANCAM_NOTE} {tags} #fyp"
    )
    return (
        "=== INSTAGRAM REELS / FACEBOOK ===\n" + instagram
        + "\n\n=== YOUTUBE SHORTS ===\n" + shorts
        + "\n\n=== TIKTOK ===\n" + tiktok_text + "\n"
    )


def build_captions(finalist: Finalist, settings: Settings) -> str:
    if finalist.kind == "announcement":
        return announcement_captions(finalist, settings)
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
