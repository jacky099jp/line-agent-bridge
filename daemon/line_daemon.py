#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LINE ↔ AI Agent 桥接 · 本机守护进程

它做什么：
  每隔几秒到云端中继取一次信 → 交给「你的 AI」处理 → 把回复推回 LINE。

「你的 AI」怎么接？在 config.json 的 handler 里选一种：
  cmd   —— 跑一条命令，把消息文本传进去，拿它的输出当回复。
           例：{"type": "cmd", "cmd": ["claude", "-p", "{text}"]}
           任何能「命令行进、文本出」的东西都行（本地大模型、脚本、自建服务）。
  cloud —— 本机不处理，直接交棒给云端的 Workers AI 兜底回答。

设计要点（都是踩过坑换来的）：
  · 取信与处理分两个线程 —— 处理一条慢消息时，取信照常，云端不会把本机判成离线。
  · 处理失败不确认（不 ack）→ 消息留在队列，云端超时后会用 AI 兜底，绝不丢。
  · 失败退避重试，网络抖动不会把日志刷爆。

用法：
  python3 line_daemon.py                     # 常驻运行
  python3 line_daemon.py --once              # 只跑一轮（调试）
  python3 line_daemon.py --dry-run           # 取信但不回复、不确认（只看会发生什么）
  python3 line_daemon.py --check             # 只检查配置与云端连通性
  python3 line_daemon.py --config /path/x.json
