# Relay on-demand: `/laporan` di grup Telegram → laporan langsung

Worker ini punya **dua fungsi**:

1. **Penjadwal** (Cloudflare Cron Triggers) — mengirim laporan harian 07:00 WIB
   dan mingguan Senin 07:30 WIB. **Ini menggantikan cron GitHub Actions**, yang
   terbukti tidak andal untuk repo yang jarang di-push (jadwal di-skip diam-diam).
2. **Relay on-demand** (webhook Telegram) — siapa pun di grup **"Lovamily
   Laporan AI"** bisa minta laporan kapan saja:

```
/laporan            → laporan harian
/laporan harian
/laporan mingguan
```

Respon ~30–60 detik (Worker balas "⏳ menyiapkan…" instan, laporan menyusul
setelah GitHub Actions selesai).

Alur: **Telegram → Cloudflare Worker → GitHub repository_dispatch → workflow
`laporan-iklan.yml` → laporan ke grup**. Worker tak menyimpan apa pun.

---

## SETUP (sekali)

### 1. GitHub PAT (fine-grained) — izin Actions

1. github.com (sebagai **bedahdataid-cell**) → Settings → Developer settings →
   **Personal access tokens → Fine-grained tokens** → **Generate new token**.
2. Isi:
   - Name: `lovamily-laporan-relay`
   - Expiration: 90 hari (atau lebih; catat untuk diperpanjang)
   - **Repository access** → Only select repositories → **bedahdataid-cell/lovamily**
   - **Permissions → Repository permissions → Actions**: **Read and write**
3. Generate → **salin token** (`github_pat_...`).

### 2. Secret token webhook (string acak bebas)

Buat string acak, mis. dari PowerShell:
```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 40 | % {[char]$_})
```
Simpan — dipakai di Worker (`TELEGRAM_SECRET`) DAN saat set webhook.

### 3. Buat Cloudflare Worker

**Cara A — dashboard (tanpa install):**
1. dash.cloudflare.com → **Workers & Pages** → **Create** → **Create Worker**.
2. Nama: `lovamily-laporan-relay` → Deploy (kode contoh dulu).
3. **Edit code** → hapus semua → tempel isi `worker.js` → **Deploy**.
4. **Settings → Variables and Secrets**, tambah:

   | Nama | Tipe | Nilai |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | Secret | token bot @BotFather |
   | `TELEGRAM_SECRET` | Secret | string acak dari langkah 2 |
   | `GITHUB_PAT` | Secret | PAT dari langkah 1 |
   | `GITHUB_REPO` | Text | `bedahdataid-cell/lovamily` |
   | `ALLOWED_CHAT_ID` | Text | `-5432346051` |

   Deploy ulang setelah menambah variable.
5. Catat URL Worker: `https://lovamily-laporan-relay.<akun>.workers.dev`
6. **Aktifkan Cron Triggers** (penjadwal laporan): Worker → **Settings** →
   **Triggers** → bagian **Cron Triggers** → **Add Cron Trigger** → tambah dua:
   - `0 0 * * *`  (harian, 07:00 WIB)
   - `30 0 * * 1` (mingguan, Senin 07:30 WIB)
   Kalau deploy lewat Wrangler (Cara B), dua cron ini sudah ada di
   `wrangler.toml` dan otomatis terpasang saat `wrangler deploy`.

**Cara B — Wrangler CLI:**
```bash
npm i -g wrangler
cd tools/laporan-iklan/cloudflare-worker
wrangler login
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_SECRET
wrangler secret put GITHUB_PAT
wrangler deploy
```
(`GITHUB_REPO` & `ALLOWED_CHAT_ID` sudah di `wrangler.toml`.)

### 4. Daftarkan webhook Telegram ke Worker

Ganti `<BOT_TOKEN>`, `<WORKER_URL>`, `<SECRET>`:
```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<WORKER_URL>&secret_token=<SECRET>&allowed_updates=["message"]
```
Buka URL itu di browser → harus balas `{"ok":true,"result":true,...}`.

Cek: `https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo`
→ `"url"` menunjuk ke Worker, `"pending_update_count"` kecil.

> ⚠️ Setelah webhook aktif, `getUpdates` **tidak bisa dipakai lagi**
> (Telegram push ke webhook, bukan polling). Untuk balik ke polling:
> `.../deleteWebhook`.

### 5. Uji

Di grup ketik `/laporan`. Dalam ~1 menit laporan harian muncul.
`/laporan mingguan` untuk analisa mingguan.

---

## Perawatan & catatan

- **PAT kedaluwarsa** → relay berhenti (jadwal cron tetap jalan). Perpanjang di
  GitHub, update Secret `GITHUB_PAT` di Worker.
- **Ganti bot token** (mis. setelah revoke) → update `TELEGRAM_BOT_TOKEN` di
  Worker **dan** `TELEGRAM_BOT_TOKEN` di GitHub Secrets, lalu set webhook ulang.
- **Semua anggota grup** bisa memicu (sesuai pilihan). Untuk batasi ke 1 user,
  tambah cek `msg.from.id` di `worker.js` (ada catatan di header file).
- Worker gratis tier: 100k request/hari — jauh di atas kebutuhan.
- Relay hanya *memicu*; laporan tetap dikirim oleh script Python di Actions,
  jadi format & isi identik dengan laporan terjadwal.
- Rate: `concurrency` di workflow mencegah dua run tumpang tindih; permintaan
  saat run lain jalan akan antre, bukan ditolak.
