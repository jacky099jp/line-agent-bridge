#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""line-agent-bridge 主控台：体检 / 排障 / 运维。

用法：
  python3 line_ctl.py status      # 全链路体检（默认）
  python3 line_ctl.py ping        # 只探云端中继
  python3 line_ctl.py queue       # 看云端队列与最后取信时间
  python3 line_ctl.py test        # 让云端主动推一条到 LINE（验证推送链路）
  python3 line_ctl.py logs 80     # 看本机守护最近 80 行日志
  python3 line_ctl.py doctor      # 深度诊断（处理器可执行性、开机自启、权限）
"""

import datetime
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

POLL_STALE = 90          # 云端判本机在线的窗口
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.example.line-agent-bridge.plist")
LABEL = "com.example.line-agent-bridge"


def poll_age(iso):
    if not iso:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.datetime.strptime(iso, fmt)
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            return max(0, int((now - dt).total_seconds()))
        except ValueError:
            continue
    return None


def cmd_status():
    cfg = C.load_config()
    print("=== LINE Agent Bridge 体检 ===\n")
    print("配置：%s" % cfg["_path"])
    print("中继：%s" % cfg.get("worker_url"))
    print("密钥：%s" % ("已设置（%d 字符）" % len(cfg.get("bridge_key", ""))
                       if cfg.get("bridge_key") else "★ 缺失"))
    h = cfg.get("handler") or {}
    print("处理器：%s%s\n" % (h.get("type", "cloud"),
                            ("  " + " ".join(C.handler_cmd(cfg))) if C.handler_cmd(cfg) else ""))

    problems = []

    # 1) 云端
    t0 = time.time()
    st, body = C.http(cfg, "/status")
    ms = int((time.time() - t0) * 1000)
    if st == 200 and isinstance(body, dict) and body.get("ok"):
        age = poll_age(body.get("lastPoll"))
        print("[云端] ✅ HTTP 200 / %dms" % ms)
        print("       本机在线=%s｜队列=%s｜云端AI=%s｜token=%s"
              % (body.get("online"), body.get("queue"), body.get("ai"), body.get("hasToken")))
        print("       最后取信：%s" % (("%d 秒前" % age) if age is not None else "（无记录）"))
        if not body.get("hasToken"):
            problems.append("云端缺少 LINE_CHANNEL_TOKEN（推不出去）")
        if not body.get("ownerId"):
            problems.append("还没有主人 —— 用你的 LINE 给机器人发一条消息即可认领")
        if age is None or age > POLL_STALE:
            problems.append("本机守护没有在取信（最后 %s）" % (body.get("lastPoll") or "无"))
    else:
        print("[云端] ❌ HTTP %s  %s" % (st, str(body)[:160]))
        problems.append("云端中继不可达或报错")

    # 2) 本机守护
    pids = C.daemon_pids()
    if pids:
        r = os.popen("/bin/ps -o etime=,command= -p %s" % pids[0]).read().strip()
        print("[本机] ✅ 守护运行中（pid %s）" % pids[0])
        logf = cfg.get("log_file")
        if logf and os.path.exists(logf):
            age = int(time.time() - os.path.getmtime(logf))
            print("       日志最后写入：%s" % ("%d 秒前" % age if age < 86400 else "很久以前"))
    else:
        print("[本机] ❌ 守护进程不在")
        problems.append("本机守护没在跑（launchctl 或手动启动）")

    print()
    if problems:
        print("结论：⚠️ 需要处理 —— " + "；".join(problems))
        return 1
    print("结论：全链路正常 ✅")
    return 0


def cmd_ping():
    cfg = C.load_config()
    t0 = time.time()
    st, body = C.http(cfg, "/ping")
    print("GET /ping → HTTP %s / %dms  %s" % (st, int((time.time() - t0) * 1000), str(body)[:120]))
    return 0 if st == 200 else 1


def cmd_queue():
    cfg = C.load_config()
    st, body = C.http(cfg, "/status")
    if st != 200 or not isinstance(body, dict):
        print("拿不到状态：HTTP %s %s" % (st, str(body)[:160]))
        return 1
    print("队列：%s 条" % body.get("queue"))
    print("最后取信：%s（在线=%s）" % (body.get("lastPoll"), body.get("online")))
    print()
    print("说明：队列数字包含「等待取」+「本机正在处理」+「过期残留」。")
    print("      如果长期不降，且守护日志没有新的「收到」，才说明卡住了。")
    return 0


def cmd_test():
    cfg = C.load_config()
    st, body = C.http(cfg, "/status")
    if not (st == 200 and isinstance(body, dict)):
        print("先拿不到云端状态：HTTP %s" % st)
        return 1
    uid = body.get("ownerId")
    if not uid:
        print("还没有 owner —— 先用你的 LINE 给机器人发一条消息，认领后再试。")
        return 1
    t0 = time.time()
    st2, b2 = C.http(cfg, "/debug-ping?u=" + urllib.parse.quote(uid), timeout=20)
    ok = st2 == 200 and isinstance(b2, dict) and b2.get("ok")
    print("GET /debug-ping → HTTP %s / %dms  %s" % (st2, int((time.time() - t0) * 1000), str(b2)[:160]))
    print("✅ 已让云端主动推一条到你的 LINE，收到即推送链路正常。" if ok else "❌ 推送失败")
    return 0 if ok else 1


def cmd_logs(n=80):
    cfg = C.load_config(required=False) or {}
    path = cfg.get("log_file") or os.path.expanduser("~/.line-agent-bridge/daemon.log")
    if not os.path.exists(path):
        print("找不到日志 %s" % path)
        return 1
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        lines = fh.readlines()
    for ln in lines[-int(n):]:
        print(ln.rstrip())
    print("\n（共 %d 行，显示最后 %s 行）" % (len(lines), n))
    return 0


def cmd_doctor():
    cfg = C.load_config(required=False)
    print("=== 深度诊断 ===\n")
    issues = []

    if cfg:
        cmd = C.handler_cmd(cfg)
        h = cfg.get("handler") or {}
        if (h.get("type") or "cloud").lower() == "cmd":
            if not cmd:
                issues.append("handler.type=cmd 但没配 cmd")
                print("[处理器] ❌ 没配置命令")
            else:
                found = C.which(cmd[0])
                print("[处理器] %s %s" % ("✅" if found else "❌", " ".join(cmd)))
                if not found:
                    issues.append("处理器命令找不到：%s" % cmd[0])
                else:
                    print("         实际路径：%s" % found)
                    print("         提示：launchd 启动的进程 PATH 很干净，"
                          "命令行里建议写绝对路径，或在配置的 handler.env 里补 PATH。")
        else:
            print("[处理器] cloud（本机不处理，交给云端 AI 兜底）")
    else:
        issues.append("找不到配置文件")
        print("[配置] ❌ 找不到 %s" % C.config_path())

    p = C.config_path()
    if os.path.exists(p):
        mode = oct(os.stat(p).st_mode & 0o777)
        ok = (os.stat(p).st_mode & 0o077) == 0
        print("[权限] %s %s（含密钥，建议 chmod 600）" % ("✅" if ok else "⚠️", mode))
        if not ok:
            issues.append("配置文件权限过宽，建议 chmod 600 %s" % p)

    if os.path.exists(PLIST):
        print("[自启] ✅ 已安装 %s" % LABEL)
    else:
        print("[自启] ⚠️ 未安装开机自启（daemon/install_daemon.sh）")

    pids = C.daemon_pids()
    print("[进程] %s" % ("✅ 守护在跑（pid %s）" % pids[0] if pids else "❌ 守护没在跑"))

    print()
    print("结论：%s" % ("✅ 没发现问题" if not issues else "⚠️ " + "；".join(issues)))
    return 0 if not issues else 1


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "status":
        return cmd_status()
    if cmd == "ping":
        return cmd_ping()
    if cmd == "queue":
        return cmd_queue()
    if cmd == "test":
        return cmd_test()
    if cmd == "logs":
        return cmd_logs(args[1] if len(args) > 1 else 80)
    if cmd == "doctor":
        return cmd_doctor()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
