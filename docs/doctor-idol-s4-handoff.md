# Serah-terima: Doctor Idol Season IV (fancam MentahanPOV, unofficial)

Status per 7 Nov 2026. Dokumen ini cukup untuk melanjutkan tanpa riwayat
percakapan sebelumnya. Detail cara pakai alat ada di `doctor_idol/README.md`.

> **Pembaruan 8 Nov 2026 (mengalahkan bagian di bawah bila berbeda).**
> Pekerjaan berlanjut di branch `claude/doctor-idol-s4-fancam-fdc89c`, worktree
> `.claude/worktrees/doctor-idol-s4-fancam-fdc89c`; lihat `logs/2026-11-08.md`
> untuk status terbaru. 16 reel dan 16 versi YouTube sudah jadi dan siap
> diunggah manual oleh user. Versi YouTube ada di
> `media/doctor-idol-s4/youtube/<slug>/`. Semua finalis `approved`, bernomor
> episode, lagu dan kota lengkap. Satu keraguan tersisa: nama band finalis-10
> "This Is Joy (?)", ditutup disclaimer caption. Lirik live bisa dibaca Whisper
> kalau audio disaring ke rentang vokal (`highpass=180,lowpass=3800,dynaudnorm`)
> tanpa VAD.

## Apa ini

User (Hari, pemilik MentahanPOV) merekam live dari kursi penonton semua
penampilan **Doctor Idol Competition Season IV** (Jawa Pos Health & Lifestyle
Expo 2026, Grand Atrium Pakuwon Mall Surabaya, 6 Nov 2026). Rekaman dipotong
jadi reels vertikal untuk IG/FB, TikTok, YouTube Shorts.

- Ini **bukan konten resmi** penyelenggara. Setiap caption wajib memuat
  "🎥 Fancam dari kursi penonton, bukan akun resmi Doctor Idol." (sudah otomatis).
- Terpisah dari pipeline street-POV utama (bot Telegram di box). Alat ini
  jalan di laptop Windows user, butuh `ffmpeg`/`ffprobe` dan Python 3.11+.

## Di mana semuanya

| Apa | Lokasi |
|---|---|
| Kode + data (git) | branch `claude/doctor-idol-discussion-bb1625`, worktree `MentahanPOV/.claude/worktrees/doctor-idol-discussion-bb1625` |
| Data finalis | `doctor_idol/finalists.toml` |
| Kode | `doctor_idol/` (catalog, highlights, render, cover, captions), CLI `scripts/doctor_idol.py` |
| Tes | `tests/test_doctor_idol.py` (25 tes, `python -m unittest tests.test_doctor_idol`) |
| Video mentah (tidak di git) | `C:\Users\user\Desktop\Claude Project\MentahanPOV\media\doctor-idol-s4\raw\` |
| Hasil (tidak di git) | `...\media\doctor-idol-s4\reels\<slug>\` berisi `reels.mp4`, `cover.jpg`, `caption.txt` |
| Klip lain | `...\media\doctor-idol-s4\extra\` (jeda, komentar juri, file asli pengumuman, Kartika 480p lama) |

**Penting:** branch ini belum di-push dan belum di-merge. Folder utama
`MentahanPOV` ada di branch `devin/1777731695-init-mentahanpov` dan **belum
punya** folder `doctor_idol/`. Kerjakan dari worktree di atas, atau merge /
checkout branch `claude/doctor-idol-discussion-bb1625` dulu. `media/` selalu
dibaca dari folder utama, dari worktree mana pun (lewat `git rev-parse
--git-common-dir`), dan bisa diganti dengan env `DOCTOR_IDOL_MEDIA`.

## Perintah

```bash
python scripts/doctor_idol.py status           # siapa yang kurang data / belum dirender
python scripts/doctor_idol.py render --all     # render yang berubah (reels + cover + caption)
python scripts/doctor_idol.py render finalis-04 --force
python scripts/doctor_idol.py captions --all   # tulis ulang caption saja
python scripts/doctor_idol.py suggest finalis-04   # usulan potongan 30 dtk (loudness)
```

Di Windows, set `PYTHONIOENCODING=utf-8` kalau output emoji error.
Render ~1 menit per reel. Setelah ubah kode: `graphify update .`.

## Gaya baku (disetujui user): seri "POV Kursi Penonton"

Footage tetap mentah (tanpa grading, filter, musik tambahan, subtitle lirik).
Yang ditambahkan per reel, 1080x1920, 30 fps, -14 LUFS:

- 0–2,5 dtk: pill tengah "POV: kamu di kursi penonton" (ikon mata)
- 2,5–6,5 dtk: kartu kiri "nama · kota" + "lagu · penyanyi"
- 4 dtk terakhir: pill "Skor kamu berapa? 1-10"
- garis progres ungu brand (#8455E8) di tepi atas; logo POV kanan atas
- potongan disambung crossfade 0,3 dtk
- `cover.jpg`: pill ungu "POV KURSI PENONTON #n", nama besar, kota · lagu
- caption: baris 1 hook POV, baris 2 nama seri + nomor, tag akun IG finalis,
  hashtag `#POVKursiPenonton #MentahanPOV #DoctorIdol ...`, catatan fancam
