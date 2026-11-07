"""Captions for each platform, written next to the reel as caption.txt.

These are fan recordings from the audience, not the organiser's content,
so every caption says so plainly (FANCAM_NOTE). That keeps the account
honest with viewers and with the organiser, and makes it clear we are not
posting on their behalf. Name, city and song come from finalists.toml.

A missing name or Instagram handle shows up as a [bracketed placeholder] so
it can't slip out unnoticed. A missing or "(?)" city, song or artist may be
posted: the sentence reads without it and a disclaimer says what is unverified.
"""

from __future__ import annotations

import re

from doctor_idol.catalog import Finalist, Settings

FANCAM_NOTE = "🎥 Fancam dari kursi penonton, bukan akun resmi Doctor Idol."
# Names Instagram explicitly because the same text goes to YouTube, which has no DMs.
TAKEDOWN_NOTE = "🙏 Keberatan dengan video ini? DM Instagram @mentahanpov, akan kami hapus."


def _city_tag(city: str) -> str:
    word = re.sub(r"[^0-9A-Za-z]", "", city.title())
    return f"#Dokter{word}" if word else ""


def _hashtags(finalist: Finalist, *, extra: tuple[str, ...] = ()) -> str:
    tags = ["#POVKursiPenonton", "#MentahanPOV", "#DoctorIdol", "#DokterIndonesia",
            _city_tag(finalist.city), *extra]
    return " ".join(t for t in tags if t)


def _or(value: str, placeholder: str) -> str:
    return value or f"[{placeholder}]"


def _disclaimer(finalist: Finalist) -> str:
    """"ℹ️ Kota dan judul lagu belum kami pastikan. ..." or "" when everything is verified."""
    items = finalist.uncertain()
    if not items:
        return ""
    listed = items[0] if len(items) == 1 else f"{', '.join(items[:-1])} dan {items[-1]}"
    return f"ℹ️ {listed[0].upper()}{listed[1:]} belum kami pastikan. Tahu infonya? Bantu koreksi di komentar ya 🙏"


def _performance(finalist: Finalist, who: str) -> str:
    """The "who sang what" sentence, leaving out whatever is not known."""
    if not finalist.song:
        return f"{who}."
    credit = f" ({finalist.artist})" if finalist.artist else ""
    return f"{who}, membawakan \"{finalist.song}\"{credit}."


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
        TAKEDOWN_NOTE,
        "",
        tags,
    ])
    shorts = "\n".join([
        f"Judul: {series}: {title} {settings.event} {settings.season} 🏆 #Shorts #DoctorIdol",
        "Deskripsi: " + "; ".join(finalist.card),
        FANCAM_NOTE,
        TAKEDOWN_NOTE,
    ])
    tiktok_text = (
        f"POV: kamu di kursi penonton waktu juara Doctor Idol diumumkan 🏆 Setuju sama juri? "
        f"{FANCAM_NOTE} {TAKEDOWN_NOTE} {tags} #fyp"
    )
    return (
        "=== INSTAGRAM REELS / FACEBOOK ===\n" + instagram
        + "\n\n=== YOUTUBE SHORTS ===\n" + shorts
        + "\n\n=== TIKTOK ===\n" + tiktok_text + "\n"
    )


def youtube_title(finalist: Finalist, settings: Settings) -> str:
    """"<lagu> - <nama> | Doctor Idol Competition Season IV (Fancam)", within YouTube's 100 characters."""
    name = finalist.name or "[nama finalis]"
    lead = f"{finalist.song} - {name}" if finalist.song and finalist.kind != "announcement" else name
    title = f"{lead} | {settings.event} {settings.season} (Fancam)"
    if len(title) > 100:
        title = f"{lead} | Doctor Idol S4 (Fancam)"
    return title[:100]


def build_youtube_text(finalist: Finalist, settings: Settings) -> str:
    """Title + description to paste, then upload notes that are for us, not for viewers."""
    if finalist.kind == "announcement":
        body = [f"Pengumuman juara {settings.event} {settings.season}, rekaman utuh dari kursi penonton.", "",
                *(f"🏆 {line}" for line in finalist.card)]
        tags = "#POVKursiPenonton #MentahanPOV #DoctorIdol"
    else:
        name = _or(finalist.name, "nama finalis")
        origin = f" dari {finalist.city}" if finalist.city else ""
        who = f"{name}{origin}, finalis {settings.event} {settings.season}"
        ig = f"@{finalist.instagram}" if finalist.instagram else "[akun IG finalis]"
        body = [_performance(finalist, who), "Penampilan utuh, direkam dari kursi penonton.", "",
                f"Instagram finalis: {ig}", "", "Kalau kamu jurinya, skornya berapa dari 10? Tulis di komentar 👇"]
        tags = _hashtags(finalist, extra=("#Fancam",))
    notes = [note for note in (_disclaimer(finalist), FANCAM_NOTE, TAKEDOWN_NOTE) if note]
    upload = [
        "- Thumbnail: thumbnail.jpg",
        "- Monetisasi: nonaktif. Audiens: bukan konten anak.",
        f"- Playlist: {settings.series}: Doctor Idol {settings.season}",
        "- Hubungkan Short-nya ke video ini lewat \"related video\".",
        "- Unggah sebagai Unlisted dulu, cek klaim Content ID di YouTube Studio, baru jadikan Public.",
    ]
    return "\n".join([
        "=== YOUTUBE ===",
        f"Judul: {youtube_title(finalist, settings)}",
        "Deskripsi:",
        *body, "", *notes, "", tags,
        "", "=== CATATAN UPLOAD (jangan ditempel) ===", *upload, "",
    ])


def build_captions(finalist: Finalist, settings: Settings) -> str:
    if finalist.kind == "announcement":
        return announcement_captions(finalist, settings)
    name = _or(finalist.name, "nama finalis")
    ig = f"@{finalist.instagram}" if finalist.instagram else "[akun IG finalis]"
    tiktok = f"@{finalist.tiktok}" if finalist.tiktok else ig
    origin = f" dari {finalist.city}" if finalist.city else ""
    who = f"{name}{origin}, finalis {settings.event} {settings.season}"
    performance = _performance(finalist, who)
    series = f"{settings.series} #{finalist.episode}" if finalist.episode else settings.series
    disclaimer = _disclaimer(finalist)
    notes = [note for note in (disclaimer, FANCAM_NOTE, TAKEDOWN_NOTE) if note]

    instagram = "\n".join([
        f"POV: kamu di kursi penonton, dokter{origin} ini naik panggung 🎤✨",
        f"{series}",
        "",
        f"{performance} {ig}",
        "",
        "Kalau kamu jurinya, skornya berapa dari 10? Tulis di komentar 👇",
        "",
        *notes,
        "",
        _hashtags(finalist, extra=("#Fancam",)),
        "",
        f"Collab: undang {ig} sebagai collaborator (opsional).",
    ])
    shorts = "\n".join([
        f"Judul: {series}: dokter{origin} naik panggung, begini suaranya 🎤 #Shorts #DoctorIdol",
        f"Deskripsi: {performance}",
        *notes,
    ])
    tiktok_text = (
        f"POV: kamu di kursi penonton, dokter{origin} ini naik panggung 🎤 {tiktok} "
        f"Skornya berapa dari 10? {' '.join(notes)} "
        + _hashtags(finalist, extra=("#Fancam", "#fyp"))
    )
    return (
        "=== INSTAGRAM REELS / FACEBOOK ===\n" + instagram
        + "\n\n=== YOUTUBE SHORTS ===\n" + shorts
        + "\n\n=== TIKTOK ===\n" + tiktok_text + "\n"
    )
