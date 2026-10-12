---
name: line-agent-bridge
description: |
  把 LINE 接上你自己的 AI Agent：你在 LINE 里发消息，由你本机的 AI（Claude Code、
  本地模型、任意命令行程序）回复，本机不在线时由云端 AI 兜底。
  用于：
  ① 安装部署这条通道（"把 LINE 接上我的 AI"、"做个 LINE 机器人"、"装 line bridge"）；
  ② 体检和排障（"LINE 没反应"、"line 又断了"、"我的机器人不回我"、"队列卡住"）；
  ③ 主动推送消息到 LINE（"发个 LINE 给我"、"把结果发到我 LINE"、定时日报、告警）。
  关键词：line、LINE、LINE bot、LINE 机器人、line bridge、webhook、cloudflare worker、
  消息队列、接上 AI、推送到 LINE。
agent_created: true
---

# line-agent-bridge

把 LINE 接到你自己的 AI Agent 上。**三层架构，排障时先分清是哪一层：**

```
手机 LINE
  → LINE 官方服务器
  → 【第 1 层】Cloudflare Worker（公网中继：验签、入队、推回）
        │                         ↑
        │                         └── 本机不在线时，用 Workers AI 兜底回答
        ↓
  【第 2 层】本机守护 daemon（轮询取信 → 交给你的 AI → 回推）
        ↓
  【第 3 层】你的 AI（任意命令行程序：claude -p / 本地模型 / 自写脚本）
```

| 层 | 位置 | 出问题的表现 |
|---|---|---|
| 中继 | `worker/`（Cloudflare） | worker 500、消息入不了队、推不出去 |
| 守护 | `daemon/`（本机常驻） | "本机没在取信"、消息排队不动 |
| 处理器 | `config.json` 的 `handler` | 收到了但回不出内容、回错东西 |

> 私有版（只服务单台机器、依赖特定客户端）请勿与本 skill 混用 —— 一套通道只留一套配置。

## 安装向导

按顺序来，每步都有验证方法。**别跳步**。

### 第 1 步：拿 LINE 的两个凭据

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 登录
2. 建 Provider → 建一个 **Messaging API** channel
3. 在 channel 的 **Basic settings** 复制 **Channel secret**
4. 在 **Messaging API** 标签页最下方 **Channel access token (long-lived)** 点 Issue，复制

还需要记住你的 **worker 随机密钥**（后面用，自己生成一串）：
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 第 2 步：部署云端中继（Cloudflare Worker）

需要 Node.js 和一个 Cloudflare 账号（免费版够用）。

```bash
cd worker/
npx wrangler login                      # 浏览器授权
npx wrangler d1 create line-agent-bridge   # 记下打印出来的 database_id
# 把 database_id 填进 wrangler.toml
npx wrangler d1 execute line-agent-bridge --remote --file=./schema.sql

npx wrangler secret put LINE_CHANNEL_SECRET   # 粘贴第 1 步的 Channel secret
npx wrangler secret put LINE_CHANNEL_TOKEN    # 粘贴第 1 步的 access token
npx wrangler secret put BRIDGE_KEY            # 粘贴你自己生成的那串

npx wrangler deploy                     # 记下输出的 *.workers.dev 地址
```

验证：
```bash
curl -s https://<你的地址>.workers.dev/     # 应返回 {"ok":true,...}
```

> 免费版 Workers AI 额度够个人用；不想用云端兜底就把 `wrangler.toml` 里的 `[ai]`
> 整段注释掉，并把 `AI_FALLBACK = "off"`。

### 第 3 步：把 webhook 指向 Worker

回到 LINE Developers Console 的 **Messaging API** 标签页：

- **Webhook URL** 填 `https://<你的地址>.workers.dev/webhook`
- 点 **Verify**（应显示 Success）
- **Use webhook** 打开
- 建议关掉 **Auto-reply messages**（不然机器人会双重回复）

### 第 4 步：配本机守护

