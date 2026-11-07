# Doctor Idol Season IV — fancam MentahanPOV (unofficial)

Rekaman live dari kursi penonton di Doctor Idol Competition Season IV,
dipotong jadi reels vertikal untuk IG/FB, TikTok dan YouTube Shorts. Ini
**bukan konten resmi** penyelenggara: semua caption menyebut "fancam dari
kursi penonton, bukan akun resmi Doctor Idol".

Terpisah dari pipeline street-POV (`main.py`, bot Telegram). Alat ini jalan
di laptop (butuh `ffmpeg`/`ffprobe`), tidak di box.

## Letak file

| Apa | Di mana | Git? |
|---|---|---|
| Data finalis (nama, kota, lagu, potongan, status) | `doctor_idol/finalists.toml` | ya |
| Kode | `doctor_idol/*.py`, `scripts/doctor_idol.py` | ya |
| Video mentah | `media/doctor-idol-s4/raw/<slug>.mp4` | tidak |
| Hasil | `media/doctor-idol-s4/reels/<slug>/` : `reels.mp4`, `cover.jpg`, `caption.txt` | tidak |
| Versi YouTube | `media/doctor-idol-s4/youtube/<slug>/` : `video.mp4`, `thumbnail.jpg`, `youtube.txt` | tidak |

`media/` selalu di folder utama MentahanPOV, juga kalau skrip dijalankan dari
git worktree. Ganti lokasinya dengan env `DOCTOR_IDOL_MEDIA`.

## Alur untuk semua finalis

1. **Taruh video** ke `media/doctor-idol-s4/raw/`. Nama file bebas.
2. **`python scripts/doctor_idol.py scan --write`** mengganti nama file jadi
   slug (`Kartika Idol.mp4` → `kartika-idol.mp4`) dan menambah blok kosong ke
   `finalists.toml`.
3. **Isi data** di `finalists.toml`: nama persis seperti di layar panggung,
   kota, judul lagu, penyanyi asli, akun IG kalau ada. Claude bisa membantu
   membaca nama dari frame layar panggung.
4. **`python scripts/doctor_idol.py suggest --all`** memberi 3 potongan
   30 detik paling semarak per video (ukuran loudness EBU R128; awal 15 detik
   dan akhir 10 detik dilewati karena biasanya MC/tepuk tangan). Pilih satu,
   atau gabung dua potongan (`[[198, 222.8], [268, 279]]`). Tetap cek pakai
   telinga: tepuk tangan juga keras.
5. **`python scripts/doctor_idol.py render --all`** membuat `reels.mp4`, `cover.jpg` dan
   `caption.txt` per finalis. Yang sudah ada dan lebih baru dari video +
   `finalists.toml` dilewati; `--force` untuk render ulang.
6. **`python scripts/doctor_idol.py status`** menampilkan siapa yang belum
   punya video, potongan, data, atau reel. Ubah `status` jadi `approved` /
   `posted` sendiri di `finalists.toml`.

`captions <slug>` menulis ulang caption saja (mis. setelah akun IG diisi).

### Rekaman 6 Nov 2026 (sudah ditata)

- `raw/finalis-01.mp4` … `finalis-15.mp4` = 15 finalis sesuai **urutan tampil**;
  nama file kamera asli (`VID_20261106_…`) tercatat di `notes`.
- `raw/pengumuman-juara.mp4` = `VID_…213708` (pengumuman) + `VID_…214216`
  (para juara memegang papan) digabung tanpa re-encode.
- `extra/` = klip lain (jeda, komentar juri, MC sebelum pengumuman), file
  asli pengumuman, dan salinan 480p lama Kartika.
- Nama bertanda **"(?)"** berasal dari suara MC dan ejaannya belum pasti.
  Tanda itu sengaja ikut tampil di video dan cover sampai dicek. Yang kosong
  tampil sebagai "[nama finalis]" di cover dan caption.

### Reel pengumuman juara

Entri dengan `kind = "announcement"` memakai gaya yang sama, dengan isi lain:
`hook` sendiri ("POV: kamu di kursi penonton saat juara diumumkan"), kartu
`card` berisi daftar juara dengan ikon piala (tampil lebih lama sesuai
jumlah baris), penutup `verdict_text` ("Setuju sama juri?"), dan caption
yang mendaftar para juara.

## Ciri khas: seri "POV Kursi Penonton"

Semua finalis memakai gaya yang sama supaya langsung dikenali sebagai
MentahanPOV di feed dan grid profil. Prinsipnya tetap *mentah*: tanpa
color grading, filter, musik tambahan, atau subtitle lirik. Suara asli
penonton dan tepuk tangan dibiarkan. Yang ditambahkan hanya bingkai brand.

