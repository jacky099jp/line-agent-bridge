#!/usr/bin/env node
/**
 * Worker 逻辑测试（纯 Node，不需要 Cloudflare，也不需要联网）
 *
 *   node test/worker.test.mjs
 *
 * 做法：用内存里的假 D1 + 假 fetch 跑真实的 index.js，
 * 验证「入队 → 取信 → 回推 → 出队」整条链路与鉴权、白名单行为。
 */

import assert from 'node:assert';
import worker from '../index.js';

// ---------------------------------------------------------------- 假 D1

function createDB() {
  const inbox = new Map();
  const meta = new Map();
  const history = [];
  let hid = 0;

  const norm = (sql) => String(sql).replace(/\s+/g, ' ').trim();

  function run(sql, a) {
    const s = norm(sql);
    if (s.startsWith('INSERT OR REPLACE INTO meta')) { meta.set(a[0], a[1]); return { changes: 1 }; }
    if (s.startsWith('INSERT OR REPLACE INTO inbox')) {
      const [id, userId, text, kind, messageId, ts] = a;
      inbox.set(id, {
        id, user_id: userId, text, kind, message_id: messageId, ts,
        state: 'pending', taken_at: null,
      });
      return { changes: 1 };
    }
    if (s.startsWith('DELETE FROM inbox WHERE ts <')) {
      let n = 0;
      for (const [k, v] of [...inbox]) if (v.ts < a[0]) { inbox.delete(k); n++; }
      return { changes: n };
    }
    if (s.startsWith('DELETE FROM inbox WHERE id IN')) {
      const rows = [...inbox.values()].sort((x, y) => x.ts - y.ts).slice(0, a[0]);
      rows.forEach((r) => inbox.delete(r.id));
      return { changes: rows.length };
    }
    if (s.startsWith('DELETE FROM inbox WHERE id =')) {
      return { changes: inbox.delete(a[0]) ? 1 : 0 };
    }
    if (s.startsWith("UPDATE inbox SET state = 'taken'")) {
      const r = inbox.get(a[1]);
      if (r) { r.state = 'taken'; r.taken_at = a[0]; }
      return { changes: r ? 1 : 0 };
    }
    if (s.startsWith("UPDATE inbox SET state = 'pending'")) {
      const r = inbox.get(a[0]);
      if (r) { r.state = 'pending'; r.taken_at = null; }
      return { changes: r ? 1 : 0 };
    }
    if (s.startsWith('INSERT INTO history')) {
      history.push({ id: ++hid, user_id: a[0], role: a[1], text: a[2], ts: a[3] });
      return { changes: 1 };
    }
    if (s.startsWith('DELETE FROM history WHERE user_id')) {
      const keep = history.filter((h) => h.user_id === a[0])
        .sort((x, y) => y.id - x.id).slice(0, a[2]).map((h) => h.id);
      for (let i = history.length - 1; i >= 0; i--) {
        if (history[i].user_id === a[0] && !keep.includes(history[i].id)) history.splice(i, 1);
      }
      return { changes: 0 };
    }
    throw new Error('测试没覆盖到这条 SQL(run): ' + s);
  }

  function first(sql, a) {
    const s = norm(sql);
    if (s.startsWith('SELECT v FROM meta')) {
      return meta.has(a[0]) ? { v: meta.get(a[0]) } : null;
    }
    if (s.startsWith('SELECT COUNT(*) AS n FROM inbox')) return { n: inbox.size };
    if (s.startsWith('SELECT * FROM inbox WHERE id =')) return inbox.get(a[0]) || null;
    throw new Error('测试没覆盖到这条 SQL(first): ' + s);
  }

  function all(sql, a) {
    const s = norm(sql);
    if (s.includes("WHERE ts > ? AND (state = 'pending'")) {
      const rows = [...inbox.values()]
        .filter((r) => r.ts > a[0] && (r.state === 'pending' || (r.state === 'taken' && r.taken_at < a[1])))
        .sort((x, y) => x.ts - y.ts).slice(0, 100);
      return { results: rows };
    }
    if (s.includes("WHERE state = 'taken' AND taken_at <")) {
      return { results: [...inbox.values()].filter((r) => r.state === 'taken' && r.taken_at < a[0]).slice(0, 20) };
    }
    if (s.startsWith('SELECT role, text FROM history')) {
      return {
        results: history.filter((h) => h.user_id === a[0])
          .sort((x, y) => y.id - x.id).slice(0, a[1]).reverse(),
      };
    }
    throw new Error('测试没覆盖到这条 SQL(all): ' + s);
  }

  const makeStmt = (sql, args) => ({
    sql, args,
    bind: (...a) => makeStmt(sql, a),
    first: () => Promise.resolve(first(sql, args)),
    run: () => Promise.resolve(run(sql, args)),
    all: () => Promise.resolve(all(sql, args)),
  });

  return {
    prepare: (sql) => makeStmt(sql, []),
    batch: async (stmts) => stmts.map((s) => run(s.sql, s.args)),
    _dump: () => ({ inbox: [...inbox.values()], meta: Object.fromEntries(meta), history }),
  };
}

