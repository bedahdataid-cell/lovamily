# Laporan Iklan Lovamily — Telegram otomatis (24/7, tanpa PC)

Pelaporan performa iklan akun **Lovamily** (`2260765378091626`) ke Telegram,
berjalan di **GitHub Actions** — cloud, tidak butuh PC menyala.

Satu script Python, dua mode:

| Mode | Window | Jadwal | Isi |
|---|---|---|---|
| `harian` | kemarin + kumulatif | tiap hari **07:00 WIB** | per-campaign & per-ad lengkap, verdict KILL/WATCH/OK |
| `mingguan` | 7 hari + kumulatif | **Senin 07:30 WIB** | + arah tren (7d vs 7d lalu) + rekomendasi teks |

Keduanya kirim ke **chat Telegram yang sama**. Script **read-only** — tak pernah
mengubah iklan.

---

## SETUP (sekali)

### 1. Token Meta Marketing API — System User (permanen)

1. **business.facebook.com** → **Business Settings**. Pilih bisnis
   **"Akun BM Umum"** (pemilik ad account Lovamily).
2. **Users → System Users → Add** → nama `laporan-iklan`, role **Admin**. Create.
3. System user itu → **Assign Assets → Ad Accounts** → pilih **Lovamily** →
   izin **View Performance** (cukup) → Save.
4. **Generate New Token**:
   - App: salah satu app kamu, mis. **ERP Studio Marketing**. Kalau belum ada
     produk Marketing API di app itu, tambahkan di developers.facebook.com →
     app → Add Product → Marketing API.
   - Scope: centang **`ads_read`** (wajib). `read_insights` boleh ikut.
   - Token **tidak kedaluwarsa** selama system user & app aktif. **Salin sekarang.**
5. Verifikasi (opsional):
   `https://graph.facebook.com/debug_token?input_token=TOKEN&access_token=TOKEN`
   → `type: USER`, `expires_at: 0`, scopes memuat `ads_read`.

### 2. Bot Telegram + chat ID

1. Chat **@BotFather** → `/newbot` → simpan **bot token** (`123456789:ABC...`).
2. Tujuan:
   - **Ke diri sendiri:** chat **@userinfobot** → dia balas `Id: 12345678`.
   - **Ke grup:** buat grup, **add bot** ke grup, kirim 1 pesan di grup, buka
     `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` → cari
     `"chat":{"id":-100...}` → itu chat ID grup.
3. Pastikan kamu / grup **sudah pernah kirim pesan ke bot** (kalau tidak,
   `sendMessage` ditolak "chat not found").

### 3. Pasang ke GitHub (jalankan DI LUAR Google Drive)

> Git rusak di dalam Google Drive. Jalankan dari `C:\Users\bedah\` atau folder
> lokal lain. Butuh `git` + `gh` (GitHub CLI) yang sudah login.

```powershell
powershell -ExecutionPolicy Bypass -File "H:\My Drive\Workspace Lovamily\tools\laporan-iklan\github-actions\pasang_ke_github.ps1"
```

Script itu meng-clone repo **bedahdataid-cell/lovamily** ke `C:\Users\bedah\lovamily-gh`,
menyalin `laporan_harian_iklan.py` + workflow, lalu `git push`.

### 4. Set Secrets di repo GitHub (sekali)

```powershell
gh secret set META_ACCESS_TOKEN  --repo bedahdataid-cell/lovamily
gh secret set META_AD_ACCOUNT_ID --repo bedahdataid-cell/lovamily --body 2260765378091626
gh secret set TELEGRAM_BOT_TOKEN --repo bedahdataid-cell/lovamily
gh secret set TELEGRAM_CHAT_ID   --repo bedahdataid-cell/lovamily
```

(Perintah tanpa `--body` akan menanyakan nilainya secara interaktif — aman,
tidak tersimpan di shell history.)

Secrets ini **terenkripsi**, hanya terbaca oleh Actions, tidak pernah muncul di
log. Jangan pernah menaruh token di file yang di-commit.

### 5. Uji

GitHub → repo `lovamily` → tab **Actions** → **Laporan Iklan Lovamily** →
**Run workflow** → mode `harian` → Run. Lihat log & cek Telegram.

Selesai. Mulai sekarang laporan terkirim otomatis 07:00 tiap hari + analisa
Senin 07:30, selama repo & Secrets ada.

---

## Uji lokal (opsional, sebelum pasang ke cloud)

```powershell
cd "H:\My Drive\Workspace Lovamily\tools\laporan-iklan"
Copy-Item .env.example .env      # lalu isi 3 nilai
python laporan_harian_iklan.py harian   --dry-run   # cetak, tidak kirim
python laporan_harian_iklan.py mingguan --dry-run
python laporan_harian_iklan.py harian              # kirim beneran
```

`.env` **tidak akan ter-commit** (`.gitignore`: `tools/**/.env`).

---

## Struktur file

```
tools/laporan-iklan/
  laporan_harian_iklan.py       <- script utama (dipakai lokal & di Actions)
  .env.example                  <- template kredensial untuk uji lokal
  README.md                     <- file ini
  PROMPT_ANALISA_MINGGUAN.md    <- (opsional) prompt kalau mau analisa via Claude/MCP
  github-actions/
    .github/workflows/laporan-iklan.yml   <- workflow cron
    pasang_ke_github.ps1                   <- installer ke repo GitHub
  # dipertahankan untuk yang mau jalur PC lokal (bukan 24/7):
  jalankan_laporan.ps1
  setup_task_scheduler.ps1
```

---

## Contoh isi laporan harian

```
📊 Laporan Harian Iklan Lovamily — 28 Aug 2026, 07:00 WIB
Akun: Lovamily · status IN_GRACE_PERIOD · saldo Rp-12.500
⚠️ IN_GRACE_PERIOD — tagihan tertunggak, segera cek billing.
────────────────────
📦 ATC : Lovamily Campaign Testing ABO (26/08/26)
   OUTCOME_SALES
   kemarin: Rp40.317 · 6 hasil · CTR 6.93% · CPC Rp328 · Rp6.720/hasil
   kumulatif: Rp40.317 · 6 hasil · CTR 6.93% · CPC Rp328 · Rp6.720/hasil
   → 🟡 pantau
   • konten hybrid pernikahan 1 : ...
     kemarin Rp7.176/2h @Rp3.588 · kumulatif Rp7.176/2h @Rp3.588 · CTR 18.2%
   ...
────────────────────
Ambang playbook: KILL bila kumulatif ≥ Rp75.000 tanpa hasil · WATCH ... · OK ...
```

---

## Keamanan & catatan

- Token = akses **baca** performa akun Lovamily. Bocor → cabut di Business
  Settings → System Users → token → Delete, buat baru, update Secret.
- Cron GitHub bisa **telat 5–15 menit** saat beban tinggi — normal, bukan bug.
- "Hasil" = action pertama yang cocok (chat WA → ATC → purchase → lead).
  **ROAS penuh tetap direkonstruksi manual** — closing di WhatsApp, Meta tidak
  tahu nilainya (`rules/iklan.md` §5).
- Semua perubahan iklan (pause, ubah budget, buat campaign) tetap **manual /
  persetujuan Owner**. Tool ini hanya melapor.
- Kalau ad sangat banyak, pesan dipecah otomatis (batas Telegram 4096 char).

---

## Jalur alternatif: PC lokal (kalau tak mau lewat GitHub)

`setup_task_scheduler.ps1` mendaftarkan Windows Task Scheduler jam 07:00.
Kekurangan: **PC harus menyala** jam itu. Untuk 24/7 tanpa PC, pakai GitHub
Actions di atas.
