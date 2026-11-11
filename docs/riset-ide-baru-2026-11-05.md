# Riset ide baru MentahanPOV — 5 Nov 2026

Lanjutan dari `docs/analisis-strategi-2026-10-31.md`. Catatan: sebagian besar sumber di bawah adalah
blog pihak ketiga, bukan dokumen resmi platform — angka dianggap arah, bukan kepastian.

## A. Apa yang berubah di platform (relevan untuk MentahanPOV)

| Temuan | Artinya untuk MentahanPOV |
|---|---|
| **YouTube 1 Okt 2026: Shorts memprioritaskan konten orisinal**, channel yang re-upload video orang lain dipotong distribusinya. | Kita 100% rekaman sendiri → **diuntungkan**. Tapi tetap hindari klip yang terlihat seragam/massal. |
| **Instagram (seri Mosseri Sep 2026): orisinalitas salah satu dari 4 prinsip ranking**; akun kecil diuji ke non-follower; sinyal utama watch time, likes, *sends*. | Sama: orisinal = plus. *Sends* (dibagikan via DM) penting → konten yang "dikirim ke teman" (lucu/indah/nostalgia) menang. |
| **Instagram Trial Reels**: Reel dikirim ke non-follower dulu (24–72 jam), bisa dipindah ke profil kalau bagus. | Uji hook/tema tanpa "mengotori" grid. Cek apakah akun memenuhi syarat & apakah Graph API mendukungnya. |
| **TikTok: Search setara For You**, tidak bergantung follower; TikTok membaca **teks di layar, ucapan, dan 50 karakter pertama caption**. Ada **Creator Search Insights** (gratis) untuk lihat kata yang dicari tapi videonya sedikit. | Kata kunci harus **tertulis di video** (bukan hanya caption). Halaman TikTok "video mentahan buat story" memang ada → permintaan nyata. |
| **YouTube Shorts Remix** (Green Screen, Cut, Collab, Sound — dan AI remix yang sedang diuji) **otomatis menautkan balik ke Short asli**. | Kreator bisa **remix langsung** footage kita tanpa download → setiap pemakaian = link balik + bukti sosial. Jangan matikan remix. |
| **Related video link di Shorts** → konversi rendah (<1% s.d. ~10%); **komentar tersemat dengan CTA 1–3%**. RPM Shorts ±$0,03–0,10 vs long-form ±$3–8 per 1.000 views. | Uang ada di long-form; Shorts = pintu masuk. Pasang related link ke video ambience panjang + komentar tersemat. |
| **Kreator template CapCut bisa full-time** (mis. Top Creator CapCut 2026: template sinematik & quotes). CapCut sendiri punya halaman "mentahan video polos". | Template CapCut berisi footage kita = format yang sangat dicari kreator Indonesia. |
| **WhatsApp**: 65% penetrasi di Indonesia; Channels = siaran satu arah + polling. | Kanal "paket mentahan mingguan" + polling request. |
| **Pinterest**: pengguna Indonesia pakai untuk inspirasi; video "aesthetic" disukai; traffic bertahan berbulan-bulan. | Platform yang belum dipakai, cocok untuk "aesthetic background", dan pin bisa ditautkan ke s.id. |
| **Microstock (Pond5 40–60%, Adobe Stock ±35%, Shutterstock)** menerima footage HP bila 4K, stabil, minim noise (perlu cek syarat resmi). | Jalur pendapatan dari footage yang sudah ada. |

## B. Ide, urut prioritas

### Cepat (tanpa coding, minggu ini)
1. **"Remix langsung dari Short ini"** — tambahkan ajakan di caption/komentar: "Bisa langsung *Remix → Green Screen* dari Shorts ini 🎬". Remix tidak perlu download, otomatis memberi link balik ke kita, dan muncul sebagai "dibuat dengan" di Short asli. Cek di YouTube Studio bahwa remix tidak dimatikan; di TikTok pastikan *Stitch/Duet/Use this video* aktif; di IG pastikan *Remix* aktif.
2. **Creator Search Insights TikTok** — buka 10 menit/minggu, catat 5 kata "mentahan …" yang banyak dicari tapi videonya sedikit → jadi daftar rekaman minggu itu.
3. **Trial Reels IG** untuk 2–3 video/minggu dengan hook berbeda dari tema yang sama → yang menang dipindah ke profil.
4. **Komentar tersemat di YouTube** pada Short yang sedang naik: "📥 Download gratis: s.id/mentahanpov · Remix langsung juga boleh 🙌" (CTA komentar konversinya lebih tinggi dari related link).
5. **Saluran WhatsApp "MentahanPOV"** — tiap Jumat: "5 mentahan minggu ini" + polling "minggu depan mau suasana apa?". Link-nya ditaruh di s.id sebagai link kedua.

### Kecil (coding ringan di pipeline)
6. **Teks kata kunci di layar**: kartu yang sudah ada (📍 lokasi + Sound on) ditambah satu baris dari Gemini, mis. "mentahan hujan malam" — TikTok/IG membaca teks di layar untuk search. Pakai `render_overlay_card` yang sudah ada.
7. **Caption TikTok khusus**: 50 karakter pertama = frasa pencarian ("Mentahan video hujan malam Malang 🌧️ …"), dikirim bot sebagai "kit TikTok" siap copy-paste.
8. **Ajakan remix di caption** (baris tetap di template, seperti baris tag).
9. **Skor kelayakan + "📦 Arsip saja"** (dari laporan sebelumnya) — sekarang lebih penting karena platform menghukum konten massal yang seragam.

