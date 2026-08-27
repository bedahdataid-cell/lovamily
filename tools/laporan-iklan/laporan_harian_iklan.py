#!/usr/bin/env python3
"""
Laporan iklan Lovamily -> Telegram.

Dua mode (argumen pertama, default 'harian'):

  harian   : window "kemarin" + "kumulatif", per-campaign & per-ad lengkap.
             Dijadwalkan 07:00 WIB tiap hari.

  mingguan : window "7 hari terakhir" + "kumulatif", plus arah tren
             (bandingkan 7d ini vs 7d sebelumnya) dan rekomendasi teks
             berdasarkan ambang playbook. Dijadwalkan Senin 07:30 WIB.

Menandai ambang playbook Lovamily (rules/iklan.md sec.4):
  KILL   : spend kumulatif >= Rp75.000 tanpa satu pun hasil (chat/ATC/purchase)
  WATCH  : 1-2 hasil, biaya/hasil <= Rp60.000
  OK     : >=3 hasil, biaya/hasil <= Rp40.000

Murni Meta Graph API + requests. Tidak bergantung pada Claude / MCP.
Jalan di GitHub Actions (cloud, tanpa PC) atau lokal.

Konfigurasi: environment variables (di GitHub Actions = Secrets), atau file
.env di folder ini untuk uji lokal. Lihat README.md.

Pemakaian:
  python laporan_harian_iklan.py                 # mode harian, kirim
  python laporan_harian_iklan.py harian --dry-run
  python laporan_harian_iklan.py mingguan
  python laporan_harian_iklan.py mingguan --dry-run
"""

from __future__ import annotations

import os
import sys
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Loader .env minimal (tanpa dependency). Tidak menimpa env yang sudah ada."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v21.0")
ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "2260765378091626").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

KILL_SPEND = int(os.environ.get("AMBANG_KILL_SPEND", "75000"))
WATCH_CPR_MAX = int(os.environ.get("AMBANG_WATCH_CPR", "60000"))
OK_CPR_MAX = int(os.environ.get("AMBANG_OK_CPR", "40000"))

GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

INSIGHT_FIELDS = ",".join(
    [
        "campaign_id",
        "campaign_name",
        "ad_id",
        "ad_name",
        "adset_name",
        "objective",
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "ctr",
        "cpc",
        "cpm",
        "actions",
        "cost_per_action_type",
    ]
)

RESULT_ACTION_TYPES = [
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.total_messaging_connection",
    "offsite_conversion.fb_pixel_add_to_cart",
    "omni_add_to_cart",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
    "purchase",
    "lead",
]

ACCOUNT_STATUS_LABEL = {
    1: "ACTIVE",
    2: "DISABLED",
    3: "UNSETTLED",
    7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT",
    9: "IN_GRACE_PERIOD",
    100: "PENDING_CLOSURE",
    101: "CLOSED",
}


class ConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------

