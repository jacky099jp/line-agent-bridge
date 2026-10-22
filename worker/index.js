/**
 * 小姚 · LINE 通道（Cloudflare Worker）
 *
 * 角色：LINE 与你本机「小姚」之间的中转站。
 *
 *   你 → LINE → 本 Worker（验签、入队、立即回 200）
 *                    ↓
 *              本机小姚每隔几秒来 /poll 取信
 *                    ↓ 处理完
 *              /reply → LINE push 给你
 *
 * 本机不在线时：消息照样入队，并立刻回你一句「已排队」的提示，
 * 等小姚接上再正式回答。（不做任何 AI 代答，避免冒充小姚乱讲）
 *
 * 部署名保持 line-openclaw-worker（URL 不变，LINE 后台无需改动）。
 *
 * 【2026-10-12 存储改造】队列从 Workers KV 改为 D1。
 *   起因：KV 免费版每天只有 1000 次「写 + 列表」额度，而本机每分钟
 *   轮询一次（list + put）＝ 1440 次/天，结构上必然在当天某点耗尽，
 *   之后 /poll、/status 全部抛 "KV list()/put() limit exceeded for the day."
 *   → Worker 返回 500，整条 LINE 通道静默死掉（本机只看到 HTTP 500）。
 *   D1 免费额度为 5,000,000 行读 / 100,000 行写 每天，够用两个数量级。
 *
 * 【2026-10-13 群组支持】Jack 要把小姚加进 LINE 群组当「记录事情」的通道。
 *   原来只认 src.userId，群消息会被当成私聊：回复落到他私聊、会话也混在一起。
 *   现在入队时一并记录来源类型与目标 id：
 *     - srcType: 'user' | 'group' | 'room'
 *     - srcId:   群组/多人房 id；私聊时等于 userId
 *   回推时本机会带 target（群 → 群 id），Worker 推给 target；
 *   群推失败（额度用尽 / 被移出群）会回退推给 Jack 私聊，绝不静默丢弃。
 *   另外 /status 会回报最近一次收到的群 id，方便本机自动发现新群。
 */

const LINE_API = 'https://api.line.me/v2/bot';

const ONLINE_WINDOW_MS = 90 * 1000;   // 本机 90 秒内来取过信 → 视为在线
const TAKE_STALE_MS = 180 * 1000;     // 被取走但没确认超过 3 分钟 → 退回队列
const MSG_MAX_AGE_MS = 6 * 3600 * 1000; // 队列里超过 6 小时的直接丢弃
const MAX_QUEUE = 50;                 // 队列上限，防洪水

const QUEUED_NOTICE =
  '【雲端】小姚的本機現在沒接上（電腦忙或離線），你這條已經排進隊伍了，' +
  '他一接上就會回你。';

// ---------------------------------------------------------------- 工具

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

function timingSafeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}

/** LINE 的 x-line-signature = base64(HMAC-SHA256(channelSecret, rawBody)) */
async function verifySignature(rawBody, signature, channelSecret) {
  if (!signature) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(channelSecret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  const mac = await crypto.subtle.sign('HMAC', key, enc.encode(rawBody));
  const expected = btoa(String.fromCharCode(...new Uint8Array(mac)));
  return timingSafeEqual(expected, signature);
}

async function pushToLine(userId, text, channelToken) {
  const res = await fetch(LINE_API + '/message/push', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + channelToken,
    },
    body: JSON.stringify({ to: userId, messages: [{ type: 'text', text }] }),
  });
  return res.status;
}

/**
 * 用 reply token 回覆 —— 免费、不占每月额度，但 token 有效期短（约 1 分钟）、
 * 且只能用一次。失败返回非 2xx，由调用方回退到 push。
 */
async function replyToLine(replyToken, text, channelToken) {
  const res = await fetch(LINE_API + '/message/reply', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + channelToken,
    },
    body: JSON.stringify({ replyToken, messages: [{ type: 'text', text }] }),
  });
  return res.status;
}

// ---------------------------------------------------------------- 队列（D1）

async function getMeta(env, k) {
  const row = await env.DB.prepare('SELECT v FROM line_meta WHERE k = ?').bind(k).first();
  return row ? row.v : null;
}

async function setMeta(env, k, v) {
  await env.DB
    .prepare('INSERT INTO line_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v')
    .bind(k, v)
    .run();
}

async function localOnline(env) {
  const t = parseInt(await getMeta(env, 'poll:last'), 10);
  return Number.isFinite(t) && Date.now() - t < ONLINE_WINDOW_MS;
}

