#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""line-bridge 主控台：体检 / 排障 / 运维 WorkBuddy ↔ LINE 通道。

用法：
  python3 line_ctl.py status        # 全链路体检（默认）
  python3 line_ctl.py ping          # 只探云端 worker
  python3 line_ctl.py queue         # 看 worker 队列与最近条目
  python3 line_ctl.py test          # 让 worker 主动推一条（验证 worker→LINE 那一跳）
  python3 line_ctl.py logs 60       # 桥接日志里 LINE 相关的最近 60 行
  python3 line_ctl.py whoami        # 显示当前定位到的配置（不打印密钥明文）
  python3 line_ctl.py worker-dir    # 显示 worker 源码目录
  python3 line_ctl.py deploy        # 部署/更新云端 worker（需 wrangler 已登录）
  python3 line_ctl.py status --fix  # 体检并尝试自愈（仅重启本机桥接）

判定口径：worker 200 且 hasToken 且 lastPoll ≤ 60 秒 → LINE 通。
"""

import datetime
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _line_common as L  # noqa: E402

BRIDGE_LABEL = "cn.yaowork.telegram-bridge"
POLL_STALE = 60          # lastPoll 超过这么久没更新 → 本机取信循环没在跑
HEARTBEAT_STALE = 120    # 桥接心跳超过这么久 → 主循环疑似卡死


def sh(cmd, timeout=20, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 999, "", str(e)


def parse_poll_age(last_poll):
    if not last_poll:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.datetime.strptime(last_poll, fmt)
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            return max(0, int((now - dt).total_seconds()))
        except ValueError:
            continue
    return None


def bridge_pids():
    rc, out, _ = sh(["/usr/bin/pgrep", "-f", "telegram-bridge/bridge.js"])
    return [p for p in out.split("\n") if p.strip()]


def read_heartbeat(d):
    try:
        import json
        with open(os.path.join(d, "heartbeat"), "r", encoding="utf-8") as fh:
            hb = json.load(fh)
        age = int(time.time() - int(hb.get("t", 0)) / 1000)
        return hb.get("state"), age
    except Exception:
        return None, None


def cmd_status(fix=False):
    d, cfg, url, key, uid = L.line_ctx()
    print("=== LINE 通道体检 ===")
    print("配置：%s" % os.path.join(d, "line.json"))
    print("worker：%s" % (url or "（未配置）"))
    print("owner_uid：%s" % (("…" + uid[-6:]) if uid else "（缺失）"))
    print("yao_key：%d 字符%s" % (len(key), "" if len(key) >= 20 else "  ★ 偏短，可疑"))
    print()

    problems = []

    # 1) 云端 worker
    t0 = time.time()
    st, body = L.http_json((url or "") + "/status", headers={"x-yao-key": key}, timeout=15)
    ms = int((time.time() - t0) * 1000)
    ok_worker = st == 200 and isinstance(body, dict) and body.get("ok")
    if ok_worker:
        last_poll = body.get("lastPoll")
        age = parse_poll_age(last_poll)
        print("[云端] ✅ HTTP 200 / %dms｜online=%s｜队列=%s｜secret=%s｜token=%s"
              % (ms, body.get("online"), body.get("queue"),
                 body.get("hasSecret"), body.get("hasToken")))
        print("       本机取信：%s" % (("%d 秒前" % age) if age is not None else "（无记录）"))
        if not body.get("hasToken"):
            problems.append("worker 侧 LINE_CHANNEL_TOKEN 缺失（无法回推给你）")
        if age is None or age > POLL_STALE:
            problems.append("本机取信循环没在跑（lastPoll %s）" % (last_poll or "空"))
    else:
        print("[云端] ❌ HTTP %s %s" % (st, str(body)[:160]))
        problems.append("worker /status 异常 —— 可能是额度/部署/网络问题，重启本机无用")

    # 2) 本机桥接（收信段跑在 bridge.js 里）
    pids = bridge_pids()
    state, hb_age = read_heartbeat(d)
    if pids:
        rc, up, _ = sh(["/bin/ps", "-o", "etime=", "-p", pids[0]])
        print("[本机] ✅ 桥接 pid %s（已跑 %s）｜主循环 state=%s%s"
              % (pids[0], up.strip() or "?", state,
                 ("，%d 秒前" % hb_age) if hb_age is not None else ""))
        if hb_age is not None and hb_age > HEARTBEAT_STALE:
            problems.append("桥接主循环心跳停在 %d 秒前（疑似卡死）" % hb_age)
    else:
        print("[本机] ❌ 桥接进程不在")
        problems.append("桥接进程不在（收信段停摆）")

    if fix and problems:
        if not pids or (hb_age is not None and hb_age > HEARTBEAT_STALE):
            print("\n→ 执行自愈：kickstart 桥接…")
            sh(["/bin/launchctl", "kickstart", "-k", "gui/%d/%s" % (os.getuid(), BRIDGE_LABEL)])
            time.sleep(10)
            st2, body2 = L.http_json((url or "") + "/status", headers={"x-yao-key": key}, timeout=15)
            if st2 == 200 and isinstance(body2, dict):
                age2 = parse_poll_age(body2.get("lastPoll"))
                if age2 is not None and age2 <= POLL_STALE:
                    print("   ✅ 桥接已重启，取信恢复到 %d 秒前" % age2)
                    problems = [p for p in problems if "取信循环" not in p]

    # 3) 部署源
    wd = L.worker_dir()
    if wd:
        idx = os.path.join(wd, "index.js")
        mt = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(idx))) if os.path.exists(idx) else "?"
        d1 = "D1" if "d1_databases" in open(os.path.join(wd, "wrangler.toml"), encoding="utf-8").read() else "KV"
        print("[源码] %s（index.js 改动 %s，存储=%s）" % (wd, mt, d1))
    else:
        print("[源码] 未定位到 worker 源码目录")

    print()
    if problems:
        print("结论：⚠️ 需要处理 —— " + "；".join(problems))
        return 1
    print("结论：LINE 全链路正常 ✅")
    return 0


def cmd_ping():
    d, cfg, url, key, uid = L.line_ctx()
    t0 = time.time()
    st, body = L.http_json(url + "/ping", headers={"x-yao-key": key}, timeout=15)
    print("GET /ping → HTTP %s / %dms  %s" % (st, int((time.time() - t0) * 1000), str(body)[:120]))
    return 0 if st == 200 else 1


def cmd_queue():
    d, cfg, url, key, uid = L.line_ctx()
    st, body = L.http_json(url + "/status", headers={"x-yao-key": key}, timeout=15)
    if st != 200 or not isinstance(body, dict):
        print("拿不到状态：HTTP %s %s" % (st, str(body)[:160]))
        return 1
    print("队列条数：%s" % body.get("queue"))
    print("最后取信：%s（online=%s）" % (body.get("lastPoll"), body.get("online")))
    print("提示：队列 >0 且长期不降 = 本机没在取信，跑 `line_ctl.py status` 看本机段。")
    return 0


def cmd_test():
    """让 worker 用 Channel Token 主动推一条 —— 验证 worker→LINE 那一跳。"""
    d, cfg, url, key, uid = L.line_ctx()
    if not uid:
        print("缺 owner_uid，无法指定收件人")
        return 1
    t0 = time.time()
    st, body = L.http_json(url + "/debug-ping?u=" + uid, headers={"x-yao-key": key}, timeout=20)
    ok = st == 200 and isinstance(body, dict) and body.get("ok")
    print("GET /debug-ping → HTTP %s / %dms  %s" % (st, int((time.time() - t0) * 1000), str(body)[:160]))
    print("✅ 已让 worker 主动推一条到你的 LINE" if ok else "❌ 推送失败")
    return 0 if ok else 1


def cmd_logs(n=60):
    d, _, _, _, _ = L.line_ctx()
    path = os.path.join(d, "bridge.log")
    if not os.path.exists(path):
        print("找不到 %s" % path)
        return 1
    rc, out, _ = sh(["/usr/bin/grep", "\\[LINE\\]", path])
    lines = [x for x in out.split("\n") if x.strip()]
    for ln in lines[-int(n):]:
        print(ln)
    print("（共 %d 行 LINE 日志，显示最后 %s 行）" % (len(lines), n))
    return 0


def cmd_whoami():
    d, cfg, url, key, uid = L.line_ctx()
    print("桥接目录：%s" % d)
    print("line.json：%s" % os.path.join(d, "line.json"))
    print("worker_url：%s" % url)
    print("owner_uid：%s" % (("…" + uid[-6:]) if uid else "（缺失）"))
    print("yao_key：%s（%d 字符）" % ("已配置" if key else "缺失", len(key)))
    wd = L.worker_dir()
    print("worker 源码：%s" % (wd or "未定位到"))
    return 0


def cmd_worker_dir():
    wd = L.worker_dir()
    print(wd or "未定位到 worker 源码目录（可用 LINE_WORKER_DIR 指定）")
    return 0 if wd else 1


def cmd_deploy():
    wd = L.worker_dir()
    if not wd:
        print("未定位到 worker 源码目录，无法部署")
        return 1
    wrangler = None
    for cand in ("wrangler",) + tuple(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/wrangler"))):
        if os.path.sep not in cand:
            rc, out, _ = sh(["/usr/bin/which", cand])
            if out:
                wrangler = out
                break
        elif os.path.exists(cand):
            wrangler = cand
            break
    if not wrangler:
        print("找不到 wrangler。请先安装或在 PATH 中提供（nvm 下：~/.nvm/versions/node/*/bin/wrangler）")
        return 1
    print("部署 %s → Cloudflare…" % wd)
    env = dict(os.environ, CI="1")
    p = subprocess.run([wrangler, "deploy"], cwd=wd, env=env,
                       capture_output=True, text=True, timeout=300)
    tail = "\n".join((p.stdout or "").split("\n")[-20:])
    print(tail)
    if p.returncode != 0:
        print("stderr:\n%s" % (p.stderr or "")[-800:])
    return p.returncode


def main():
    args = [a for a in sys.argv[1:]]
    fix = "--fix" in args
    args = [a for a in args if a != "--fix"]
    cmd = args[0] if args else "status"

    if cmd == "status":
        return cmd_status(fix=fix)
    if cmd == "ping":
        return cmd_ping()
    if cmd == "queue":
        return cmd_queue()
    if cmd == "test":
        return cmd_test()
    if cmd == "logs":
        n = args[1] if len(args) > 1 else 60
        return cmd_logs(n)
    if cmd == "whoami":
        return cmd_whoami()
    if cmd == "worker-dir":
        return cmd_worker_dir()
    if cmd == "deploy":
        return cmd_deploy()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
