/**
 * LINE ↔ AI Agent 桥接 · 云端中继（Cloudflare Worker + D1）
 * ============================================================
 *
 * 它做什么：
 *   1. 收 LINE 官方 webhook（验签）→ 写进 D1 队列
 *   2. 本机守护进程（daemon）定时 /poll 取信 → 交给你的 AI Agent → /reply 推回 LINE
 *   3. 本机不在线时，用 Cloudflare Workers AI 兜底直接答（可选，不配就是离线排队）
 *
 * 为什么要有这一层（直连不是更简单？）：
 *   - LINE 要求 webhook 必须公网可达 → 你家电脑没有公网 IP
 *   - 直连需要把 Channel token 放本机 → 中继层让 token 只留在云端，不下发
 *   - 本机断网/关机时消息不会丢 → 队列兜底
 *
 * 存储：D1（不要用 KV —— 免费版 KV 每天只有 1000 次「写+列表」，
 *      几秒一次的轮询当天就会耗尽，整站 500。这是踩过的坑。）
 *
 * ── 绑定（wrangler.toml）─────────────────────────────────
 *   DB                     D1 数据库（必须）
 *   AI                     Workers AI（可选，用于云端兜底）
 *   OWNER_ID               （可选）你的 LINE userId；留空则第一条消息自动认领
 *   ALLOW_USERS            （可选）额外允许的 userId，逗号分隔
 *   AI_FALLBACK            "on"/"off"，默认 on（需绑 AI）
 *   SYSTEM_PROMPT          （可选）云端兜底时的系统提示词
 *
 * ── 密钥（wrangler secret put）───────────────────────────
 *   LINE_CHANNEL_SECRET    LINE channel secret（验签）
 *   LINE_CHANNEL_TOKEN     LINE channel access token（回推）
 *   BRIDGE_KEY             本机与云端之间的共享密钥（自己随便定一串）
 */

const TAKE_STALE_MS = 180 * 1000;        // 被取走但没确认，超过 3 分钟 → 重投或交云端
const MSG_MAX_AGE_MS = 6 * 3600 * 1000;  // 队列里超过 6 小时的直接丢弃
const MAX_QUEUE = 200;                   // 队列上限，防洪水
const ONLINE_WINDOW_MS = 90 * 1000;      // 本机多久没取信算离线
const REPLY_CHUNK = 4500;                // LINE 单条上限 5000，留余量
const HISTORY_KEEP = 20;                 // 每个用户保留的历史条数

const DEFAULT_SYSTEM =
  '你是一个通过 LINE 与用户对话的助手。回答简洁、口语化，不要用 Markdown 标记（LINE 不支持）。' +
  '不确定的事直说不确定，不要编造。';

// Workers AI 模型会下线，别写死单个 —— 按顺序试。
const AI_MODELS = [
  '@cf/meta/llama-3.3-70b-instruct-fp8-fast',
  '@cf/meta/llama-3.1-8b-instruct',
  '@cf/meta/llama-3-8b-instruct',
];

// ---------------------------------------------------------------- 工具

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });

