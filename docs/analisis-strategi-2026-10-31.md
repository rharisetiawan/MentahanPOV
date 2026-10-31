# Analisis lengkap & solusi taktis MentahanPOV — 31 Okt 2026

## 1. Kondisi semua sosmed (dicek publik, tanpa login, 31 Okt 2026)

| Platform | Akun | Konten | Follower | Catatan |
|---|---|---|---|---|
| YouTube | Mentahan POV (@MentahanPov) | 97 Shorts | **51** | ±98.400 views total, median 1.100/video |
| Instagram | @mentahanpov | 113 post | **19** | Bio bahasa Inggris, link Drive |
| TikTok | @mentahanpov.id | 96 video | **22** | Total 1.030 like (±11 like/video), bio bahasa Indonesia |
| Facebook | **POV Mentahan** | — | **2** | Nama beda sendiri; website `povmentahan.weverx.com` **mati** (DNS tidak ditemukan) |
| Threads | @mentahanpov | — | — | Baru aktif Okt |

**Total: ±400 posting di semua platform → ±94 follower.**
Views per video IG/FB/TikTok tidak bisa dibaca tanpa login.

### Distribusi views YouTube Shorts
| Rentang | Jumlah video |
|---|---|
| < 500 | 26 |
| 500 – 999 | 12 |
| 1.000 – 1.999 | 53 |
| ≥ 2.000 | 6 |

Median 1.100, sama di paruh video lama dan baru → plafon "kuota uji" Shorts, tidak ada tren naik.
- Terkuat: Icon Bali pagi (3K), motor lewat rumah warna-warni (2,7K), anak melambai ke kereta (2,5K), kolam koi (2,2K), Gereja Kayutangan malam (2,1K), hujan sore ASMR (2,1K).
- Terlemah: halaman kampus sepi, kafe pagi, jalan pinggiran biasa, timelapse panjang (6–270).

## 2. Temuan kritis (bocor di corong)

1. **Folder Drive di bio Instagram minta login Google.** Dibuka tanpa akun → diarahkan ke halaman *Sign in*. Kode (`core/gdrive.py`) hanya membuat **file** publik, bukan folder induknya. Kalau ini benar, janji "100% gratis, download di sini" **tidak bisa dipakai** pengunjung — satu-satunya CTA brand ini putus. *(Mohon cek ulang dari jendela incognito.)*
2. **Instruksi download tidak bisa dijalankan.** Caption: "klik link bio → buka folder X → cari nama file". Tanpa akses folder, tidak bisa. Di Shorts, link di deskripsi tidak bisa diklik.
3. **Identitas tidak konsisten.** YouTube "Mentahan POV", IG "mentahanpov", TikTok "mentahanpov.id", FB "POV Mentahan". Bio IG Inggris, TikTok Indonesia, FB kosong + website mati. Orang yang menemukan satu akun tidak akan mengenali akun lain.
4. **Stories IG/FB otomatis ke 19 dan 2 follower.** Stories hanya tampil ke follower → hampir nol jangkauan, tapi tetap memakan waktu render/upload dan risiko error.
5. **TikTok (tempat budaya "mentahan" Indonesia) diarsipkan**, padahal akunnya sudah ada 96 video.

## 3. Flow yang kamu buat — review

```
Rekam di HP → kirim ke bot Telegram → dry-run (fakta GPS, caption Gemini, upload master ke Drive, render posting copy)
→ pesan konfirmasi [✅ Post | 📅 Jadwalkan | ✏️ Edit | ❌ Batal]
→ posting serentak ke YouTube, FB, IG, FB Story, IG Story (+Threads) → insights 24/72 jam
```

**Bagus:** satu kali kirim langsung tersebar; ada titik review manusia; master tetap bersih; fakta lokasi tidak dikarang AI; paket "2 detik pertama" (mulai dari hook, sound-on, loop, loudnorm) tepat sasaran.