async function enqueue(env, item) {
  const now = Date.now();
  const ts = item.ts || now;
  await env.DB.batch([
    // 顺手清理超龄消息
    env.DB.prepare('DELETE FROM line_inbox WHERE ts < ?').bind(now - MSG_MAX_AGE_MS),
    env.DB.prepare(
      'INSERT OR REPLACE INTO line_inbox ' +
      '(id, userId, text, kind, messageId, ts, state, takenAt, srcType, srcId, replyToken) ' +
      "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?)"
    ).bind(
      item.id, item.userId, item.text || '', item.kind || 'text', item.messageId || '', ts,
      item.srcType || 'user', item.srcId || item.userId || '', item.replyToken || ''
    ),
  ]);
  // 队列上限：满了丢最旧的
  const c = await env.DB.prepare('SELECT COUNT(*) AS n FROM line_inbox').first();
  const n = c ? Number(c.n) : 0;
  if (n > MAX_QUEUE) {
    await env.DB
      .prepare('DELETE FROM line_inbox WHERE id IN (SELECT id FROM line_inbox ORDER BY ts ASC LIMIT ?)')
      .bind(n - MAX_QUEUE)
      .run();
  }
}

/** 取信：pending 的和「取走太久没确认」的都交出去，并标记为 taken */
async function poll(env) {
  const now = Date.now();
  const rs = await env.DB.prepare(
    'SELECT id, userId, text, kind, messageId, ts, srcType, srcId, replyToken FROM line_inbox ' +
    "WHERE ts > ? AND (state = 'pending' OR (state = 'taken' AND takenAt < ?)) " +
    'ORDER BY ts ASC LIMIT 100'
  ).bind(now - MSG_MAX_AGE_MS, now - TAKE_STALE_MS).all();
  const rows = (rs && rs.results) || [];
  if (!rows.length) return [];
  const stmt = env.DB.prepare("UPDATE line_inbox SET state = 'taken', takenAt = ? WHERE id = ?");
  await env.DB.batch(rows.map((r) => stmt.bind(now, r.id)));
  return rows.map((v) => ({
    id: v.id,
    userId: v.userId,
    text: v.text || '',
    kind: v.kind || 'text',
    messageId: v.messageId || '',
    ts: v.ts,
    srcType: v.srcType || 'user',
    srcId: v.srcId || '',
    replyToken: v.replyToken || '',
  }));
}

async function ack(env, ids) {
  const list = (ids || []).filter(Boolean);
  if (!list.length) return;
  const stmt = env.DB.prepare('DELETE FROM line_inbox WHERE id = ?');
  await env.DB.batch(list.map((id) => stmt.bind(id)));
}