| Detik | Elemen | Kenapa |
|---|---|---|
| 0 – 2,5 | Pill tengah 👁 **"POV: kamu di kursi penonton"** | Nama brand jadi kalimat pertama yang dibaca; format POV asli TikTok; sekaligus jujur bahwa ini rekaman penonton |
| 2,5 – 6,5 | Kartu kiri: 📍 nama · kota, 🔊 lagu · penyanyi | Konteks tanpa harus baca caption; teks di layar ikut dibaca pencarian TikTok/IG |
| 4 detik terakhir | Pill tengah 💬 **"Skor kamu berapa? 1-10"** | Komentar adalah sinyal ranking; skor mengundang orang berdebat |
| sepanjang video | Garis ungu brand di tepi atas, terisi sampai selesai | Penanda visual seri + memperlihatkan sisa durasi (membantu ditonton sampai habis) |
| sepanjang video | Logo POV kanan atas, 216 px, 92% + bayangan | Tengah layar = penyanyi, jadi logo tidak di tengah seperti street-POV |
| cover | Frame dari reel + pill ungu **"POV KURSI PENONTON #n"**, nama besar, kota · lagu | Grid IG terbaca sebagai satu seri bernomor; teks di area 3:4 yang tampil di grid |

Caption: baris pertama "POV: kamu di kursi penonton, dokter dari <kota> ini
naik panggung", baris kedua nama seri + nomor, hashtag `#POVKursiPenonton
#MentahanPOV`. Judul Shorts diawali "POV Kursi Penonton #n:".

Semua caption memuat catatan fancam dan catatan keberatan ("DM Instagram
@mentahanpov, akan kami hapus"). Kota/lagu/penyanyi yang kosong atau bertanda
"(?)" tidak jadi placeholder: kalimatnya menyesuaikan dan muncul disclaimer
"... belum kami pastikan". Rincian lain ditambahkan lewat kolom `unconfirmed`
(mis. `["gelar"]`). Nama dan akun IG yang kosong tetap jadi `[placeholder]`.

Teknis: 1080×1920, 30 fps, H.264 + AAC, -14 LUFS; potongan disambung
crossfade 0,3 detik; rekaman yang tidak 9:16 diletakkan di atas versi blur
dirinya, tidak dicrop. Semua teks dan durasi ada di `[settings]` pada
`finalists.toml` (kosongkan teks / `false` untuk mematikan elemen).
`episode` = nomor urut posting, bukan urutan tampil di panggung.

## Versi panjang YouTube

`python scripts/doctor_idol.py youtube --all` (atau `youtube <slug>`, `--force`
untuk render ulang) membuat penampilan utuh, sekitar 5 menit render per video.
Video yang lebih baru dari rekaman mentah dan `finalists.toml` dilewati, jadi
perintah yang sama bisa diulang kalau terhenti.

- 1920×1080, video vertikal utuh di tengah di atas versi blur dirinya. Sengaja
  16:9: video vertikal sampai 3 menit akan diperlakukan YouTube sebagai Shorts.
- Bingkai sama dengan reel (hook, kartu nama, logo, garis progres, ajakan skor),
  tanpa musik atau subtitle lirik, -14 LUFS.
- Kolom `full = [mulai, selesai]` memotong rekaman kalau ujungnya tidak layak
  (dipakai finalis-07 dan finalis-14). Tanpa kolom ini, seluruh rekaman dipakai.
- `youtube.txt`: judul + deskripsi untuk ditempel, lalu catatan upload yang
  tidak ikut ditempel (monetisasi nonaktif, Unlisted dulu, cek Content ID).

## Aturan karena ini unofficial

- Jangan pakai logo/nama Doctor Idol sebagai identitas akun, dan jangan
  mengaku penyelenggara. Hashtag `#DoctorIdol` boleh.
- User sudah meminta izin finalis saat merekam (6 Nov 2026) untuk reels pendek.
  Tag / undang collab saat posting. Kalau finalis atau penyelenggara minta
  turun, turunkan dari semua platform; pantau DM selama seri tayang.
- Versi panjang (penampilan utuh) belum tentu tercakup izin itu: pastikan dulu.
- Lagu cover berhak cipta: posting lewat Reels/TikTok (lisensi musik
  platform), jangan dimonetisasi di YouTube; Content ID bisa memblokir atau
  membisukan. Lirik tidak dijadikan subtitle.
- Rekaman asli disimpan, jangan dihapus, untuk bukti kalau ada klaim.
