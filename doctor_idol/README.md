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
| Hasil | `media/doctor-idol-s4/reels/<slug>/reels.mp4` + `caption.txt` | tidak |

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
5. **`python scripts/doctor_idol.py render --all`** membuat `reels.mp4` dan
   `caption.txt` per finalis. Yang sudah ada dan lebih baru dari video +
   `finalists.toml` dilewati; `--force` untuk render ulang.
6. **`python scripts/doctor_idol.py status`** menampilkan siapa yang belum
   punya video, potongan, data, atau reel. Ubah `status` jadi `approved` /
   `posted` sendiri di `finalists.toml`.

`captions <slug>` menulis ulang caption saja (mis. setelah akun IG diisi).

## Gaya video (sama untuk semua finalis)

- 1080×1920, 30 fps, H.264 + AAC, loudness -14 LUFS.
- Potongan disambung crossfade 0,3 detik (`transition_s`).
- Logo POV pojok kanan atas, 216 px, opasitas 92% + bayangan. Bukan di tengah
  seperti pipeline street-POV karena tengah layar = penyanyi.
- Kartu nama di kiri atas 4 detik pertama: "nama · kota" dan "lagu · penyanyi".
- Rekaman yang tidak 9:16 diletakkan di atas versi blur dirinya, tidak dicrop.

Semua angka ada di `[settings]` pada `finalists.toml`.

## Aturan karena ini unofficial

- Jangan pakai logo/nama Doctor Idol sebagai identitas akun, dan jangan
  mengaku penyelenggara. Hashtag `#DoctorIdol` boleh.
- Minta izin finalis sebelum posting (DM cukup), lalu tag / undang collab.
  Kalau finalis atau penyelenggara minta turun, turunkan.
- Lagu cover berhak cipta: posting lewat Reels/TikTok (lisensi musik
  platform), jangan dimonetisasi di YouTube; Content ID bisa memblokir atau
  membisukan. Lirik tidak dijadikan subtitle.
- Rekaman asli disimpan, jangan dihapus, untuk bukti kalau ada klaim.