// ---------------------------------------------------------------- 测试脚手架

const sent = [];          // 记录推给 LINE 的消息
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.includes('api.line.me')) {
    try { sent.push(JSON.parse(opts.body || '{}')); } catch { sent.push({}); }
    return new Response(JSON.stringify({}), { status: 200 });
  }
  return realFetch(url, opts);
};

const KEY = 'k'.repeat(32);

function makeEnv(extra = {}) {
  return {
    DB: createDB(),
    BRIDGE_KEY: KEY,
    LINE_CHANNEL_TOKEN: 'dummy-token',
    LINE_CHANNEL_SECRET: '',
    AI_FALLBACK: 'off',
    ...extra,
  };
}

const auth = { 'x-bridge-key': KEY, 'content-type': 'application/json' };

let env;
const call = (path, init) => worker.fetch(new Request('https://t.workers.dev' + path, init), env);
const webhook = (body) => call('/webhook', {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
});
const textEvent = (uid, text, id = 'evt-' + Math.random().toString(36).slice(2, 8)) => ({
  events: [{ type: 'message', source: { userId: uid }, message: { type: 'text', id, text } }],
});

let passed = 0;
async function test(name, fn) {
  try {
    await fn();
    passed++;
    console.log('  ✓ ' + name);
  } catch (e) {
    console.error('  ✗ ' + name + '\n    ' + (e && e.message));
    process.exitCode = 1;
  }
}

// ---------------------------------------------------------------- 用例

console.log('\n== Worker 逻辑测试 ==\n');

await test('健康检查 / 返回 ok，且不含敏感字段', async () => {
  env = makeEnv();
  const r = await call('/');
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.equal(j.ok, true);
  assert.equal(j.service, 'line-agent-bridge');
  assert.ok(!('hasToken' in j), '公开接口不应暴露 token 状态');
});

await test('没有 key 访问受保护接口 → 401', async () => {
  env = makeEnv();
  for (const p of ['/poll', '/status', '/ping', '/revive']) {
    const r = await call(p);
    assert.equal(r.status, 401, p + ' 应要求鉴权');
  }
});

await test('webhook 入队 + 自动认领主人', async () => {
  env = makeEnv();
  sent.length = 0;
  await call('/ping', { headers: auth });                 // 先让云端认为本机在线
  const r = await webhook(textEvent('Ualice', '你好'));
  assert.equal(r.status, 200);

  const st = await (await call('/status', { headers: auth })).json();
  assert.equal(st.queue, 1, '消息应已入队');
  assert.equal(st.ownerId, 'Ualice', '第一条消息应认领主人');
  assert.ok(sent.some((m) => String(m.messages?.[0]?.text || '').includes('主人')), '应通知认领成功');
});

await test('陌生用户被拒绝，且不进队列', async () => {
  env = makeEnv();
  await call('/ping', { headers: auth });
  await webhook(textEvent('Ualice', '我先来'));
  const before = (await (await call('/status', { headers: auth })).json()).queue;
  await webhook(textEvent('Ubob', '我是别人'));
  const after = await (await call('/status', { headers: auth })).json();
  assert.equal(after.queue, before, '陌生用户的消息不应进队列');
});