def rupiah(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "-"
    return "Rp" + f"{n:,.0f}".replace(",", ".")


def now_wib() -> datetime:
    return datetime.now(timezone(timedelta(hours=7)))


def check_config() -> None:
    missing = [
        name
        for name, val in (
            ("META_ACCESS_TOKEN", ACCESS_TOKEN),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        )
        if not val
    ]
    if missing:
        raise ConfigError("Konfigurasi belum lengkap: " + ", ".join(missing))


def esc(s) -> str:
    return html.escape(str(s), quote=False)


# ---------------------------------------------------------------------------
# Meta Graph API
# ---------------------------------------------------------------------------

class RateLimited(RuntimeError):
    pass


def graph_get(path: str, params: dict, _retries: int = 3) -> dict:
    """GET Graph API dengan retry backoff untuk rate limit transient Meta."""
    last = ""
    for attempt in range(_retries + 1):
        resp = requests.get(
            f"{GRAPH}/{path}",
            params={**params, "access_token": ACCESS_TOKEN},
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.json()
        last = resp.text[:500]
        # kode 4 / subcode 1504022 / 613 = app/user request limit — transient
        transient = (
            resp.status_code in (429, 500, 503)
            or '"code":4' in last
            or '"code":17' in last
            or '"code":613' in last
            or '"is_transient":true' in last
        )
        if transient and attempt < _retries:
            import time
            time.sleep(20 * (attempt + 1))  # 20s, 40s, 60s
            continue
        if transient:
            raise RateLimited(
                "Meta rate limit / batas panggilan API tercapai. "
                "App masih Development mode — limit rendah. "
                "Coba lagi ~1 jam, atau ajukan app ke mode Live. "
                f"[{resp.status_code}] {last[:200]}"
            )
        raise RuntimeError(f"Graph {path} ({resp.status_code}): {last}")
    raise RuntimeError(f"Graph {path}: gagal setelah {_retries} retry. {last}")


def fetch_insights(level: str, *, date_preset: str | None = None,
                   time_range: dict | None = None) -> list[dict]:
    params = {"level": level, "fields": INSIGHT_FIELDS, "limit": 200}
    if date_preset:
        params["date_preset"] = date_preset
    if time_range:
        params["time_range"] = f'{{"since":"{time_range["since"]}","until":"{time_range["until"]}"}}'
    data = graph_get(f"act_{AD_ACCOUNT_ID}/insights", params)
    rows: list[dict] = []
    while True:
        rows.extend(data.get("data", []))
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            break
        resp = requests.get(nxt, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Paging ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
    return rows


def fetch_account_status() -> dict:
    return graph_get(
        f"act_{AD_ACCOUNT_ID}",
        {"fields": "name,account_status,balance,currency,timezone_name"},
    )


# ---------------------------------------------------------------------------
# Ekstraksi hasil & verdict
# ---------------------------------------------------------------------------

def sum_results(row: dict) -> tuple[int, str]:
    actions = {a["action_type"]: a.get("value", 0) for a in row.get("actions", [])}
    for atype in RESULT_ACTION_TYPES:
        if atype in actions:
            try:
                return int(round(float(actions[atype]))), atype
            except (TypeError, ValueError):
                return 0, atype
    return 0, "-"


def cost_per_result(row: dict, result_type: str):
    for c in row.get("cost_per_action_type", []):
        if c.get("action_type") == result_type:
            try:
                return float(c.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def verdict(spend: float, results: int, cpr) -> tuple[str, str]:
    """Kembalikan (emoji_label, penjelasan_singkat)."""
    if results == 0 and spend >= KILL_SPEND:
        return "\U0001F534 STOP", f"habis {rupiah(spend)} tanpa hasil — sebaiknya dimatikan"
    if results >= 3 and cpr is not None and cpr <= OK_CPR_MAX:
        return "\U0001F7E2 BAGUS", f"{results} hasil, {rupiah(cpr)}/hasil — kandidat dinaikkan"
    if 1 <= results <= 2 and cpr is not None and cpr <= WATCH_CPR_MAX:
        return "\U0001F7E1 PANTAU", f"{results} hasil, {rupiah(cpr)}/hasil — tunggu 7 hari"
    if results == 0:
        return "⚪ BARU", "belum ada hasil, masih terlalu dini"
    return "\U0001F7E1 PANTAU", f"{results} hasil — biaya/hasil masih di atas target"


def ctr_note(ctr: float) -> str:
    if ctr >= 8:
        return " ⭐"
    return ""


def shorten_ad_name(raw: str) -> str:
    """'kupikir foto sungkeman ini : 1811... - Aug 18, 2026' -> 'kupikir foto sungkeman ini'."""
    name = raw.split(" : ")[0].split(" - ")[0].strip()
    name = name.rstrip(" :–-").strip()
    if len(name) > 42:
        name = name[:41].rstrip() + "…"
    return name or "(tanpa nama)"


# Pengelompokan ad berdasar kata kunci di nama — supaya pola angle terlihat.
ANGLE_GROUPS = [
    ("Pernikahan / Sungkeman", ("nikah", "sungkem", "altar", "altas", "pengantin", "ayah di momen")),
    ("Wisuda", ("wisuda", "wisudawa")),
    ("Foto lama disatukan", ("foto lama", "disatukan", "di satukan", "satu foto")),
]


def angle_of(name: str) -> str:
    low = name.lower()
    for label, keys in ANGLE_GROUPS:
        if any(k in low for k in keys):
            return label
    return "Lainnya"


def arrow(now_v, prev_v) -> str:
    try:
        n, p = float(now_v), float(prev_v)
    except (TypeError, ValueError):
        return ""
    if p == 0:
        return " (baru)" if n else ""
    delta = (n - p) / p * 100
    if abs(delta) < 5:
        return " →"
    return f" {'▲' if delta > 0 else '▼'}{abs(delta):.0f}%"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

OBJ_ID = {"OUTCOME_SALES": "Penjualan", "OUTCOME_ENGAGEMENT": "Chat WA",
          "OUTCOME_LEADS": "Leads", "OUTCOME_TRAFFIC": "Traffic",
          "OUTCOME_AWARENESS": "Awareness"}


def account_header(mode: str) -> list[str]:
    acct = fetch_account_status()
    code = acct.get("account_status")
    bal = acct.get("balance")
    bal_txt = rupiah(int(bal) / 100) if bal not in (None, "") else "-"
    label = {"harian": "kemarin", "mingguan": "7 hari terakhir"}.get(mode, mode)
    head = [
        f"\U0001F4CA <b>Laporan Iklan Lovamily</b>",
        f"{now_wib():%A, %d %b %Y · %H:%M} WIB — data {label}",
    ]
    if code == 9:
        head.append("")
        head.append(f"🔴 <b>Tagihan tertunggak</b> (saldo {bal_txt}). "
                    "Iklan bisa berhenti — segera bayar di Ads Manager.")
    head.append("━" * 18)
    return head


def campaign_summary_line(camp_recent: dict, camp_cum: dict) -> list[str]:
    """Ringkasan 1 blok pendek per campaign: total + verdict + kalimat."""
    ref = camp_cum or camp_recent
    name = ref.get("campaign_name", "(campaign)")
    obj = OBJ_ID.get(ref.get("objective", ""), ref.get("objective", "-"))

    s_now = float((camp_recent or {}).get("spend", 0) or 0)
    r_now, _ = sum_results(camp_recent) if camp_recent else (0, "-")
    s_cum = float((camp_cum or {}).get("spend", 0) or 0)
    r_cum, rt_cum = sum_results(camp_cum) if camp_cum else (0, "-")
    cpr_cum = cost_per_result(camp_cum, rt_cum) if camp_cum else None
    emo, why = verdict(s_cum, r_cum, cpr_cum)

    out = [
        f"\U0001F4E6 <b>{esc(name)}</b> — tujuan: {esc(obj)}",
        f"   {emo} · {esc(why)}",
        f"   Kemarin: {rupiah(s_now)} → {r_now} hasil    |    "
        f"Total: {rupiah(s_cum)} → {r_cum} hasil"
        + (f" ({rupiah(cpr_cum)}/hasil)" if cpr_cum is not None else ""),
    ]
    return out


def fmt_ad_compact(ad_recent: dict, ad_cum: dict) -> str:
    """Satu ad, 2 baris: nama + verdict-emoji, lalu angka ringkas."""
    ref = ad_cum or ad_recent
    name = shorten_ad_name(ref.get("ad_name", "(ad)"))
    s_now = float((ad_recent or {}).get("spend", 0) or 0)
    r_now, _ = sum_results(ad_recent) if ad_recent else (0, "-")
    s_cum = float((ad_cum or {}).get("spend", 0) or 0)
    r_cum, rt = sum_results(ad_cum) if ad_cum else (0, "-")
    cpr = cost_per_result(ad_cum, rt) if ad_cum else None
    ctr = float((ad_cum or ref).get("ctr", 0) or 0)
    emo, _why = verdict(s_cum, r_cum, cpr)
    dot = emo.split()[0]  # cuma bulatan warnanya

    tail = f" · {rupiah(cpr)}/hasil" if cpr is not None else ""
    return (
        f"   {dot} {esc(name)}{ctr_note(ctr)}\n"
        f"      total {rupiah(s_cum)} · {r_cum} hasil · CTR {ctr:.1f}%{tail}"
        f"  (kemarin {rupiah(s_now)}/{r_now}h)"
    )


# ---------------------------------------------------------------------------
# Build message per mode
# ---------------------------------------------------------------------------

def build_harian() -> str:
    wins = [("recent", "kemarin"), ("cum", "kumulatif")]
    camp = {
        "recent": {r["campaign_id"]: r for r in fetch_insights("campaign", date_preset="yesterday")},
        "cum": {r["campaign_id"]: r for r in fetch_insights("campaign", date_preset="maximum")},
    }
    ads = {
        "recent": {r["ad_id"]: r for r in fetch_insights("ad", date_preset="yesterday")},
        "cum": {r["ad_id"]: r for r in fetch_insights("ad", date_preset="maximum")},
    }
    return assemble("harian", wins, camp, ads)


def build_mingguan() -> str:
    today = now_wib().date()
    this_since = today - timedelta(days=7)
    this_until = today - timedelta(days=1)
    prev_since = today - timedelta(days=14)
    prev_until = today - timedelta(days=8)
    tr_this = {"since": this_since.isoformat(), "until": this_until.isoformat()}
    tr_prev = {"since": prev_since.isoformat(), "until": prev_until.isoformat()}

    wins = [("recent", "7d"), ("cum", "kumulatif")]
    camp = {
        "recent": {r["campaign_id"]: r for r in fetch_insights("campaign", time_range=tr_this)},
        "cum": {r["campaign_id"]: r for r in fetch_insights("campaign", date_preset="maximum")},
    }
    ads = {
        "recent": {r["ad_id"]: r for r in fetch_insights("ad", time_range=tr_this)},
        "cum": {r["ad_id"]: r for r in fetch_insights("ad", date_preset="maximum")},
    }
    prev_camp = {r["campaign_id"]: r for r in fetch_insights("campaign", time_range=tr_prev)}

    msg = assemble("mingguan", wins, camp, ads)

    # --- blok TREN (7d ini vs 7d lalu) ---
    trend_lines = ["", "━" * 18, "<b>Tren mingguan</b> (7 hari ini vs 7 hari sebelumnya)"]
    for cid, row in sorted(
        camp["recent"].items(),
        key=lambda kv: float(kv[1].get("spend", 0) or 0),
        reverse=True,
    ):
        prev = prev_camp.get(cid, {})
        r_now, rt = sum_results(row)
        r_prev, _ = sum_results(prev)
        s_now = float(row.get("spend", 0) or 0)
        s_prev = float(prev.get("spend", 0) or 0)
        nm = esc(row.get("campaign_name", "(campaign)"))
        trend_lines.append(
            f"• {nm}\n"
            f"   belanja {rupiah(s_prev)} → {rupiah(s_now)}{arrow(s_now, s_prev)}  ·  "
            f"hasil {r_prev} → {r_now}{arrow(r_now, r_prev)}"
        )

    # --- REKOMENDASI ---
    rec_lines = ["", "<b>Rekomendasi minggu ini</b>"]
    recs = build_recs(camp["cum"], ads["cum"], camp["recent"], prev_camp)
    rec_lines += [f"{i}. {line}" for i, line in enumerate(recs, 1)]
    rec_lines.append("")
    rec_lines.append(
        "⚠️ Butuh min. 7 hari data sebelum menyimpulkan. Kalau semua turun bersamaan, "
        "cek dulu: admin telat balas WA / stok / harga / landing page — bukan langsung "
        "matikan iklan. Keputusan pause / ubah budget tetap di tangan Owner."
    )
    rec_lines.append(
        "ℹ️ Nilai penjualan sebenarnya (ROAS) dihitung terpisah dari catatan closing "
        "WhatsApp Sales Closer: tanggal · nama iklan · nilai order ÷ belanja iklan."
    )
    return msg + "\n" + "\n".join(trend_lines + rec_lines)


def build_recs(camp_cum, ads_cum, camp_7d, prev_camp) -> list[str]:
    out = []
    for aid, row in ads_cum.items():
        spend = float(row.get("spend", 0) or 0)
        res, rt = sum_results(row)
        cpr = cost_per_result(row, rt)
        name = shorten_ad_name(row.get("ad_name", "(ad)"))
        if res == 0 and spend >= KILL_SPEND:
            out.append(f"🔴 Matikan «{esc(name)}» — sudah habis {rupiah(spend)} tanpa satu pun hasil.")
        elif res >= 3 and cpr is not None and cpr <= OK_CPR_MAX:
            out.append(f"🟢 Naikkan «{esc(name)}» — {res} hasil @ {rupiah(cpr)}/hasil. "
                       f"Pastikan ada closing WA terlacak & bertahan 7–14 hari dulu.")
    for cid, row in camp_7d.items():
        try:
            freq = float(row.get("frequency", 0) or 0)
        except (TypeError, ValueError):
            freq = 0
        if freq >= 3.0:
            out.append(f"🟠 Frekuensi tayang {freq:.1f}× di «{esc(row.get('campaign_name', '(campaign)'))}» "
                       f"— audiens mulai bosan, siapkan kreatif baru.")
    if not out:
        out.append("Belum ada iklan yang perlu dimatikan atau dinaikkan. Lanjutkan "
                   "pengamatan; jaga ragam angle / format / hook tetap beragam.")
    return out


def _all_ids(d: dict) -> set:
    out = set()
    for w in d.values():
        out |= set(w)
    return out


def assemble(mode: str, wins, camp: dict, ads: dict) -> str:
    head = account_header(mode)

    cids = sorted(
        _all_ids(camp),
        key=lambda cid: float((camp["cum"].get(cid, {}) or {}).get("spend", 0) or 0),
        reverse=True,
    )
    aids = _all_ids(ads)

    # --- kumpulkan verdict per-ad untuk bagian "perlu perhatian" ---
    perhatian_stop, perhatian_bagus = [], []
    for aid in aids:
        ac = ads["cum"].get(aid, {})
        s = float(ac.get("spend", 0) or 0)
        r, rt = sum_results(ac)
        cpr = cost_per_result(ac, rt)
        nm = shorten_ad_name(ac.get("ad_name", "(ad)"))
        if r == 0 and s >= KILL_SPEND:
            perhatian_stop.append(f"   🔴 {esc(nm)} — habis {rupiah(s)}, 0 hasil. Pertimbangkan matikan.")
        elif r >= 3 and cpr is not None and cpr <= OK_CPR_MAX:
            perhatian_bagus.append(
                f"   🟢 {esc(nm)} — {r} hasil @ {rupiah(cpr)}/hasil. Kandidat dinaikkan."
            )

    # --- ringkasan total akun ---
    tot_spend_cum = sum(float((camp["cum"].get(c, {}) or {}).get("spend", 0) or 0) for c in cids)
    tot_res_cum = sum(sum_results(camp["cum"].get(c, {}))[0] for c in cids)
    tot_spend_rec = sum(float((camp["recent"].get(c, {}) or {}).get("spend", 0) or 0) for c in cids)
    tot_res_rec = sum(sum_results(camp["recent"].get(c, {}))[0] for c in cids)
    ringkas = [
        f"\U0001F4B0 <b>Total</b>: kemarin {rupiah(tot_spend_rec)} → {tot_res_rec} hasil"
        f"  ·  keseluruhan {rupiah(tot_spend_cum)} → {tot_res_cum} hasil",
    ]

    perlu = []
    if perhatian_stop or perhatian_bagus:
        perlu.append("")
        perlu.append("\U0001F514 <b>Perlu perhatian</b>")
        perlu += perhatian_stop + perhatian_bagus
    else:
        perlu.append("")
        perlu.append("\U0001F7E2 Tidak ada yang mendesak — semua kreatif masih dalam masa uji.")

    # --- detail per campaign, ad dikelompokkan per angle ---
    detail = ["", "━" * 18, "<b>Rincian per iklan</b>"]
    for cid in cids:
        detail.append("")
        detail += campaign_summary_line(camp["recent"].get(cid, {}), camp["cum"].get(cid, {}))

        camp_aids = [
            aid for aid in aids
            if (ads["cum"].get(aid, ads["recent"].get(aid, {})) or {}).get("campaign_id") == cid
        ]
        # kelompokkan per angle, urut spend desc di dalam grup
        groups: dict[str, list] = {}
        for aid in camp_aids:
            nm = (ads["cum"].get(aid, ads["recent"].get(aid, {})) or {}).get("ad_name", "")
            groups.setdefault(angle_of(shorten_ad_name(nm)), []).append(aid)

        # urutkan grup: yang total spend-nya besar dulu
        def gspend(g):
            return sum(float((ads["cum"].get(a, {}) or {}).get("spend", 0) or 0) for a in g)

        for gname, gids in sorted(groups.items(), key=lambda kv: gspend(kv[1]), reverse=True):
            detail.append(f"   <i>— {esc(gname)} —</i>")
            for aid in sorted(
                gids,
                key=lambda a: float((ads["cum"].get(a, {}) or {}).get("spend", 0) or 0),
                reverse=True,
            ):
                detail.append(fmt_ad_compact(ads["recent"].get(aid), ads["cum"].get(aid)))

    foot = [
        "",
        "━" * 18,
        "<b>Arti tanda</b>: 🟢 bagus (siap dinaikkan) · 🟡 pantau (tunggu 7 hari) · "
        "🔴 stop (boros tanpa hasil) · ⚪ baru · ⭐ CTR tinggi (&gt;8%).",
        "‘hasil’ = chat WA / add-to-cart / pembelian, sesuai tujuan campaign. "
        "Angka pembelian penuh (ROAS) tetap dihitung manual dari closing WhatsApp.",
    ]
    return "\n".join(head + ringkas + perlu + detail + foot)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def split_chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in split_chunks(text):
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Telegram ({r.status_code}): {r.text[:300]}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    mode = (args[0] if args else "harian").lower()
    dry = "--dry-run" in sys.argv
    if mode not in ("harian", "mingguan"):
        print(f"mode tidak dikenal: {mode} (pakai 'harian' atau 'mingguan')", file=sys.stderr)
        return 2

    try:
        check_config()
        msg = build_harian() if mode == "harian" else build_mingguan()
    except ConfigError as e:
        print(f"[config] {e}", file=sys.stderr)
        return 2
    except RateLimited as e:
        warn = (
            f"⏳ Laporan {mode} tertunda — {e}\n"
            "Jadwal berikutnya akan mencoba lagi otomatis."
        )
        print(warn, file=sys.stderr)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            try:
                send_telegram(warn)
            except Exception:  # noqa: BLE001
                pass
        return 0  # bukan kegagalan permanen — jangan tandai run merah
    except Exception as e:  # noqa: BLE001
        err = f"❌ Laporan iklan ({mode}) GAGAL {now_wib():%d %b %H:%M}\n{type(e).__name__}: {e}"
        print(err, file=sys.stderr)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            try:
                send_telegram(err)
            except Exception as e2:  # noqa: BLE001
                print(f"[telegram fallback gagal] {e2}", file=sys.stderr)
        return 1

    if dry:
        print(msg)
        return 0
    send_telegram(msg)
    print(f"[ok] laporan {mode} terkirim {now_wib():%Y-%m-%d %H:%M}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