### Taruhan lebih besar (1–4 minggu)
10. **Long-form ambience mingguan** (10–30 menit, mis. "Hujan malam di Malang") gabungan master dari seri yang sama → jam tonton (syarat monetisasi 4.000 jam) dan RPM long-form ~50–100× Shorts. Pasang sebagai *related video* di Shorts seri yang sama. Catatan: variasikan isi tiap video — kebijakan "inauthentic/repetitious content" YouTube bisa menolak monetisasi untuk video yang nyaris identik.
11. **Template CapCut "Jadi gini ➜ mentahannya"** — 3–5 template quote/story memakai footage sendiri; tiap template mencantumkan @mentahanpov. Bila diterima sebagai CapCut creator, ini jalur pertumbuhan + penghasilan sendiri.
12. **Pinterest** sebagai platform ke-6: video pin 9:16, judul teks di layar ("Aesthetic rainy night — free footage"), link ke s.id. Bisa jadi distributor baru di pipeline (Pinterest API mendukung video pin — perlu dicek).
13. **Model freemium footage**: gratis = 1080p tanpa watermark (seperti sekarang); berbayar = 4K/master asli lewat Pond5/Adobe Stock, atau "dukung via Saweria/Trakteer". **Cek dulu** syarat microstock soal footage yang juga dibagikan gratis — jangan unggah yang sama sebelum aturannya jelas.

## C. Yang TIDAK disarankan
- Menaikkan volume posting atau memakai klip yang mirip-mirip — bertentangan dengan arah orisinalitas & "repetitious content".
- Membeli follower/views atau tools "auto-engagement" — risiko akun dibatasi.
- Mengandalkan angka penghasilan "ambient channel $3.000/hari" dari blog — tidak terverifikasi.

## D. Usulan urutan 30 hari
| Minggu | Kerjakan |
|---|---|
| 1 | Ide 1–5 (manual) |
| 2 | Ide 6–8 (kode kecil, satu deploy) |
| 3 | Long-form pertama (ide 10) + 2 template CapCut (ide 11) |
| 4 | Evaluasi (bareng evaluasi ±14 Nov): mana yang menaikkan klik s.id, follower, dan views dari search |

## Sumber
- YouTube originality update: [Social Media Today](https://www.socialmediatoday.com/news/youtube-updates-shorts-algorithm-to-put-more-focus-on-original-content/831976/), [9to5Google](https://9to5google.com/2026/10/02/youtube-wont-recommend-unoriginal-shorts/), [MediaPost](https://www.mediapost.com/publications/article/418466/youtube-alters-shorts-algorithm-for-originality.html?edition=144047), [OutlierKit](https://outlierkit.com/resources/youtube-shorts-original-content-update/)
- Instagram ranking: [Dataslayer](https://www.dataslayer.ai/blog/instagram-algorithm-2025-complete-guide-for-marketers), [Firebelly](https://firebellymedia.co.uk/news/instagram-algorithm-mosseri-originality-recency/), [HighStyle](https://www.highstyle.ai/insights/instagram-reels-algorithm-2026)
- Trial Reels: [TechCrunch](https://techcrunch.com/2024/12/10/instagram-rolls-out-trial-reels-that-arent-shown-to-a-creators-followers), [Publer](https://publer.com/blog/instagram-trial-reels-guide/)
- TikTok search: [Metricool](https://metricool.com/tiktok-seo/), [Hootsuite](https://blog.hootsuite.com/tiktok-algorithm/), [Vito Atmo](https://vitoatmo.com/artikel/tiktok-seo-brand-indonesia-2026), [TikTok "video mentahan buat story"](https://www.tiktok.com/discover/video-mentahan-buat-story)
- Remix & attribution: [Lindsey Gamble](https://lindseygamble.com/blog/youtube-expands-remix-feature-to-allow-creators-to-use-segments-from-other-videos-in-youtube-shorts), [PPC Land](https://ppc.land/youtube-tests-ai-that-turns-someone-elses-short-into-a-brand-new-video/)
- Related links & RPM: [vidIQ](https://vidiq.com/blog/post/youtube-related-links-connect-shorts-long-videos), [Subscribr](https://subscribr.ai/youtube-strategy/youtube-shorts-hurting-long-form-views), [Metricool YouTube trends](https://metricool.com/youtube-trends/)
- Ambience monetisasi: [Infinity Looper](https://infinitylooper.com/use-cases/youtube-monetisation)
- CapCut: [Selular](https://selular.id/2026/05/capcut-bikin-kreator-indonesia-naik-kelas-lewat-gala-2026/), [CapCut mentahan video polos](https://www.capcut.com/id-id/explore/mentahan-video-polos), [Detik tren 2026](https://www.detik.com/jabar/berita/d-8507159/kreator-wajib-tahu-ini-10-tren-konten-media-sosial-tahun-2026)
- WhatsApp: [Infobip](https://www.infobip.com/blog/whatsapp-statistics-2025-global-usage-market-overview), [eMarketer](https://www.emarketer.com/content/daily-forecast-whatsapp-dominates-mobile-messaging-in-indonesia)
- Pinterest: [DailySocial](https://dailysocial.id/post/pinterest-opens-new-office-in-singapore-focusing-on-india-and-southeast-asia), [B2W](https://www.b2w.tv/blog/pinterest-video-strategy)
- Microstock: [Shopify Indonesia](https://www.shopify.com/id-id/blog/jual-foto-online), [KapanLagi](https://www.kapanlagi.com/feeds/cara-menjual-foto-di-adobe-stock-panduan-lengkap-untuk-pemula-24a15cdf2a.html), [Microstock Guru](https://www.microstockguru.com/pond5-contributors-sell-stock-footage/)