**Masalah dari sisi strategi:**
- Flow mengoptimalkan **kecepatan posting**, bukan **kualitas pilihan**. Semua yang direkam ikut diposting; tidak ada tahap "layak posting atau cukup arsip?".
- **Satu caption Inggris untuk semua platform** (kecuali judul YouTube & Threads). Padahal audiens tiap platform beda.
- **Tidak ada langkah "jual manfaat"**: video tidak pernah menunjukkan *contoh hasil pakai* footage-nya.
- **Corong berakhir di Drive** yang tidak terukur (dan sekarang kemungkinan terkunci).
- Insights hanya dipakai untuk memberi contoh hook ke Gemini, bukan untuk keputusan tema/jadwal/platform.
- Ketergantungan pada box HG680: saat mati (±2–3 minggu, Okt), seluruh posting berhenti.

## 4. Diagnosis inti
Orang **menonton** (YouTube ±1K/video) tapi **tidak ada yang mau mengikuti** (±94 follower dari ±400 posting), dan **tidak bisa mengambil** filenya (Drive terkunci). Sebabnya:
1. Konten dinikmati sebagai tontonan sekali lewat, tidak menunjukkan kenapa harus follow.
2. Bahasa Inggris generik, padahal brand dan pasarnya ("mentahan") Indonesia.
3. Tidak ada seri/janji rutin ("tiap hari 1 mentahan hujan baru").
4. Manfaat utama (download gratis) tidak terlihat dan tidak berfungsi.

## 5. Solusi taktis

### Tahap 0 — Tambal kebocoran (hari ini–3 hari, tanpa coding)
1. **Buka akses folder Drive induk**: Drive → folder → Share → *Anyone with the link → Viewer*. Uji dari incognito.
2. **Satukan identitas**: nama tampilan **MentahanPOV** di semua platform; handle sedapat mungkin sama. Ganti nama Page FB "POV Mentahan" → "MentahanPOV".
3. **Hapus website mati** dari Page FB; ganti dengan satu link yang sama di semua bio.
4. **Bio seragam, bahasa Indonesia**, contoh:
   `🎥 Mentahan video asli Indonesia — gratis, tanpa watermark`
   `🌧️ Hujan · 🌃 Malam · 🛵 Jalanan · 🐦 Alam`
   `📥 Ambil filenya 👇`
5. **Matikan Stories otomatis** (`facebook_story`, `instagram_story` dari `PLATFORMS`) sampai follower IG > ±500.

### Tahap 1 — Ubah posisi & bahasa (minggu 1)
- Positioning: **"Mentahan video gratis dari Indonesia, siap dipakai untuk story, quotes, dan edit CapCut."**
- Prompt Gemini → **bahasa Indonesia**, kata kunci yang diketik orang: *mentahan video, mentahan story WA, mentahan CapCut, video aesthetic, no copyright, hujan, senja, malam*.
- Judul YouTube contoh: `Mentahan video hujan malam Malang | aesthetic no copyright`.
- Tambah **tag mood** di caption: #galau #healing #senja #rindu — orang Indonesia mencari mentahan berdasarkan *perasaan* untuk story, bukan lokasi.
- Caption **pendek**: hook (1 baris) → "Gratis, tanpa watermark — link di bio" → kode file → 5 hashtag.

### Tahap 2 — Format baru (ide fresh)
1. **"Jadi gini ➜ mentahannya"** (paling penting): 3–5 detik pertama footage sudah diedit seperti story WA (teks quote + musik tren), lalu potong ke "👇 ini mentahannya, gratis" dan klip mentah. Menunjukkan manfaat, menarik kreator, memberi alasan follow. Pipeline bisa membuat versi ini otomatis (overlay teks quote dari Gemini).
2. **Musim hujan = momentum sekarang** (Nov–Mar). Seri harian **"Mentahan Hujan #1, #2, …"**. Siapkan stok sekarang.
3. **Kalender konten Indonesia**: Jumat berkah (masjid, adzan — sudah terbukti 1K–1,5K), malam minggu, akhir bulan/gajian, Ramadan (ngabuburit, sahur — siapkan dari Jan/Feb 2027), Natal & Tahun Baru (lampu kota).
4. **Mentahan by request**: "Komen mau mentahan suasana apa, aku rekamkan". Di TikTok, balas dengan fitur *reply with video* → sinyal komentar kuat dan orang kembali.
5. **Seri lokal & kolaborasi**: "Malang 30 detik" (Kayutangan, Alun-alun, Ijen, kereta). Tawarkan footage gratis ke akun info lokal (@infomalang, @malangraya, dll.) dengan kredit @mentahanpov → jangkauan ribuan follower mereka.
6. **Paket mingguan**: "5 mentahan hujan minggu ini" sebagai carousel/compilation, link ke satu folder paket.
7. **Long-form YouTube ambience mingguan** (10–30 menit: "Hujan malam di Malang", "Suara pasar pagi") → jam tonton untuk syarat monetisasi + tujuan "related video" dari Shorts.
8. **Template CapCut** memakai footage sendiri (kalau akun memenuhi syarat creator template) — format yang sangat dicari di Indonesia.