"""

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = os.path.expanduser("~/.line-agent-bridge/config.json")
UA = "line-agent-bridge/1.0 (+daemon)"

_stop = threading.Event()
_lock = threading.Lock()
_in_flight = set()
_pending = queue.Queue()
_fail_count = {}          # 消息 id → 连续失败次数（超过上限就明确报错并出队，避免无限重投）
MAX_RETRY = 3


# ---------------------------------------------------------------- 基础设施

def log(cfg, msg, level="INFO"):
    line = "[%s] [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    print(line, flush=True)
    path = (cfg or {}).get("log_file")
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                os.replace(path, path + ".1")       # 简单轮转，最多留一份
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def load_config(path):
    if not os.path.exists(path):
        sys.stderr.write(
            "找不到配置文件 %s\n"
            "可以从 config.example.json 复制一份改，或跑 skill 的安装向导。\n" % path)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["_path"] = path
    cfg.setdefault("poll_interval", 3)
    cfg.setdefault("ping_interval", 30)
    cfg.setdefault("handler", {})
    if cfg.get("log_file"):
        cfg["log_file"] = os.path.expanduser(cfg["log_file"])
    return cfg


def http(cfg, path, method="GET", body=None, timeout=15, raw=False):
    url = cfg["worker_url"].rstrip("/") + path
    headers = {"User-Agent": UA, "x-bridge-key": cfg.get("bridge_key", "")}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = resp.read()
            if raw:
                return resp.status, buf
            try:
                return resp.status, json.loads(buf.decode("utf-8", "ignore"))
            except ValueError:
                return resp.status, buf.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return e.code, "HTTP %s" % e.code
    except Exception as e:
        return 0, str(e)


# ---------------------------------------------------------------- 处理器

def run_handler(cfg, text, user_id):
    """把消息交给「你的 AI」，返回回复文本；返回 None 表示交给云端兜底。"""
    h = cfg.get("handler") or {}
    kind = (h.get("type") or "cloud").lower()

    if kind == "cloud":
        return None

    if kind != "cmd":
        log(cfg, "未知的 handler.type=%s，按 cloud 处理" % kind, "WARN")
        return None

    cmd_tpl = h.get("cmd")
    if not cmd_tpl:
        log(cfg, "handler.type=cmd 但没配 cmd", "ERROR")
        return None
    if isinstance(cmd_tpl, str):
        cmd_tpl = [cmd_tpl]

    cmd = [str(x).replace("{text}", text).replace("{user}", user_id) for x in cmd_tpl]
    timeout = int(h.get("timeout", 300))
    cwd = h.get("cwd") or None
    env = dict(os.environ)
    env.update(h.get("env") or {})
    env.setdefault("LINE_USER_ID", user_id)

    log(cfg, "执行处理器：%s" % " ".join(cmd[:3] + (["…"] if len(cmd) > 3 else [])), "DEBUG")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        log(cfg, "处理器超时（%d 秒）" % timeout, "WARN")
        return None
    except FileNotFoundError:
        log(cfg, "找不到命令：%s" % cmd[0], "ERROR")
        return None
    except Exception as e:
        log(cfg, "处理器异常：%s" % e, "ERROR")
        return None

    out = (p.stdout or "").strip()
    if p.returncode != 0:
        log(cfg, "处理器退出码 %s，stderr=%s" % (p.returncode, (p.stderr or "").strip()[:300]), "WARN")
        if not out:
            return None
    log(cfg, "处理器耗时 %.1f 秒，返回 %d 字" % (time.time() - t0, len(out)), "DEBUG")
    return out or None


def transcribe(cfg, message_id, user_id):
    """语音消息：本机没有 LINE token，由云端代取音频，再交给可选的 asr_cmd。"""
    asr = (cfg.get("handler") or {}).get("asr_cmd")
    if not asr:
        return None, "（还没配置语音识别，暂时听不懂语音消息）"
    st, buf = http(cfg, "/audio?id=" + urllib.parse.quote(message_id), timeout=60, raw=True)
    if st != 200 or not buf:
        return None, "（取语音失败 HTTP %s）" % st
    if isinstance(asr, str):
        asr = [asr]
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".m4a")
        with os.fdopen(fd, "wb") as fh:
            fh.write(buf)
        cmd = [str(x).replace("{file}", tmp) for x in asr]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        text = (p.stdout or "").strip()
        if text:
            return text, None
        return None, "（语音识别没返回结果）"
    except Exception as e:
        return None, "（语音识别失败：%s）" % e
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------- 消息处理

def handle(cfg, item):
    """处理一条消息。返回 True 表示已妥善处置（可出队）。"""
    mid = item.get("id")
    uid = item.get("userId")
    kind = item.get("kind") or "text"
    text = (item.get("text") or "").strip()

    if kind == "audio":
        text, err = transcribe(cfg, item.get("messageId") or "", uid)
        if not text:
            log(cfg, "语音处理失败：%s" % err)
            st, _ = http(cfg, "/reply", "POST",
                         {"userId": uid, "text": err or "（听不懂这条语音）", "id": mid})
            return st == 200
        log(cfg, "语音转写：%s" % text[:60])

    if not text:
        http(cfg, "/ack", "POST", {"ids": [mid]})
        return True

    log(cfg, "收到 %s：%s" % (uid[-8:], text[:80]))
    reply = run_handler(cfg, text, uid)

    if reply:
        prefix = cfg.get("reply_prefix") or ""
        if len(reply) > 4500:                        # 云端会自动分段，这里只做提示
            log(cfg, "回复较长（%d 字），将分段发送" % len(reply))
        st, body = http(cfg, "/reply", "POST", {"userId": uid, "text": prefix + reply, "id": mid},
                        timeout=30)
        ok = st == 200 and isinstance(body, dict) and body.get("ok")
        log(cfg, "已回复 %s（HTTP %s）" % (uid[-8:], st))
        return bool(ok)

    # 本机处理不了 → 交棒给云端 AI
    st, body = http(cfg, "/handoff", "POST", {"id": mid}, timeout=40)
    answered = isinstance(body, dict) and body.get("answered")
    if answered:
        log(cfg, "本机处理不了，已交云端 AI 回答")
        return True
    log(cfg, "交棒也失败（HTTP %s），留在队列稍后重试" % st, "WARN")
    return False


def worker_loop(cfg):
    while not _stop.is_set():
        try:
            item = _pending.get(timeout=1)
        except queue.Empty:
            continue
        mid = item.get("id")
        ok = False
        try:
            if cfg.get("_dry_run"):
                preview = str(item.get("text") or item.get("kind") or "")[:80]
                log(cfg, "[dry-run] 会处理：%s" % preview)
                ok = True
            else:
                ok = handle(cfg, item)
        except Exception as e:
            log(cfg, "处理消息异常：%s" % e, "ERROR")
        finally:
            with _lock:
                _in_flight.discard(mid)
        if ok:
            _fail_count.pop(mid, None)
            continue

        # 失败：重试有限次，避免同一条消息每 3 分钟无限循环
        n = _fail_count.get(mid, 0) + 1
        _fail_count[mid] = n
        if n >= MAX_RETRY and not cfg.get("_dry_run"):
            log(cfg, "消息 %s 连续失败 %d 次，回复错误提示并出队" % (mid, n), "ERROR")
            http(cfg, "/reply", "POST", {
                "userId": item.get("userId"),
                "text": "抱歉，本机处理这条消息失败了（已重试 %d 次）。\n"
                        "请检查本机守护的处理器配置（handler.cmd 能否正常执行）。" % n,
                "id": mid,
            }, timeout=20)
            _fail_count.pop(mid, None)
        else:
            log(cfg, "消息 %s 未完成（第 %d/%d 次），等云端重投" % (mid, n, MAX_RETRY), "WARN")


def poll_loop(cfg):
    interval = max(1, int(cfg.get("poll_interval", 3)))
    ping_every = max(1, int(cfg.get("ping_interval", 30)) // interval)
    fails = 0
    n = 0
    log(cfg, "开始取信：%s（每 %d 秒，处理器=%s）"
        % (cfg["worker_url"], interval, (cfg.get("handler") or {}).get("type", "cloud")))

    while not _stop.is_set():
        n += 1
        st, body = http(cfg, "/poll", timeout=15)
        if st == 200 and isinstance(body, dict):
            fails = 0
            for it in body.get("items") or []:
                mid = it.get("id")
                with _lock:
                    if mid in _in_flight:
                        continue
                    _in_flight.add(mid)
                _pending.put(it)
        else:
            fails += 1
            # 退避：连续失败就放慢，但保证 90 秒内至少 ping 一次（否则云端会把本机判离线）
            wait = min(30, interval * fails)
            log(cfg, "取信失败（HTTP %s，第 %d 次）：%s" % (st, fails, str(body)[:120]), "WARN")
            if fails >= 3:
                http(cfg, "/ping", timeout=10)
            if _stop.wait(wait):
                break
            continue

        if n % ping_every == 0:
            http(cfg, "/ping", timeout=10)

        if cfg.get("_once"):
            break
        if _stop.wait(interval):
            break


# ---------------------------------------------------------------- 入口

def do_check(cfg):
    print("配置：%s" % cfg["_path"])
    print("worker_url：%s" % cfg.get("worker_url"))
    print("bridge_key：%s" % ("已设置（%d 字符）" % len(cfg.get("bridge_key", ""))
                             if cfg.get("bridge_key") else "★ 缺失"))
    h = cfg.get("handler") or {}
    print("处理器：%s%s" % (h.get("type", "cloud"),
                          ("  cmd=" + str(h.get("cmd"))) if h.get("cmd") else ""))
    if h.get("type") == "cmd":
        exe = (h.get("cmd") or [""])[0]
        found = exe if os.path.sep in exe else subprocess.run(
            ["/usr/bin/which", exe], capture_output=True, text=True).stdout.strip()
        print("  命令可执行：%s" % (found or "★ 找不到 %s" % exe))
    ok = bool(cfg.get("worker_url")) and bool(cfg.get("bridge_key"))
    st, body = http(cfg, "/status", timeout=15)
    print("\n云端 /status → HTTP %s" % st)
    if isinstance(body, dict):
        for k in ("ok", "online", "queue", "owner", "ai", "hasToken"):
            if k in body:
                print("  %-10s %s" % (k, body[k]))
    else:
        print("  %s" % str(body)[:200])
    print("\n结论：%s" % ("配置可用 ✅" if ok and st == 200 else "有问题 ⚠️"))
    return 0 if ok and st == 200 else 1


def main():
    ap = argparse.ArgumentParser(description="LINE ↔ AI Agent 桥接 · 本机守护")
    ap.add_argument("--config", default=os.environ.get("LINE_BRIDGE_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（调试）")
    ap.add_argument("--dry-run", action="store_true", help="取信但不处理、不回复")
    ap.add_argument("--check", action="store_true", help="只检查配置与连通性")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg["_once"] = args.once
    cfg["_dry_run"] = args.dry_run

    if args.check:
        return do_check(cfg)

    def _sig(signum, frame):
        log(cfg, "收到停止信号，正在退出…")
        _stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    t = threading.Thread(target=worker_loop, args=(cfg,), daemon=True, name="handler")
    t.start()
    try:
        poll_loop(cfg)
        if args.once:
            # 等处理器把手上的消息做完再退出，否则调试模式看不到结果
            deadline = time.time() + 120
            while time.time() < deadline and not _stop.is_set():
                with _lock:
                    busy = bool(_in_flight) or not _pending.empty()
                if not busy:
                    break
                time.sleep(0.3)
            time.sleep(0.5)
    finally:
        _stop.set()
    log(cfg, "已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
