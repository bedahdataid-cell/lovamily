/**
 * Relay Telegram -> GitHub Actions untuk laporan iklan Lovamily on-demand.
 *
 * Alur:
 *   1. Telegram webhook POST tiap update ke Worker ini.
 *   2. Kalau text pesan diawali "/laporan" (opsional "harian"/"mingguan"),
 *      Worker balas "diproses..." ke grup lalu memicu GitHub
 *      repository_dispatch event "laporan_iklan".
 *   3. Workflow .github/workflows/laporan-iklan.yml (trigger repository_dispatch)
 *      menjalankan script -> laporan dikirim ke grup oleh script itu sendiri.
 *
 * Worker ini TIDAK menyimpan state apa pun. Ringan, ~1 request per perintah.
 *
 * ENV (Settings -> Variables and Secrets di dashboard Worker):
 *   TELEGRAM_BOT_TOKEN   - token bot (@BotFather)
 *   TELEGRAM_SECRET      - string acak; dicek vs header webhook Telegram
 *   GITHUB_PAT           - fine-grained PAT, izin "Actions: write" utk repo lovamily
 *   GITHUB_REPO          - "bedahdataid-cell/lovamily"
 *   ALLOWED_CHAT_ID      - "-5432346051" (grup Lovamily Laporan AI). Kosong = semua chat.
 *
 * Perintah yang dikenali di grup:
 *   /laporan            -> mode harian
 *   /laporan harian
 *   /laporan mingguan
 *   /laporan@lovamily_laporan_bot mingguan   (bentuk mention juga diterima)
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("laporan-iklan relay: OK", { status: 200 });
    }

    // Verifikasi request memang dari Telegram (secret token webhook).
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
      return json({ ok: true }); // update lain (join, edit, dst) diabaikan
    }

    const chatId = String(msg.chat.id);
    if (env.ALLOWED_CHAT_ID && chatId !== String(env.ALLOWED_CHAT_ID)) {
      return json({ ok: true }); // chat lain diabaikan diam-diam
    }

    const text = msg.text.trim();
    const m = text.match(/^\/laporan(?:@\w+)?(?:\s+(harian|mingguan))?\b/i);
    if (!m) {
      return json({ ok: true }); // bukan perintah kita
    }
    const mode = (m[1] || "harian").toLowerCase();

    // Balas cepat supaya user tahu diterima.
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `⏳ Menyiapkan laporan ${mode}… (biasanya < 1 menit)`,
      reply_to_message_id: msg.message_id,
      allow_sending_without_reply: true,
    });

    // Picu GitHub Actions.
    const ghResp = await fetch(
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
          client_payload: { mode, requested_by: msg.from && msg.from.id, chat_id: chatId },
        }),
      }
    );

    if (!ghResp.ok) {
      const detail = await ghResp.text();
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: `❌ Gagal memicu laporan (GitHub ${ghResp.status}). Cek PAT / repo.\n${detail.slice(0, 300)}`,
      });
    }

    return json({ ok: true });
  },
};

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
    /* abaikan — balasan kenyamanan saja */
  }
}
