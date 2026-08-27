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

def graph_get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"{GRAPH}/{path}", params={**params, "access_token": ACCESS_TOKEN}, timeout=60
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Graph {path} ({resp.status_code}): {resp.text[:400]}")
    return resp.json()


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


def verdict(spend: float, results: int, cpr) -> str:
    if results == 0 and spend >= KILL_SPEND:
        return "\U0001F534 KILL"
    if results >= 3 and cpr is not None and cpr <= OK_CPR_MAX:
        return "\U0001F7E2 OK"
    if 1 <= results <= 2 and cpr is not None and cpr <= WATCH_CPR_MAX:
        return "\U0001F7E1 WATCH"
    if results == 0:
        return "⚪ belum ada hasil"
    return "\U0001F7E1 pantau"


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

def account_header(mode: str) -> list[str]:
    acct = fetch_account_status()
    code = acct.get("account_status")
    status = ACCOUNT_STATUS_LABEL.get(code, str(code))
    bal = acct.get("balance")
    bal_txt = rupiah(int(bal) / 100) if bal not in (None, "") else "-"
    title = "Laporan Harian" if mode == "harian" else "🗓️ Analisa Mingguan"
    head = [
        f"\U0001F4CA <b>{title} Iklan Lovamily</b> — {now_wib():%d %b %Y, %H:%M} WIB",
        f"Akun: {esc(acct.get('name', '-'))} · status <b>{esc(status)}</b> · saldo {bal_txt}",
    ]
    if code == 9:
        head.append("⚠️ <b>IN_GRACE_PERIOD</b> — tagihan tertunggak, segera cek billing.")
    head.append("─" * 20)
    return head


def fmt_campaign_block(rows_by_win: dict, wins: list[tuple[str, str]],
                       name_hint: str = "", obj_hint: str = "") -> str:
    """rows_by_win: {win_key: campaign_row}. wins: [(win_key, label), ...]"""
    ref = next((rows_by_win[k] for k, _ in wins if rows_by_win.get(k)), {})
    name = ref.get("campaign_name") or name_hint or "(campaign)"
    obj = ref.get("objective") or obj_hint or "-"
    lines = [f"\U0001F4E6 <b>{esc(name)}</b>", f"   <i>{esc(obj)}</i>"]
    for key, label in wins:
        row = rows_by_win.get(key)
        if not row:
            lines.append(f"   {label}: -")
            continue
        spend = float(row.get("spend", 0) or 0)
        res, rtype = sum_results(row)
        cpr = cost_per_result(row, rtype)
        ctr = float(row.get("ctr", 0) or 0)
        piece = f"   {label}: {rupiah(spend)} · {res} hasil · CTR {ctr:.2f}% · CPC {rupiah(row.get('cpc'))}"
        if cpr is not None:
            piece += f" · {rupiah(cpr)}/hasil"
        lines.append(piece)
    # verdict selalu dari window kumulatif
    cum = rows_by_win.get("cum")
    if cum:
        s = float(cum.get("spend", 0) or 0)
        r, rt = sum_results(cum)
        lines.append(f"   → {verdict(s, r, cost_per_result(cum, rt))}")
    return "\n".join(lines)


def fmt_ad_line(rows_by_win: dict, wins: list[tuple[str, str]]) -> str:
    ref = next((rows_by_win[k] for k, _ in wins if rows_by_win.get(k)), {})
    name = ref.get("ad_name", "(ad)")
    parts = []
    for key, label in wins:
        row = rows_by_win.get(key)
        if not row:
            continue
        spend = float(row.get("spend", 0) or 0)
        res, rt = sum_results(row)
        cpr = cost_per_result(row, rt)
        seg = f"{label} {rupiah(spend)}/{res}h"
        if cpr is not None:
            seg += f" @{rupiah(cpr)}"
        parts.append(seg)
    cum = rows_by_win.get("cum")
    ctr = float((cum or ref).get("ctr", 0) or 0)
    return f"   • {esc(name)}\n     " + " · ".join(parts) + f" · CTR {ctr:.1f}%"


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

    body = [account_header("mingguan")[0]]  # placeholder replaced below
    msg = assemble("mingguan", wins, camp, ads)

    # tambahkan blok TREN + REKOMENDASI di akhir
    trend_lines = ["", "─" * 20, "<b>Arah tren (7d ini vs 7d lalu)</b>"]
    for cid, row in sorted(
        camp["recent"].items(),
        key=lambda kv: float(kv[1].get("spend", 0) or 0),
        reverse=True,
    ):
        prev = prev_camp.get(cid, {})
        r_now, rt = sum_results(row)
        r_prev, _ = sum_results(prev)
        cpr_now = cost_per_result(row, rt)
        cpr_prev = cost_per_result(prev, rt)
        trend_lines.append(
            f"• {esc(row.get('campaign_name', '(campaign)'))}: "
            f"spend{arrow(row.get('spend'), prev.get('spend'))} · "
            f"hasil {r_prev}→{r_now}{arrow(r_now, r_prev)} · "
            f"biaya/hasil{arrow(cpr_now, cpr_prev)}"
        )

    rec_lines = ["", "<b>Rekomendasi</b>"]
    recs = build_recs(camp["cum"], ads["cum"], camp["recent"], prev_camp)
    rec_lines += [f"{i}. {line}" for i, line in enumerate(recs, 1)] or ["— tidak ada aksi mendesak."]
    rec_lines.append("")
    rec_lines.append(
        "⚠️ Min. 7 hari sebelum menyimpulkan. Jangan mass-kill kalau semua turun "
        "bareng — cek WA telat balas / stok / harga / LP dulu. Aksi tulis "
        "(pause, ubah budget) tetap keputusan Owner."
    )
    rec_lines.append(
        "ℹ️ ROAS penuh direkonstruksi manual dari closing WA "
        "(tanggal · nama ads · CEP · kualitas A/B/C · nilai order) ÷ spend."
    )
    return msg + "\n" + "\n".join(trend_lines + rec_lines)