function timingSafeEqual(a, b) {
  a = String(a || '');
  b = String(b || '');
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

/** LINE webhook 验签：x-line-signature = base64(HMAC-SHA256(channelSecret, body)) */
async function verifySignature(body, signature, secret) {
  if (!signature) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  const mac = await crypto.subtle.sign('HMAC', key, enc.encode(body));
  let bin = '';
  const bytes = new Uint8Array(mac);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return timingSafeEqual(btoa(bin), signature);
}

/** 长文本切成 LINE 能吃下的若干段 */
function chunkText(text, limit = REPLY_CHUNK) {
  text = String(text || '');
  if (text.length <= limit) return [text];
  const out = [];
  let cur = '';
  for (const line of text.split('\n')) {
    let seg = line;
    while (seg.length > limit) {
      if (cur) { out.push(cur); cur = ''; }
      out.push(seg.slice(0, limit));
      seg = seg.slice(limit);
    }
    if (cur.length + seg.length + 1 > limit) { out.push(cur); cur = seg; }
    else cur = cur ? cur + '\n' + seg : seg;
  }
  if (cur) out.push(cur);
  return out;
}

// ---------------------------------------------------------------- 存储（D1）

async function setMeta(env, k, v) {
  await env.DB.prepare('INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)').bind(k, String(v)).run();
}

async function getMeta(env, k) {
  const row = await env.DB.prepare('SELECT v FROM meta WHERE k = ?').bind(k).first();
  return row ? row.v : null;
}

/** 本机是否在线：最近 ONLINE_WINDOW_MS 内取过信 */
async function localOnline(env) {
  const last = parseInt(await getMeta(env, 'poll:last'), 10);
  return Number.isFinite(last) && Date.now() - last < ONLINE_WINDOW_MS;
}

/** 已认领的 owner（meta 优先，环境变量兜底） */
async function ownerId(env) {
  return (await getMeta(env, 'owner:id')) || env.OWNER_ID || '';
}

/** 是否允许这个用户（owner 未认领时，第一条消息自动认领） */
async function isAllowed(env, userId) {
  const owner = await ownerId(env);
  if (!owner) {
    await setMeta(env, 'owner:id', userId);
    return { allowed: true, claimed: true };
  }
  if (userId === owner) return { allowed: true };
  const extra = String(env.ALLOW_USERS || '').split(',').map((s) => s.trim()).filter(Boolean);
  return { allowed: extra.includes(userId) };
}

async function enqueue(env, item) {
  const now = Date.now();
  await env.DB.batch([
    // 顺手清理过期
    env.DB.prepare('DELETE FROM inbox WHERE ts < ?').bind(now - MSG_MAX_AGE_MS),
    env.DB.prepare(
      'INSERT OR REPLACE INTO inbox (id, user_id, text, kind, message_id, ts, state, taken_at) ' +
      "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)"
    ).bind(item.id, item.userId, item.text || '', item.kind || 'text', item.messageId || '', item.ts),
  ]);
  const c = await env.DB.prepare('SELECT COUNT(*) AS n FROM inbox').first();
  const n = c ? Number(c.n) : 0;
  if (n > MAX_QUEUE) {
    await env.DB
      .prepare('DELETE FROM inbox WHERE id IN (SELECT id FROM inbox ORDER BY ts ASC LIMIT ?)')
      .bind(n - MAX_QUEUE)
      .run();
  }
}

/** 取信：pending 的，以及「被取走太久没确认」的都交出去 */
async function poll(env) {
  const now = Date.now();
  const rs = await env.DB.prepare(
    'SELECT id, user_id, text, kind, message_id, ts FROM inbox ' +
    "WHERE ts > ? AND (state = 'pending' OR (state = 'taken' AND taken_at < ?)) " +
    'ORDER BY ts ASC LIMIT 100'
  ).bind(now - MSG_MAX_AGE_MS, now - TAKE_STALE_MS).all();
  const rows = (rs && rs.results) || [];
  if (!rows.length) return [];
  const stmt = env.DB.prepare("UPDATE inbox SET state = 'taken', taken_at = ? WHERE id = ?");
  await env.DB.batch(rows.map((r) => stmt.bind(now, r.id)));
  return rows.map((v) => ({
    id: v.id,
    userId: v.user_id,
    text: v.text || '',
    kind: v.kind || 'text',
    messageId: v.message_id || '',
    ts: v.ts,
  }));
}

async function ack(env, ids) {
  const list = (ids || []).filter(Boolean);
  if (!list.length) return;
  const stmt = env.DB.prepare('DELETE FROM inbox WHERE id = ?');
  await env.DB.batch(list.map((id) => stmt.bind(id)));
}

async function dropFromQueue(env, id) {
  if (id) await env.DB.prepare('DELETE FROM inbox WHERE id = ?').bind(id).run();
}

// 历史（给云端 AI 兜底做上下文）
async function pushHistory(env, userId, role, text) {
  await env.DB.prepare('INSERT INTO history (user_id, role, text, ts) VALUES (?, ?, ?, ?)')
    .bind(userId, role, String(text || '').slice(0, 4000), Date.now()).run();
  await env.DB.prepare(
    'DELETE FROM history WHERE user_id = ? AND id NOT IN ' +
    '(SELECT id FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?)'
  ).bind(userId, userId, HISTORY_KEEP).run();
}

async function getHistory(env, userId) {
  const rs = await env.DB.prepare(
    'SELECT role, text FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?'
  ).bind(userId, HISTORY_KEEP).all();
  const rows = (rs && rs.results) || [];
  return rows.reverse().map((r) => ({ role: r.role, content: r.text }));
}

// ---------------------------------------------------------------- LINE API

async function callLine(pathname, token, body, method = 'POST') {
  const res = await fetch('https://api.line.me' + pathname, {
    method,
    headers: {
      'content-type': 'application/json',
      Authorization: 'Bearer ' + token,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  return res.status;
}

/** 推消息给用户（超长自动分段） */
async function pushToLine(env, userId, text) {
  const token = env.LINE_CHANNEL_TOKEN;
  if (!token) return 0;
  let last = 0;
  const chunks = chunkText(text);
  for (const c of chunks) {
    last = await callLine('/v2/bot/message/push', token, {
      to: userId,
      messages: [{ type: 'text', text: c }],
    });
    if (last < 200 || last >= 300) return last;   // 单段失败就别往下发了
  }
  return last;
}

// ---------------------------------------------------------------- 云端 AI 兜底

async function aiAnswer(env, userId, text) {
  if (!env.AI) return null;
  if (String(env.AI_FALLBACK || 'on').toLowerCase() === 'off') return null;
  const history = await getHistory(env, userId);
  const messages = [
    { role: 'system', content: env.SYSTEM_PROMPT || DEFAULT_SYSTEM },
    ...history,
    { role: 'user', content: text },
  ];
  for (const model of AI_MODELS) {
    try {
      const out = await env.AI.run(model, { messages, max_tokens: 800 });
      const reply = (out && (out.response || out.result || '') || '').trim();
      if (reply) return reply;
    } catch (e) {
      console.error('[ai] 模型失败', model, String(e));
    }
  }
  return null;
}

/** 云端代答一条：AI → 推回 → 记历史 → 出队 */
async function answerInCloud(env, item, note) {
  const text = (item.text || '').trim();
  if (!text) { await dropFromQueue(env, item.id); return false; }
  let reply = await aiAnswer(env, item.userId, text);
  if (!reply) {
    if (note) await pushToLine(env, item.userId, note);
    return false;   // 没配 AI 就交回队列
  }
  const st = await pushToLine(env, item.userId, reply);
  const ok = st >= 200 && st < 300;
  if (ok) {
    await pushHistory(env, item.userId, 'user', text);
    await pushHistory(env, item.userId, 'assistant', reply);
    await dropFromQueue(env, item.id);
  }
  return ok;
}

/**
 * 收回卡住的消息（挂在 /poll 主链路上 —— 不要用 ctx.waitUntil，实测不跑）。
 * 被取走超过 TAKE_STALE_MS 还没 ack 的：能云端答就云端答，否则退回 pending 等本机重投。
 */
async function reviveStale(env) {
  const now = Date.now();
  const rs = await env.DB.prepare(
    "SELECT id, user_id, text, kind, message_id, ts FROM inbox " +
    "WHERE state = 'taken' AND taken_at < ? LIMIT 20"
  ).bind(now - TAKE_STALE_MS).all();
  const rows = (rs && rs.results) || [];
  if (!rows.length) return 0;
  let handled = 0;
  for (const r of rows) {
    const item = { id: r.id, userId: r.user_id, text: r.text, kind: r.kind, messageId: r.message_id };
    if (await answerInCloud(env, item, null)) handled++;
    else {
      // 云端答不了 → 退回队列，让本机重投
      await env.DB.prepare("UPDATE inbox SET state = 'pending', taken_at = NULL WHERE id = ?")
        .bind(r.id).run();
    }
  }
  return handled;
}

// ---------------------------------------------------------------- 入口

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const p = url.pathname;

    // ---- 健康检查（公开，不含敏感信息）
    if (request.method === 'GET' && p === '/') {
      return json({
        ok: true,
        service: 'line-agent-bridge',
        online: await localOnline(env),
        queue: await queueCount(env),
        ai: !!env.AI,
        time: new Date().toISOString(),
      });
    }

    // ---- LINE webhook（公开，必须验签）
    if (p === '/webhook') {
      if (request.method === 'GET') return new Response('OK', { status: 200 });
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });

      const raw = await request.text();
      const secret = env.LINE_CHANNEL_SECRET;
      if (secret) {
        const ok = await verifySignature(raw, request.headers.get('x-line-signature'), secret)
          .catch(() => false);
        if (!ok) return new Response('Bad signature', { status: 403 });
      } else {
        console.error('[webhook] LINE_CHANNEL_SECRET 未配置，本次跳过验签');
      }

      let body;
      try { body = JSON.parse(raw); } catch { return new Response('OK', { status: 200 }); }

      const online = await localOnline(env);
      for (const ev of body.events || []) {
        try {
          const userId = (ev.source || {}).userId;
          if (!userId) continue;
          const perm = await isAllowed(env, userId);

          if (!perm.allowed) {
            await pushToLine(env, userId, '抱歉，这个机器人只服务已授权用户。');
            continue;
          }
          if (perm.claimed) {
            await pushToLine(env, userId,
              '已把你设为本机器人的主人 ✅ 以后你的消息都收得到。');
          }

          if (ev.type === 'message' && ev.message && ev.message.type === 'text') {
            const text = (ev.message.text || '').trim();
            if (!text) continue;
            const item = {
              id: 'm' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
              userId, text, kind: 'text', ts: Date.now(),
            };
            // 本机不在线 → 有 AI 就直接答，别让它干等
            if (!online) {
              const handled = await answerInCloud(env, item, '(本机未在线，这是云端 AI 的回复)');
              if (handled) continue;
              await pushToLine(env, userId, '本机当前未在线，消息已排进队列，稍后回复。');
            }
            await enqueue(env, item);
          } else if (ev.type === 'message' && ev.message && ev.message.type === 'audio') {
            // 语音：本机没有 token 下载不了，只入队（带 messageId），本机取信后回头调 /audio
            const item = {
              id: 'm' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
              userId, kind: 'audio', messageId: ev.message.id || '', text: '', ts: Date.now(),
            };
            await enqueue(env, item);
            if (!online) await pushToLine(env, userId, '本机当前未在线，语音已排进队列。');
          } else if (ev.type === 'follow') {
            await pushToLine(env, userId, '你好！直接发消息就行，我会收到并回复。');
          }
        } catch (e) {
          console.error('[webhook] 事件处理出错', String(e));
        }
      }
      return new Response('OK', { status: 200 });   // 永远快速 200，别给 LINE 重试的理由
    }

    // ---- 以下接口只给本机守护用，必须带 key
    const key = request.headers.get('x-bridge-key') || url.searchParams.get('k');
    if (!env.BRIDGE_KEY || !timingSafeEqual(key, env.BRIDGE_KEY)) {
      return json({ ok: false, error: 'unauthorized' }, 401);
    }

    if (request.method === 'GET' && p === '/ping') {
      await setMeta(env, 'poll:last', Date.now());
      return json({ ok: true, ts: Date.now() });
    }

    if (request.method === 'GET' && p === '/poll') {
      await setMeta(env, 'poll:last', Date.now());
      await reviveStale(env);          // 挂在主链路上，别用 waitUntil
      const items = await poll(env);
      return json({ ok: true, items, online: true });
    }

    // 代取语音内容（token 不下发到本机）
    if (request.method === 'GET' && p === '/audio') {
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

    if (request.method === 'POST' && p === '/reply') {
      const b = await request.json().catch(() => ({}));
      if (!b.userId || !b.text) return json({ ok: false, error: 'userId and text required' }, 400);
      const st = await pushToLine(env, b.userId, b.text);
      if (b.id) await ack(env, [b.id]);
      await pushHistory(env, b.userId, 'user', b.userText || '(本机 Agent 处理)');
      await pushHistory(env, b.userId, 'assistant', b.text);
      return json({ ok: st >= 200 && st < 300, httpStatus: st });
    }

    if (request.method === 'POST' && p === '/ack') {
      const b = await request.json().catch(() => ({}));
      await ack(env, b.ids || []);
      return json({ ok: true });
    }

    // 本机接不了 → 主动交棒给云端 AI（别让用户干等）
    if (request.method === 'POST' && p === '/handoff') {
      const b = await request.json().catch(() => ({}));
      const id = b.id || url.searchParams.get('id');
      if (!id) return json({ ok: false, error: 'need id' }, 400);
      const row = await env.DB.prepare('SELECT * FROM inbox WHERE id = ?').bind(id).first();
      if (!row) return json({ ok: false, error: 'not found' }, 404);
      const item = {
        id: row.id, userId: row.user_id, text: row.text, kind: row.kind, messageId: row.message_id,
      };
      const answered = await answerInCloud(env, item, '(本机忙，这是云端 AI 的回复)');
      return json({ ok: true, answered: answered ? 1 : 0 });
    }

    // 手动收回所有卡住的消息
    if (request.method === 'POST' && p === '/revive') {
      const n = await reviveStale(env);
      return json({ ok: true, handled: n });
    }

    if (request.method === 'GET' && p === '/status') {
      const oid = await ownerId(env);
      return json({
        ok: true,
        online: await localOnline(env),
        lastPoll: await lastPollIso(env),
        queue: await queueCount(env),
        owner: oid ? '已设置' : '未认领（发一条消息即可认领）',
        ownerId: oid || null,      // 本机可读（已用 key 鉴权），用来主动推消息
        ai: !!env.AI,
        hasSecret: !!env.LINE_CHANNEL_SECRET,
        hasToken: !!env.LINE_CHANNEL_TOKEN,
      });
    }

    // 主动推一条测试消息
    if (request.method === 'GET' && p === '/debug-ping') {
      const uid = url.searchParams.get('u') || (await ownerId(env));
      if (!uid) return json({ ok: false, error: 'need ?u=<LINE userId>' }, 400);
      const st = await pushToLine(env, uid, '【通道实测】这条是云端主动推的，收到说明推送链路正常。');
      return json({ ok: st < 300, httpStatus: st });
    }

    return json({ ok: false, error: 'not found' }, 404);
  },
};

async function queueCount(env) {
  const c = await env.DB.prepare('SELECT COUNT(*) AS n FROM inbox').first();
  return c ? Number(c.n) : 0;
}

async function lastPollIso(env) {
  const last = parseInt(await getMeta(env, 'poll:last'), 10);
  return Number.isFinite(last) ? new Date(last).toISOString() : null;
}
