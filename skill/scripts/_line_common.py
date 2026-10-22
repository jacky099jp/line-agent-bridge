#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""line-bridge 共用工具：定位配置、HTTP 调用、日志。

只用 Python 标准库，不依赖 telegram-bridge skill，也不装任何包。

【单一真源】LINE 的配置就是桥接目录下的 line.json
（worker_url / yao_key / owner_uid / note）。这里不复制、不改写它，
只定位并读取 —— 桥接进程（bridge.js）读的是同一份文件。
"""

import glob
import json
import os
import subprocess
import urllib.error
import urllib.request

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_RECORD = os.path.join(SKILL_DIR, "line_bridge_dir")

# Cloudflare 会按 UA 拦 Python-urllib 默认 UA（403 / error code 1010），
# 访问 *.workers.dev 的请求必须自带 User-Agent。
UA = "yao-line-bridge/1.0 (+mac-mini)"

DEFAULT_WORKER_DIR = os.path.expanduser("~/WorkBuddy/Claw/line-worker")


def _dir_candidates():
    """按可信度依次给出候选桥接目录。"""
    env = os.environ.get("TELEGRAM_BRIDGE_DIR")
    if env:
        yield env
    try:
        with open(DIR_RECORD, "r", encoding="utf-8") as fh:
            rec = fh.read().strip()
        if rec:
            yield rec
    except OSError:
        pass
    # 退路：扫描 ~/WorkBuddy/*/telegram-bridge（时间戳目录名，倒序=最新在前）
    for p in sorted(glob.glob(os.path.expanduser("~/WorkBuddy/*/telegram-bridge")), reverse=True):
        yield p


def find_line_dir(required=True):
    """定位含 line.json 的桥接目录。找不到时 required=True 直接退出。"""
    for d in _dir_candidates():
        if d and os.path.isfile(os.path.join(d, "line.json")):
            return d
    if required:
        raise SystemExit(
            "找不到含 line.json 的桥接目录 —— LINE 通道可能未启用。\n"
            "可设环境变量 TELEGRAM_BRIDGE_DIR 指定桥接目录。"
        )
    return None


def remember(d):
    """把定位结果记下来，下次免扫描。"""
    try:
        with open(DIR_RECORD, "w", encoding="utf-8") as fh:
            fh.write(d)
    except OSError:
        pass


def load_cfg(d):
    with open(os.path.join(d, "line.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def worker_dir():
    """定位 worker 源码目录（部署/回滚用）。"""
    for cand in (
        os.environ.get("LINE_WORKER_DIR"),
        os.path.join(SKILL_DIR, "worker_dir.txt"),
        DEFAULT_WORKER_DIR,
    ):
        if not cand:
            continue
        p = cand
        if p.endswith(".txt"):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    p = fh.read().strip()
            except OSError:
                continue
        if p and os.path.isfile(os.path.join(p, "wrangler.toml")):
            return p
    return None


def http_json(url, headers=None, timeout=10, method="GET", body=None, raw=False):
    """返回 (status, dict|str|bytes)。异常返回 (0, 错误字符串)。"""
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = resp.read()
            if raw:
                return resp.status, buf
            txt = buf.decode("utf-8", "ignore")
            try:
                return resp.status, json.loads(txt)
            except ValueError:
                return resp.status, txt
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return e.code, "HTTP %s" % e.code
    except Exception as e:
        return 0, str(e)


def owner_uid_from_log(d):
    """line.json 没写 owner_uid 时，从桥接日志里捞最近的 LINE userId。"""
    path = os.path.join(d, "bridge.log")
    try:
        p = subprocess.run(
            ["/usr/bin/grep", "-o", "-E", r"\[LINE\] 收到 (U[0-9a-f]+)", path],
            capture_output=True, text=True, timeout=10,
        )
        ids = [ln.split()[-1] for ln in (p.stdout or "").split("\n") if ln.strip()]
        if ids:
            return ids[-1]
    except Exception:
        pass
    return None


def line_ctx(required=True):
    """一次拿到 (目录, 配置, worker_url, key, owner_uid)。"""
    d = find_line_dir(required=required)
    if not d:
        return None
    remember(d)
    cfg = load_cfg(d)
    url = str(cfg.get("worker_url") or "").rstrip("/")
    key = cfg.get("yao_key") or ""
    uid = cfg.get("owner_uid") or owner_uid_from_log(d)
    return d, cfg, url, key, uid


def quota(url=None, key=None):
    """查 LINE 推播額度。返回 dict：{ok,total,used,remaining,pct,err}。

    【2026-10-13 實測】LINE 按「收件人數」計費：
      回覆（reply token）= 免費；私聊 push = 1 則；群組 push = 群組人數（本群 5 則）。
    """
    if url is None or key is None:
        ctx = line_ctx(required=False)
        if not ctx:
            return {"ok": False, "err": "找不到 line.json"}
        _d, _cfg, url, key, _uid = ctx
    st, b = http_json(str(url).rstrip("/") + "/quota", headers={"x-yao-key": key}, timeout=20)
    if st != 200 or not isinstance(b, dict) or not b.get("ok"):
        return {"ok": False, "err": "HTTP %s %s" % (st, str(b)[:160])}
    total = (b.get("quota") or {}).get("value")
    used = (b.get("consumption") or {}).get("totalUsage")
    if total is None or used is None:
        return {"ok": False, "err": "回傳缺欄位：%s" % json.dumps(b, ensure_ascii=False)[:160]}
    total, used = int(total), int(used)
    left = total - used
    return {"ok": True, "total": total, "used": used, "remaining": left,
            "pct": round(used * 100.0 / total, 1) if total else 0.0}