def build_recs(camp_cum, ads_cum, camp_7d, prev_camp) -> list[str]:
    out = []
    for aid, row in ads_cum.items():
        spend = float(row.get("spend", 0) or 0)
        res, rt = sum_results(row)
        cpr = cost_per_result(row, rt)
        name = row.get("ad_name", "(ad)")
        if res == 0 and spend >= KILL_SPEND:
            out.append(f"KILL ad «{name}» — spend {rupiah(spend)} tanpa hasil (ambang {rupiah(KILL_SPEND)}).")
        elif res >= 3 and cpr is not None and cpr <= OK_CPR_MAX:
            out.append(f"Kandidat GRADUATE ad «{name}» — {res} hasil @ {rupiah(cpr)}/hasil. "
                       f"Cek closing terlacak, bertahan 7–14 hari sebelum copy ke Winning.")
    # sinyal fatigue: frequency tinggi di 7d
    for cid, row in camp_7d.items():
        try:
            freq = float(row.get("frequency", 0) or 0)
        except (TypeError, ValueError):
            freq = 0
        if freq >= 3.0:
            out.append(f"Frequency {freq:.1f} di «{esc(row.get('campaign_name','(campaign)'))}» "
                       f"(7d) — indikasi creative fatigue, siapkan kreatif baru.")
    if not out:
        out.append("Belum ada kreatif yang menembus ambang KILL/GRADUATE. Lanjutkan "
                   "pengamatan, pastikan ragam angle/format/hook tetap terjaga.")
    return out


def assemble(mode: str, wins, camp: dict, ads: dict) -> str:
    head = account_header(mode)
    body: list[str] = []
    all_cids = set()
    for w in camp.values():
        all_cids |= set(w)
    order = sorted(
        all_cids,
        key=lambda cid: float((camp["cum"].get(cid, {}) or {}).get("spend", 0) or 0),
        reverse=True,
    )
    for cid in order:
        rows_by_win = {k: camp[k].get(cid) for k in camp}
        # fallback nama/objective dari baris ad mana pun di campaign ini
        hint = next(
            (
                r
                for w in ads.values()
                for r in w.values()
                if (r or {}).get("campaign_id") == cid
            ),
            {},
        )
        body.append(
            fmt_campaign_block(
                rows_by_win, wins,
                name_hint=hint.get("campaign_name", ""),
                obj_hint=hint.get("objective", ""),
            )
        )
        ad_ids = {
            aid
            for w in ads.values()
            for aid, r in w.items()
            if (r or {}).get("campaign_id") == cid
        }
        ad_ids = sorted(
            ad_ids,
            key=lambda aid: float((ads["cum"].get(aid, {}) or {}).get("spend", 0) or 0),
            reverse=True,
        )
        for aid in ad_ids:
            body.append(fmt_ad_line({k: ads[k].get(aid) for k in ads}, wins))
        body.append("")

    foot = [
        "─" * 20,
        f"Ambang playbook: KILL bila kumulatif ≥ {rupiah(KILL_SPEND)} tanpa hasil · "
        f"WATCH bila 1–2 hasil ≤ {rupiah(WATCH_CPR_MAX)}/hasil · "
        f"OK bila ≥3 hasil ≤ {rupiah(OK_CPR_MAX)}/hasil.",
        "ℹ️ 'hasil' = chat WA / add-to-cart / purchase sesuai objektif.",
    ]
    return "\n".join(head + body + foot)


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