```bash
mkdir -p ~/.line-agent-bridge
cp daemon/config.example.json ~/.line-agent-bridge/config.json
chmod 600 ~/.line-agent-bridge/config.json
```

编辑 `~/.line-agent-bridge/config.json`，至少改三处：

- `worker_url` → 第 2 步的地址
- `bridge_key` → 与 `BRIDGE_KEY` 同一串
- `handler.cmd` → 你的 AI。例如：
  - Claude Code：`["claude", "-p", "{text}"]`
  - 本地模型/自写脚本：`["/path/to/your-ai", "{text}"]`
  - 不想本机处理：把 `handler.type` 改成 `"cloud"`（全部交给云端 AI）

先自检：
```bash
python3 daemon/line_daemon.py --check
```

再前台试跑（先别急着装自启）：
```bash
python3 daemon/line_daemon.py --once     # 跑一轮看看
python3 daemon/line_daemon.py            # 常驻，Ctrl-C 退出
```

### 第 5 步：认领主人 + 测试

用你的 LINE 给机器人发一句话（第一次发的人自动成为主人，之后只服务这个人）。
然后：

```bash
python3 skill/scripts/line_ctl.py status   # 全链路体检
python3 skill/scripts/line_ctl.py test     # 让云端主动推一条给你
```

### 第 6 步：装开机自启（可选但推荐）

```bash
./daemon/install_daemon.sh
```

## 日常使用

```bash
S=~/.workbuddy/skills/line-agent-bridge/scripts   # 或本仓库的 skill/scripts

python3 $S/line_send.py "你好，这是一条主动推送"
python3 $S/line_send.py --title "每日摘要" --file /path/report.md
python3 $S/line_ctl.py status      # 体检
python3 $S/line_ctl.py queue       # 队列
python3 $S/line_ctl.py test        # 推送链路实测
python3 $S/line_ctl.py logs 80     # 看守护日志
python3 $S/line_ctl.py doctor      # 深度诊断（处理器、权限、自启）
```

## 硬规则

1. **只服务已授权的人**。主人由「第一条消息」自动认领；要加人就把 userId 填进
   `wrangler.toml` 的 `ALLOW_USERS` 再 deploy。
2. **不要用 KV 存队列** —— 免费版 KV 每天只有 1000 次「写 + 列表」，
   几秒一次的轮询当天就耗尽，整站 500。这里用 **D1**（免费额度大两个数量级）。
3. **`BRIDGE_KEY` 别写进 `wrangler.toml`**，用 `wrangler secret put`；
   本机 `config.json` 记得 `chmod 600`。
4. **处理失败不要 ack**（daemon 已这么做）—— 留在队列比丢掉好。
5. launchd 启动的进程 **PATH 很干净**：`handler.cmd` 里建议写**绝对路径**
   （`which claude` 查一下），否则会出现「手动跑得好好的，开机自启就不行」。

## 常见问题速查

| 症状 | 先查 | 处理 |
|---|---|---|
| 机器人完全没反应 | `line_ctl.py status` | worker 不通 → 看 Worker 日志（`npx wrangler tail`） |
| 消息排着不动 | `line_ctl.py queue` + `logs` | 守护没跑 → 启动；守护报错 → `doctor` |
| 收到了但回的是云端 AI | `queue` 看 online | 本机被判离线（90 秒没取信）→ 守护没跑或卡住 |
| 回的内容不对 | `config.json` 的 `handler.cmd` | 手动跑一遍那条命令看输出 |
| 开机后不回，手动跑就好 | `doctor` | 十有八九是 PATH 问题，命令写绝对路径 |
| Worker 报 500 | `npx wrangler tail` | 多半是 D1 没建表或绑定没配对 |

更多排障细节与踩过的坑，见 `references/troubleshooting.md`。

## 环境要求

- Node.js ≥ 18（部署 Worker 用）
- Python 3（守护与脚本只用标准库，无需 pip）
- macOS 用 launchd 自启；Linux 可自行改成 systemd