- reel pengumuman (`kind = "announcement"`): hook "…saat juara diumumkan",
  kartu piala daftar juara, penutup "Setuju sama juri?"

Semua teks/durasi bisa diatur di `[settings]` pada `finalists.toml`.

## Data 15 finalis (urutan tampil)

Sumber nama/kota/akun: halaman "Pantau Doctor Idol"
(claude.ai/artifact/HemGx2rGrCDgovyxvrGfzJ) + reel IG tiap finalis. Pencocokan
video → nama lewat papan juara, panggilan MC, layar LED, dan wajah di reel IG.
**Semua 15 nama sudah dikonfirmasi user.**

| Slug | Nama | Kota | IG | Lagu | Catatan |
|---|---|---|---|---|---|
| finalis-01 | dr. Verina Setyabudhi, Sp.PD | Malang | doktervee | ? | 🥇 Juara 1 |
| finalis-02 | dr. Ahmad Iqbal Romdloni | Pasuruan | iqbalromdloni | ? | |
| finalis-03 | dr. Aldhi Wimandra | ? | aldhiwimandra | Juwita Malam (Ismail Marzuki) | |
| finalis-04 | dr. Natasya Febrilia Yulianti | Jember | natasyafebrilia | Sway (?) (The Pussycat Dolls) | ❤️ Most Loved Doctor |
| finalis-05 | drg. Iga Tri Budiarti | ? | iga.tribudiarti | ? | |
| finalis-06 | dr. Andre Saputra | Sidoarjo | andsput | ? | |
| finalis-07 | dr. Ni Ketut Putri Ayu Puspita Dewi | Surabaya | putriayupus | ? | |
| finalis-08 | dr. Anre Hernadia Inas, Dipl. AAAM | Kediri | hernadiaines | ? | |
| finalis-09 | dr. Anggindita Diah Widihidayati | Surabaya | angginditadw | Lihatlah Lebih Dekat (Sherina) | |
| finalis-10 | dr. Almer Deta Tarandha | ? | almerdt | ? | |
| finalis-11 | drg. Revian Gigih Laksono, MPH | Surabaya | revianglaks | ? | 🥈 Juara 2 |
| finalis-12 | dr. Abdurachman Omar Baabdullah, Sp.PD | Malang | abdurbaabdullah | ? | 🥉 Juara 3, rekaman 4K |
| finalis-13 | Haris Rizky Minata | ? | harisrizkymnt | ? | gelar belum diketahui |
| finalis-14 | dr. Kartika Widyasari | Malang | kartikawyd | When I Look at You (Miley Cyrus) | episode 1, prototipe gaya |
| finalis-15 | dr. Dhita Budi Wibowo, Sp.An-TI | Yogyakarta | dhitabw | Mengejar Matahari (Ari Lasso) | finalis terakhir |
| pengumuman-juara | (reel pengumuman) | | | | VID_213708 + VID_214216 digabung |

Potongan (`segments`) tiap finalis sudah dipilih dari timeline thumbnail
tiap 5 detik: bagian close-up + klimaks, menghindari saat kamera berpaling.
Semua 16 reel sudah dirender dan dicek per frame (30–33 dtk, pengumuman
42,7 dtk, -13,7..-14,4 LUFS).

## Yang belum selesai

1. **Judul lagu** 10 finalis (`?` di tabel) dan konfirmasi "Sway (?)"
   finalis-04. Whisper tidak bisa membaca lirik live (musik + sorakan keras).
   Isi `song` + `artist` di `finalists.toml`, lalu `render --all`.
2. **Kota** finalis-03, 05, 10, 13; gelar Haris Rizky Minata.
3. **Episode** (nomor urut posting) per finalis: `episode = n`. Baru Kartika = 1.
   Nomor tampil di cover dan caption.
4. **Izin finalis** sebelum posting (DM cukup), lalu ubah `status` ke
   `approved` → `posted`.
5. Opsional: push/merge branch `claude/doctor-idol-discussion-bb1625`.

## Aturan yang harus dijaga

- Jangan hapus/ubah catatan fancam di caption; jangan pakai logo Doctor Idol
  sebagai identitas akun; jangan mengaku penyelenggara.
- Lagu cover berhak cipta: posting lewat Reels/TikTok, jangan monetisasi di
  YouTube, jangan jadikan lirik subtitle.
- Video (`media/`) tidak masuk git. Rekaman asli jangan dihapus.
- Nama yang belum pasti diberi akhiran "(?)" di `name`; tanda itu sengaja
  ikut tampil di video supaya tidak lolos ke posting.
- Bahasa kerja dengan user: Bahasa Indonesia. Log sesi ke `logs/YYYY-MM-DD.md`.
