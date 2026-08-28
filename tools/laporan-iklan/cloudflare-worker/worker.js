/**
 * Relay + penjadwal untuk laporan iklan Lovamily.
 *
 * DUA fungsi:
 *
 * A. On-demand (fetch handler) — Telegram webhook.
 *    "/laporan [harian|mingguan]" di grup -> picu GitHub repository_dispatch.
 *
 * B. Terjadwal (scheduled handler) — Cloudflare Cron Triggers.
 *    Dipakai karena cron GitHub Actions TIDAK ANDAL untuk repo yang sepi
 *    (sering di-skip). Cloudflare cron presisi. Worker memicu
 *    repository_dispatch yang sama; GitHub Actions tetap yang menjalankan
 *    script Python & mengirim ke Telegram.
 *
 *    Cron di wrangler.toml (UTC):
 *      "0 0 * * *"   -> 07:00 WIB  -> mode "harian"
 *      "30 0 * * 1"  -> Senin 07:30 WIB -> mode "mingguan"
 *
 * Worker tidak menyimpan state. ENV (dashboard: Settings -> Variables & Secrets):
 *   TELEGRAM_BOT_TOKEN  - token bot (@BotFather)
 *   TELEGRAM_SECRET     - string acak; dicek vs header webhook Telegram
 *   GITHUB_PAT          - fine-grained PAT, izin Contents+Actions RW utk repo lovamily
 *   GITHUB_REPO         - "bedahdataid-cell/lovamily"
 *   ALLOWED_CHAT_ID     - "-5432346051" (grup). Kosong = semua chat.
 */

export default {
  // ---- A. On-demand: webhook Telegram ----
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("laporan-iklan relay: OK", { status: 200 });
    }

    if (env.TELEGRAM_SECRET) {
      const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (got !== env.TELEGRAM_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    const msg = update.message || update.channel_post;
    if (!msg || typeof msg.text !== "string") {
      return json({ ok: true });
    }

    const chatId = String(msg.chat.id);
    if (env.ALLOWED_CHAT_ID && chatId !== String(env.ALLOWED_CHAT_ID)) {
      return json({ ok: true });
    }

    const text = msg.text.trim();
    const m = text.match(/^\/laporan(?:@\w+)?(?:\s+(harian|mingguan))?\b/i);
    if (!m) {
      return json({ ok: true });
    }
    const mode = (m[1] || "harian").toLowerCase();

    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `⏳ Menyiapkan laporan ${mode}… (biasanya < 1 menit)`,
      reply_to_message_id: msg.message_id,
      allow_sending_without_reply: true,
    });

    const r = await dispatchGithub(env, mode, { requested_by: msg.from && msg.from.id, chat_id: chatId });
    if (!r.ok) {
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: `❌ Gagal memicu laporan (GitHub ${r.status}). Cek PAT / repo.\n${r.detail.slice(0, 300)}`,
      });
    }
    return json({ ok: true });
  },

  // ---- B. Terjadwal: Cloudflare Cron Triggers ----
  async scheduled(event, env, ctx) {
    // event.cron = string cron yang memicu, mis. "0 0 * * *"
    const mode = event.cron === "30 0 * * 1" ? "mingguan" : "harian";
    ctx.waitUntil(
      (async () => {
        const r = await dispatchGithub(env, mode, { source: "cloudflare-cron", cron: event.cron });
        if (!r.ok && env.ALLOWED_CHAT_ID) {
          await tg(env, "sendMessage", {
            chat_id: env.ALLOWED_CHAT_ID,
            text: `❌ Penjadwal laporan ${mode} gagal memicu GitHub (${r.status}).\n${r.detail.slice(0, 300)}`,
          });
        }
      })()
    );
  },
};

// --- helper ---

async function dispatchGithub(env, mode, extra) {
  try {
    const resp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_PAT}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "lovamily-laporan-relay",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({
          event_type: "laporan_iklan",
          client_payload: { mode, ...extra },
        }),
      }
    );
    if (resp.ok) return { ok: true, status: resp.status, detail: "" };
    return { ok: false, status: resp.status, detail: await resp.text() };
  } catch (e) {
    return { ok: false, status: 0, detail: String(e) };
  }
}

function json(obj) {
  return new Response(JSON.stringify(obj), {
    headers: { "content-type": "application/json" },
  });
}

async function tg(env, method, body) {
  try {
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    /* abaikan */
  }
}
