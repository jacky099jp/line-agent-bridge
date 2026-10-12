# line-agent-bridge

**把你自己的 AI 接到 LINE 上。** 你在 LINE 里发消息，回你的不是某个陌生模型，而是
**你自己电脑上的 AI** —— Claude Code、本地大模型、或任何「命令行进、文本出」的程序。

本机不在线时，云端用 Cloudflare Workers AI 兜底回答，消息不会石沉大海。

```
手机 LINE
  → LINE 官方服务器
  → Cloudflare Worker（公网中继：验签 → 入队 → 推回）
        │                          ↑
        │                          └── 本机不在线 → Workers AI 兜底
        ↓
  本机守护 daemon（每几秒取信 → 交给你的 AI → 推回）
        ↓
  你的 AI（claude -p / 本地模型 / 自写脚本）
```

## 为什么需要中间那一层

你可能想问：直接让 LINE 打到本机不就行了？三个现实问题：

1. **LINE 要求 webhook 必须公网可达** —— 你家电脑没有公网 IP，Worker 就是那个「门牌号」。
2. **安全** —— 直连意味着把 LINE 的 Channel token 放在你家电脑上。这个架构里
   **token 只存在云端**，本机被入侵也丢不掉 LINE 控制权。
3. **可靠** —— 电脑关机、断网、重启时消息不会丢，躺在云端队列里等你回来。

## 特性

- **本机思考** —— 回复由你自己的 AI 产生，能读你本机的文件、跑你的命令、带你自己的上下文。
- **云端兜底** —— 本机不在线就交给 Workers AI，用户不会对着一片沉默发呆。
- **零依赖** —— Worker 只用 Cloudflare 内置能力；守护与脚本只用 Python 标准库，不装任何包。
- **不丢消息** —— 处理失败不确认，留在队列；卡住超过 3 分钟自动收回。
- **只服务授权者** —— 第一条消息的发送者自动成为主人，其他人会被挡在门外。
- **开机自启 + 崩溃自动重启** —— 附 macOS launchd 模板与一键安装脚本。
- **自带体检工具** —— 一条命令看清「中继 / 守护 / 处理器」哪一层出问题。

## 快速开始

```bash
git clone https://github.com/jacky099jp/line-agent-bridge.git
cd line-agent-bridge
```

**① 部署云端中继**

```bash
cd worker/
npx wrangler login
npx wrangler d1 create line-agent-bridge        # 把 database_id 填进 wrangler.toml
npx wrangler d1 execute line-agent-bridge --remote --file=./schema.sql
npx wrangler secret put LINE_CHANNEL_SECRET     # LINE Console 里的 Channel secret
npx wrangler secret put LINE_CHANNEL_TOKEN      # 同上的 access token
npx wrangler secret put BRIDGE_KEY              # 自己生成：python3 -c "import secrets;print(secrets.token_urlsafe(32))"
npx wrangler deploy                             # 记下 *.workers.dev 地址
```

**② 在 LINE 后台把 Webhook URL 指向它**

`https://<你的地址>.workers.dev/webhook`，点 Verify 通过后打开 Use webhook。

**③ 启动本机守护**

```bash
mkdir -p ~/.line-agent-bridge
cp daemon/config.example.json ~/.line-agent-bridge/config.json
chmod 600 ~/.line-agent-bridge/config.json
# 编辑 config.json：填 worker_url、bridge_key、handler.cmd

python3 daemon/line_daemon.py --check     # 自检
python3 daemon/line_daemon.py             # 前台跑起来试试
```

**④ 装上开机自启**

```bash
./daemon/install_daemon.sh
```

**⑤ 用你的 LINE 发一句话** —— 第一条消息会自动认领主人，然后就该有回复了。

```bash
python3 skill/scripts/line_ctl.py status   # 体检
python3 skill/scripts/line_ctl.py test     # 让云端推一条给你
```

> 完整的逐步说明（含每一步的验证方法）见 **[skill/SKILL.md](skill/SKILL.md)**。
> 如果你在用支持 skill 的 AI 客户端，把 `skill/` 装进去，直接对它说
> 「帮我把 LINE 接上我的 AI」，它会照着做。

## 目录结构

```
worker/     Cloudflare Worker 中继（webhook + D1 队列 + 推回 + AI 兜底）
daemon/     本机守护（取信 → 交给你的 AI → 推回）+ launchd 自启
skill/      给 AI 客户端用的技能包（安装向导、体检、推送）
```

## 环境要求

- 一个 Cloudflare 账号（免费版够用）
- 一个 LINE 官方账号 + Messaging API channel（免费）
- Node.js ≥ 18（只在部署时用）
- Python 3（守护与脚本只用标准库）
- macOS（自启用 launchd）；Linux 改 systemd 同样可用

## 常见问题

| 症状 | 看哪里 |
|---|---|
| 机器人完全没反应 | `npx wrangler tail` 看 Worker 日志；确认 Webhook Verify 通过 |
| 消息排着不动 | `python3 skill/scripts/line_ctl.py queue` 与 `logs` —— 多半是守护没跑 |
| 手动跑正常、开机自启不行 | 十有八九是 PATH：`handler.cmd` 写绝对路径（`line_ctl.py doctor` 会查） |
| 回的内容不对 | `config.json` 里 `handler.cmd` 手动跑一遍看输出 |
| 想全交给云端 AI | `handler.type` 改成 `"cloud"` |

更多见 [skill/references/troubleshooting.md](skill/references/troubleshooting.md)。

## 许可

MIT