await test('完整链路：取信 → 回推 → 出队', async () => {
  env = makeEnv();
  sent.length = 0;
  await call('/ping', { headers: auth });
  await webhook(textEvent('Ualice', '帮我查一下'));

  const polled = await (await call('/poll', { headers: auth })).json();
  assert.equal(polled.items.length, 1);
  assert.equal(polled.items[0].text, '帮我查一下');
  assert.equal(polled.items[0].userId, 'Ualice');

  const before = sent.length;
  const rep = await (await call('/reply', {
    method: 'POST', headers: auth,
    body: JSON.stringify({ userId: 'Ualice', text: '查到了：42', id: polled.items[0].id }),
  })).json();
  assert.equal(rep.ok, true);
  assert.equal(sent.length, before + 1, '应推了一条给 LINE');
  assert.equal(sent[sent.length - 1].messages[0].text, '查到了：42');

  const st = await (await call('/status', { headers: auth })).json();
  assert.equal(st.queue, 0, '已确认的消息应出队');
});

await test('同一条消息不会被重复取走（taken 保护）', async () => {
  env = makeEnv();
  await call('/ping', { headers: auth });
  await webhook(textEvent('Ualice', '只应出现一次'));
  const a = await (await call('/poll', { headers: auth })).json();
  const b = await (await call('/poll', { headers: auth })).json();
  assert.equal(a.items.length, 1);
  assert.equal(b.items.length, 0, '刚取走的消息不应立刻重投');
});

await test('长回复自动分段（每段不超 LINE 上限）', async () => {
  env = makeEnv();
  sent.length = 0;
  const long = Array.from({ length: 1200 }, (_, i) => '第' + (i + 1) + '行内容').join('\n');
  assert.ok(long.length > 4500, '测试数据要够长，实际 ' + long.length);
  await call('/reply', {
    method: 'POST', headers: auth,
    body: JSON.stringify({ userId: 'Ualice', text: long }),
  });
  assert.ok(sent.length >= 2, '超长文本应被切成多段，实际 ' + sent.length + ' 段');
  for (const m of sent) {
    assert.ok(m.messages[0].text.length <= 5000, '每段不应超过 LINE 的 5000 上限');
  }
});

await test('云端 AI 未配置时，兜底路径不会丢消息', async () => {
  env = makeEnv({ AI_FALLBACK: 'on' });   // 开了兜底但没有 AI 绑定
  sent.length = 0;
  const r = await webhook(textEvent('Ualice', '本机不在线时发的'));
  assert.equal(r.status, 200);
  const st = await (await call('/status', { headers: auth })).json();
  assert.equal(st.queue, 1, '没有 AI 时应把消息留在队列，而不是丢掉');
  assert.equal(st.ai, false);
});

await test('卡住的消息可被 /revive 收回', async () => {
  env = makeEnv();
  await call('/ping', { headers: auth });
  await webhook(textEvent('Ualice', '卡住的消息'));
  const polled = await (await call('/poll', { headers: auth })).json();
  const id = polled.items[0].id;

  // 手工把 taken_at 往前拨 5 分钟，模拟卡住
  const db = env.DB._dump();
  const row = db.inbox.find((r) => r.id === id);
  row.taken_at = Date.now() - 5 * 60 * 1000;

  const rv = await (await call('/revive', { method: 'POST', headers: auth })).json();
  assert.equal(rv.ok, true);
  const after = env.DB._dump().inbox.find((r) => r.id === id);
  assert.ok(after, '消息不应被删掉');
  assert.equal(after.state, 'pending', '没有云端 AI 时应退回队列等重投');
});

await test('unknown 路径 → 404', async () => {
  env = makeEnv();
  const r = await call('/nope', { headers: auth });
  assert.equal(r.status, 404);
});

globalThis.fetch = realFetch;
console.log('\n== 通过 ' + passed + ' 项 ==\n');