// ---------------------------------------------------------------- 入口

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const p = url.pathname;

    // ---- 健康检查（公开，无害）
    if (request.method === 'GET' && p === '/') {
      return json({
        status: '✅ 小姚 LINE 通道（中转版·D1）',
        online: await localOnline(env),
        time: new Date().toISOString(),
      });
    }

    // ---- LINE webhook：必须验签，公开
    if (p === '/webhook' || p === '/line-webhook') {
      if (request.method === 'GET') return new Response('OK', { status: 200 });
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });

      const raw = await request.text();
      const secret = env.LINE_CHANNEL_SECRET;
      const sig = request.headers.get('x-line-signature');
      if (secret) {
        const ok = await verifySignature(raw, sig, secret).catch(() => false);
        if (!ok) return new Response('Bad signature', { status: 403 });
      } else {
        // 密钥丢失时放行并在日志里喊出来，避免整条 LINE 直接死掉
        console.error('[line-worker] LINE_CHANNEL_SECRET 缺失，本次跳过验签');
      }

      let body;
      try { body = JSON.parse(raw); } catch { return new Response('OK', { status: 200 }); }

      const token = env.LINE_CHANNEL_TOKEN;
      const online = await localOnline(env);
      for (const event of body.events || []) {
        try {
          const src = event.source || {};
          // 【2026-10-13】把每个事件原样记一行，用于诊断「拉进群后立刻退出」这类问题。
          // 一次一行、字段截断，避免刷爆日志。
          console.log('[line-worker] event ' + String(event.type) + ' src=' + JSON.stringify(src).slice(0, 200));
          // 事件落库：诊断「拉进群后立刻退出」这类问题全靠它（join/leave 都会留下痕迹）
          {
            const evRow = env.DB.prepare(
              'INSERT OR REPLACE INTO line_events (id, ts, type, srcType, srcId, userId, raw) VALUES (?, ?, ?, ?, ?, ?, ?)'
            ).bind(
              'e' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
              Date.now(), String(event.type || ''),
              String(src.type || ''), String(src.groupId || src.roomId || ''),
              String(src.userId || ''), JSON.stringify(event).slice(0, 900)
            ).run().catch(() => {});
            if (ctx && ctx.waitUntil) ctx.waitUntil(evRow);
          }
          const userId = src.userId;
          if (!userId) continue;

          // 来源判定：私聊 / 群组 / 多人房。群组消息以前会被当成私聊处理。
          const replyToken = event.replyToken || '';
          const srcType = src.type || 'user';
          const srcId = srcType === 'group'
            ? (src.groupId || '')
            : srcType === 'room'
              ? (src.roomId || '')
              : userId;
          if (srcType === 'group' || srcType === 'room') {
            // 记下最近出现的群，方便本机自动发现并白名单
            await setMeta(env, 'line:lastGroup', JSON.stringify({
              type: srcType, id: srcId, from: userId, at: Date.now(),
            })).catch(() => {});
          }

          if (event.type === 'message' && event.message && event.message.type === 'text') {
            const text = (event.message.text || '').trim();
            if (!text) continue;
            const item = {
              id: 'm' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
              userId,
              srcType,
              srcId,
              text,
              replyToken,
              ts: Date.now(),
              state: 'pending',
            };
            await enqueue(env, item);
            if (!online && token) await pushToLine(userId, QUEUED_NOTICE, token);
          } else if (event.type === 'message' && event.message && event.message.type === 'audio') {
            // 语音消息：本机没有 Channel Token，下载不了音频。
            // 这里只入队（带 messageId），本机取信后回头调 /audio 由本 Worker 代取。
            const item = {
              id: 'm' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
              userId,
              srcType,
              srcId,
              kind: 'audio',
              replyToken,
              messageId: event.message.id || '',
              text: '',
              ts: Date.now(),
              state: 'pending',
            };
            await enqueue(env, item);
            if (!online && token) await pushToLine(userId, QUEUED_NOTICE, token);
          } else if (event.type === 'message' && event.message && event.message.type === 'image') {
            // 【2026-10-14】圖片消息：与语音同构 —— 本机没有 Channel Token，
            // 这里只入队（带 messageId），本机取信后回头调 /content 由本 Worker 代取图片。
            const item = {
              id: 'm' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
              userId,
              srcType,
              srcId,
              kind: 'image',
              replyToken,
              messageId: event.message.id || '',
              text: '',
              ts: Date.now(),
              state: 'pending',
            };
            await enqueue(env, item);
            if (!online && token) await pushToLine(userId, QUEUED_NOTICE, token);
          } else if (event.type === 'message' && event.message && event.message.type === 'file') {
            // 【2026-10-14】檔案（PDF/文件…）：同圖片處理，本機取信後回頭代取。
            const item = {
              id: 'm' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
              userId,
              srcType,
              srcId,
              kind: 'file',
              replyToken,
              messageId: event.message.id || '',
              fileName: (event.message.fileName || ''),
              text: '',
              ts: Date.now(),
              state: 'pending',
            };
            await enqueue(env, item);
            if (!online && token) await pushToLine(userId, QUEUED_NOTICE, token);
          } else if (event.type === 'join' && token) {
            // 【2026-10-13】被拉进群/多人房时主动打招呼，并说明这个群的用途。
            // 优先用 join 事件自带的 reply token（免费），失败才退回 push。
            const greet =
              '我是小姚 🐻 這個群我當成「記錄事情」的通道：\n' +
              '你們說的話我會按時間記下來，要查、要整理、要提醒，直接在群裡說一聲就好。';
            const rt = event.replyToken || '';
            const where = srcId || userId;
            let st = rt ? await replyToLine(rt, greet, token).catch(() => 0) : 0;
            if (!(st >= 200 && st < 300)) st = await pushToLine(where, greet, token);
            console.log('[line-worker] join 欢迎已发送 http=' + st + ' reftoken=' + !!rt);
          } else if (event.type === 'follow' && token) {
            await pushToLine(
              userId,
              '嗨！我是小姚 🐻 你的訊息我會收到。有話直接說，如果我沒馬上回，就是在忙，等一下就好。',
              token
            );
          }
        } catch (e) {
          console.error('[line-worker] 处理事件出错:', String(e));
        }
      }
      return new Response('OK', { status: 200 });   // 永远快速回 200，不给 LINE 重试的理由
    }

    // ---- 以下接口只给本机小姚用，必须带 key
    const key = request.headers.get('x-yao-key') || url.searchParams.get('k');
    if (!env.YAO_KEY || !timingSafeEqual(String(key || ''), env.YAO_KEY)) {
      return json({ ok: false, error: 'unauthorized' }, 401);
    }

    if (request.method === 'GET' && p === '/ping') {
      await setMeta(env, 'poll:last', String(Date.now()));
      return json({ ok: true, ts: Date.now() });
    }

    if (request.method === 'GET' && p === '/poll') {
      await setMeta(env, 'poll:last', String(Date.now()));
      const items = await poll(env);
      return json({ ok: true, items });
    }

    // 代取消息内容（语音 / 图片 / 文件通用）：由本 Worker 拿 Channel Token 去 LINE 取，
    // token 不下发到本机。/audio 是旧名，保留兼容；/content 是通用名。
    if (request.method === 'GET' && (p === '/audio' || p === '/content')) {
      const id = url.searchParams.get('id') || '';
      if (!id) return json({ ok: false, error: 'need id' }, 400);
      if (!env.LINE_CHANNEL_TOKEN) return json({ ok: false, error: 'no token' }, 500);
      const r = await fetch(
        'https://api-data.line.me/v2/bot/message/' + encodeURIComponent(id) + '/content',
        { headers: { Authorization: 'Bearer ' + env.LINE_CHANNEL_TOKEN } }
      );
      if (!r.ok) return json({ ok: false, error: 'line content HTTP ' + r.status }, 502);
      const buf = await r.arrayBuffer();
      return new Response(buf, {
        status: 200,
        headers: {
          'content-type': r.headers.get('content-type') || 'application/octet-stream',
          'content-length': String(buf.byteLength),
        },
      });
    }

    if (request.method === 'POST' && p === '/ack') {
      const b = await request.json().catch(() => ({}));
      await ack(env, b.ids || []);
      return json({ ok: true });
    }

    if (request.method === 'POST' && p === '/reply') {
      const b = await request.json().catch(() => ({}));
      // target 优先（群组回推）；老的本机版本只传 userId，保持兼容。
      const target = String(b.target || b.userId || '');
      if (!target || !b.text) return json({ ok: false, error: 'target and text required' }, 400);

      // 【2026-10-13】靜默約定（Jack 規定：群裡只有被叫「小姚」才回應）。
      // 本機判斷不該插話時，把回覆寫成 [靜默] → 這裡直接吞掉：不發送、不耗推播額度，但照樣 ack 讓訊息出佇列。
      if (/^\s*\[(靜默|静默|SILENT|silent)\]\s*$/.test(String(b.text))) {
        if (b.id) await ack(env, [b.id]);
        return json({ ok: true, silent: true });
      }

      let viaReply = null;
      let st = 0;
      if (b.replyToken) {
        // reply token 免费不占额度；过期/用掉会返回非 2xx，退回 push
        viaReply = await replyToLine(b.replyToken, b.text, env.LINE_CHANNEL_TOKEN).catch(() => 0);
        if (viaReply >= 200 && viaReply < 300) st = viaReply;
      }
      if (st < 200 || st >= 300) {
        // 【2026-10-15】Jack 規則：「LINE 如果來不及回應，可以等下次有叫你時一起回應。」
        // 回得太慢 → reply token 已過期 → 以前會退回 push（群組一次就扣 5 則）。
        // 帶 hold:true 時不 push，直接把「送出失敗」回報本機，由本機把回覆掛著，
        // 等他下次叫我（會有新的免費 reply token）再一起送出。訊息照樣 ack 出佇列，不重複處理。
        if (b.hold) {
          if (b.id) await ack(env, [b.id]);
          return json({ ok: false, held: true, httpStatus: st, viaReplyToken: false });
        }
        st = await pushToLine(target, b.text, env.LINE_CHANNEL_TOKEN);
      }
      let fallback = null;
      if ((st < 200 || st >= 300) && b.fallbackUserId && String(b.fallbackUserId) !== target) {
        // 群推失败（额度用尽 / 已被移出群 / room 不支持 push）→ 回退私聊，绝不静默丢
        const st2 = await pushToLine(
          b.fallbackUserId,
          '【群組回覆沒送出去｜HTTP ' + st + '】' + b.text,
          env.LINE_CHANNEL_TOKEN
        );
        fallback = st2;
      }
      if (b.id) await ack(env, [b.id]);
      return json({
        ok: st >= 200 && st < 300,
        httpStatus: st,
        viaReplyToken: !!viaReply,
        fallbackHttpStatus: fallback,
      });
    }

    if (request.method === 'GET' && p === '/quota') {
      // 免费方案的推播额度是本通道的隐形天花板，主动暴露出来，别等它悄悄断掉
      const out = { ok: true };
      try {
        const r1 = await fetch(LINE_API + '/message/quota', {
          headers: { Authorization: 'Bearer ' + env.LINE_CHANNEL_TOKEN },
        });
        out.quota = await r1.json().catch(() => null);
        const r2 = await fetch(LINE_API + '/message/quota/consumption', {
          headers: { Authorization: 'Bearer ' + env.LINE_CHANNEL_TOKEN },
        });
        out.consumption = await r2.json().catch(() => null);
      } catch (e) {
        out.ok = false;
        out.error = String(e);
      }
      return json(out);
    }

    if (request.method === 'GET' && p === '/events') {
      const n = Math.min(parseInt(url.searchParams.get('n') || '20', 10) || 20, 100);
      const rs = await env.DB.prepare(
        'SELECT ts, type, srcType, srcId, userId, raw FROM line_events ORDER BY ts DESC LIMIT ?'
      ).bind(n).all();
      return json({ ok: true, events: (rs && rs.results) || [] });
    }

    // 【2026-10-13】查群組成員與顯示名稱（診斷「群裡有誰」用）
    //   GET /members?groupId=Cxxxx  → 列出成員 id 與 displayName
    //   GET /members?userId=Uxxxx  → 查單一使用者資料
    if (request.method === 'GET' && p === '/members') {
      const gid = url.searchParams.get('groupId');
      const uid = url.searchParams.get('userId');
      const tk = env.LINE_CHANNEL_TOKEN;
      const out = { ok: true };
      try {
        if (gid) {
          const r1 = await fetch(
            LINE_API + '/group/' + encodeURIComponent(gid) + '/members/ids',
            { headers: { Authorization: 'Bearer ' + tk } }
          );
          const raw1 = await r1.text();
          out.status1 = r1.status;
          out.raw1 = raw1.slice(0, 300);
          let j1 = {};
          try { j1 = JSON.parse(raw1); } catch (_) {}
          const ids = j1.memberIds || [];
          out.groupId = gid;
          out.count = ids.length;
          out.members = [];
          for (const id of ids) {
            let nm = null;
            try {
              const r2 = await fetch(
                LINE_API + '/group/' + encodeURIComponent(gid) + '/member/' + encodeURIComponent(id),
                { headers: { Authorization: 'Bearer ' + tk } }
              );
              const j2 = await r2.json();
              nm = j2.displayName || null;
            } catch (_) {}
            out.members.push({ userId: id, displayName: nm });
          }
        } else if (uid) {
          const r2 = await fetch(LINE_API + '/profile/' + encodeURIComponent(uid), {
            headers: { Authorization: 'Bearer ' + tk },
          });
          out.profile = await r2.json();
        } else {
          return json({ ok: false, error: 'need ?groupId= or ?userId=' }, 400);
        }
      } catch (e) {
        out.ok = false;
        out.error = String(e);
      }
      return json(out);
    }

    if (request.method === 'GET' && p === '/status') {
      const c = await env.DB.prepare('SELECT COUNT(*) AS n FROM line_inbox').first();
      const last = parseInt(await getMeta(env, 'poll:last'), 10);
      const lastGroup = await getMeta(env, 'line:lastGroup').catch(() => null);
      return json({
        ok: true,
        online: await localOnline(env),
        lastPoll: Number.isFinite(last) ? new Date(last).toISOString() : null,
        queue: c ? Number(c.n) : 0,
        hasSecret: !!env.LINE_CHANNEL_SECRET,
        hasToken: !!env.LINE_CHANNEL_TOKEN,
        lastGroup: lastGroup ? JSON.parse(lastGroup) : null,
      });
    }

    // 主动推一条测试消息给你
    if (request.method === 'GET' && p === '/debug-ping') {
      const uid = url.searchParams.get('u');
      if (!uid) return json({ ok: false, error: 'need ?u=<LINE userId>' }, 400);
      const st = await pushToLine(uid, '【小姚・通道实测】这条是 Worker 主动推的。', env.LINE_CHANNEL_TOKEN);
      return json({ ok: st < 300, httpStatus: st });
    }

    return new Response('Not found', { status: 404 });
  },
};
