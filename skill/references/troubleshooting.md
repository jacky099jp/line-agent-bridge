# 排障与设计取舍

这份文档解释「为什么这么设计」以及踩过的坑。出问题时先看这里，再看代码。

## 1. 为什么队列用 D1，不用 KV

**踩坑实录**：最初用 Cloudflare KV 存队列，线上跑了不到一天整站开始 500：

```
/poll   → "KV put() limit exceeded for the day."
/status → "KV list() limit exceeded for the day."
```

免费版 KV 每天只有 **1000 次「写 + 列表」**。而取信是每几秒一次，
每次至少 1 次写（刷新取信时间）+ 1 次列表 → 一天上万次，**当天必然耗尽**。

更坑的是 `GET /` 走的是纯读操作（读额度 10 万/天），所以还正常 ——
**很容易误判成「通道没问题」**，实际收信已经全挂。

换 D1 后免费额度是 500 万行读 / 10 万行写每天，大两个数量级。
队列量级（个人使用）根本用不完。

> 教训：**看到「一部分接口正常、一部分 500」，别急着怀疑网络 —— 先抓真实异常堆栈。**
> `npx wrangler tail --format json` 能看到 HTTP 层看不到的内部错误。

## 2. 「本机在线」是怎么判的

判据：**云端最近 90 秒内被 `/poll` 或 `/ping` 访问过**（存在 meta 的 `poll:last`）。

这决定了两个行为：

- 本机不在线时，webhook 收到消息会**直接走云端 AI 兜底**，不让用户干等。
- 本机在线时，消息入队等本机取。

**为什么取信和处理要分两个线程**：如果串行，处理一条长消息（比如本地模型跑 5 分钟）期间
就不再取信 → 90 秒后云端判离线 → 用户新消息被云端接走，体验会「一会儿本机答、一会儿云端答」。
所以 daemon 里取信线程和处理器线程是分开的，取信永远不停。

## 3. 消息会不会丢 / 会不会重复

- **不会丢**：daemon 处理失败就**不 ack**，消息留在队列。
- **重投机制**：被取走超过 `TAKE_STALE_MS`（3 分钟）仍未确认 → 云端会再次交付
  （`/poll` 里同步跑 `reviveStale`，把卡住的交给云端 AI 或退回队列）。
- **可能重复**：如果处理器跑超过 3 分钟，消息可能被判定为「卡住」而重投。
  常见做法是把耗时任务改成「先回执，再异步推送结果」。

> ⚠️ 不要用 `ctx.waitUntil()` 做兜底 —— 实测在 Worker 里根本没跑起来。
> 兜底必须挂在主链路上（这就是 `reviveStale` 放在 `/poll` 里的原因）。

## 4. 无限重投的保护

处理器配置错了（比如命令路径写错），消息会一直失败。
daemon 对**同一条消息**最多重试 3 次，之后回复一句明确的错误提示并出队，
避免「每 3 分钟重试一次」的无限循环把日志刷爆。

日志里看到这类字样就要去 `line_ctl.py doctor` 查处理器：

```
[ERROR] 找不到命令：/path/to/your-ai
[ERROR] 消息 xxx 连续失败 3 次，回复错误提示并出队
```

## 5. launchd 的 PATH 陷阱

**症状**：终端里手动跑 `line_daemon.py` 一切正常，装成开机自启后就不工作或报
「找不到命令」。

**原因**：launchd 启动的进程继承的是极简 PATH（`/usr/bin:/bin` 之类），
不包括你的 `~/.nvm/...`、`/opt/homebrew/bin`、`~/.local/bin`。

**两种修法**（任选）：
1. `handler.cmd` 里写**绝对路径**（推荐）：
   ```bash
   which claude     # 例如 /Users/you/.nvm/versions/node/v20/bin/claude
   ```
   配成 `["/Users/you/.nvm/versions/node/v20/bin/claude", "-p", "{text}"]`

2. 在配置里补 PATH：
   ```json
   "handler": { "env": { "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" } }
   ```

`line_ctl.py doctor` 会检查命令是否可执行。

## 6. LINE 平台本身的限制（容易误当 bug）

| 限制 | 说明 |
|---|---|
| **回复 token 1 分钟过期** | 本方案不用 reply token，全走 push，所以不受此限 |
| **push 消息计费/配额** | 免费额度每月有限（Messaging API 免费 Plan 约 200 条/月），超了推不出去 |
| **单条消息 5000 字符** | 本方案自动分段（每 4500 字切一刀） |
| **webhook 必须 200 且够快** | 所以我们永远先回 200 再处理，绝不让 LINE 重试 |
| **一个 channel 只能有一个 webhook** | 别和 LINE 官方后台的 Bot 功能抢 |

查用量：LINE Developers Console → 你的 channel → Messaging API → 用量统计。

## 7. 常见错误码

| 现象 | 可能原因 |
|---|---|
| Worker `/webhook` 返回 403 Bad signature | `LINE_CHANNEL_SECRET` 与 channel 不匹配；或中间有代理改了 body |
| `/reply` 返回 `httpStatus: 401` | `LINE_CHANNEL_TOKEN` 无效或已撤销 → 重新 Issue |
| `/reply` 返回 `httpStatus: 429` | 推送频率超限 → 放慢，或检查是否配额用尽 |
| `/status` 里 `hasToken: false` | secret 没设上；`npx wrangler secret list` 确认 |
| daemon 一直「取信失败 HTTP 500」 | 去 `npx wrangler tail` 看 worker 内部报错（多半是 D1 表没建） |
| 队列数字长期不降 | 看是「本机在跑但慢」还是「本机根本没跑」—— 后者看 `line_ctl.py doctor` |

## 8. 安全边界

- **LINE token 只在云端**：本机没有 Channel token，语音等媒体由 Worker 代取
  （`/audio`），token 不下发。这样本机被入侵也不至于丢掉 LINE 控制权。
- **BRIDGE_KEY 只保护本机接口**：`/poll`、`/reply` 等必须带 key；
  请用 `wrangler secret put`，不要写进 `wrangler.toml` 提交到 git。
- **只回白名单**：主人认领后，其他人发消息只会收到「未授权」提示，不会触达你的 AI。
- **本机 config.json 建议 `chmod 600`**（含 BRIDGE_KEY）。