### Tahap 3 — Peran tiap platform
| Platform | Peran | Cara |
|---|---|---|
| **TikTok** | Mesin pertumbuhan utama | Manual 1–2/hari, bahasa Indonesia, sound tren (versi mentah tetap di link), balas komentar dengan video |
| **YouTube** | Pencarian + jam tonton | Shorts dengan judul kata kunci ID + long-form mingguan + playlist per seri |
| **Instagram** | Etalase + kolaborasi | Reels ID, *Collab post* dengan akun lokal, highlight "Cara download" |
| **Threads** | Ngobrol & request | Tanya "mentahan apa minggu depan?" |
| **Facebook** | Bonus otomatis | Biarkan otomatis, tanpa Stories |

### Tahap 4 — Perbaikan flow (coding, urut prioritas)
1. **Prompt bahasa Indonesia + mood tag + caption pendek** — ubah `prompts/sop_v3.txt` (kecil).
2. **Skor kelayakan dari Gemini** (1–10 "potensi berhenti scroll" + alasan) ditampilkan di pesan konfirmasi; skor rendah → tombol "📦 Arsip saja" (upload Drive tanpa posting).
3. **Halaman katalog publik** (statis, dari `state/posts.json`): cari per kategori/mood/lokasi, tombol download langsung, + short link terukur. Menggantikan "cari nama file di folder".
4. **Versi "Jadi gini ➜ mentahannya"** sebagai varian render (teks quote dari Gemini + potong ke klip mentah).
5. **Kit TikTok manual**: bot mengirim balik posting copy + caption TikTok siap copy-paste.
6. **Rekap mingguan di Telegram**: follower per platform, median views 72 jam, video terbaik/terburuk, tema yang menang.
7. **Gabungan long-form mingguan** otomatis dari master seri yang sama.

### Tahap 5 — Cara merekam (sumber kualitas)
- Mulai rekam **saat sudah ada gerakan** (kereta datang, hujan deras, lampu menyala), bukan sebelum.
- Durasi tayang 10–20 detik; rekam 30–60 detik supaya ada pilihan potongan.
- Wajib punya **satu "momen"**: manusia/hewan/perubahan cahaya/suara khas. Pemandangan kosong → arsip saja.
- Prioritaskan yang terbukti: malam + basah/hujan, landmark yang dikenal, momen kecil manusia/hewan, kereta.
- Rekam vertikal 9:16, stabil (pegang dua tangan/gimbal), audio tidak tertutup angin.

## 6. Rencana 30 hari
| Minggu | Kerjakan | Target |
|---|---|---|
| 1 | Tahap 0 semua; prompt bahasa Indonesia; matikan Stories; deploy paket 2 detik; mulai seri **Mentahan Hujan** | Folder bisa dibuka publik; baseline tercatat |
| 2 | TikTok manual 1/hari; 3 video "Jadi gini ➜ mentahannya" (edit manual dulu di CapCut); hubungi 3 akun info lokal | Klik link mulai terhitung |
| 3 | Long-form YouTube pertama; skor kelayakan di bot; konten request pertama | Follower TikTok > 100 |
| 4 | Evaluasi: format mana yang menang → otomasi yang menang saja | Median views naik ≥ 50% di 1 platform; total follower > 300 |

**Angka mingguan:** follower per platform, median views 72 jam, follower per 1K views, klik link bio, jumlah komentar/request.

## 7. Masih perlu dari user
- Views/reach per video IG, FB, TikTok (perlu login — screenshot dari Insights/Professional dashboard, atau login di browser pane).
- YouTube Studio: viewed vs swiped away, sumber traffic (feed vs search).
- Konfirmasi: folder Drive di bio memang terkunci saat dibuka dari incognito?
