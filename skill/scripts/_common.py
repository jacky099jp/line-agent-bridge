#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""line-agent-bridge skill 共用工具：定位配置、调用云端中继。

只用 Python 标准库，不装任何包。
配置与 daemon 共用同一份：~/.line-agent-bridge/config.json
"""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = os.path.expanduser("~/.line-agent-bridge/config.json")
UA = "line-agent-bridge/1.0 (+skill)"


def config_path():
    return os.environ.get("LINE_BRIDGE_CONFIG", DEFAULT_CONFIG)


def load_config(required=True):
    p = config_path()
    if not os.path.exists(p):
        if required:
            raise SystemExit(
                "找不到配置文件 %s\n"
                "先按安装向导配置，或从 daemon/config.example.json 复制一份改。" % p)
        return None
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["_path"] = p
    return cfg


def http(cfg, path, method="GET", body=None, timeout=15, raw=False):
    """调云端中继。返回 (status, dict|str|bytes)。"""
    base = str(cfg.get("worker_url") or "").rstrip("/")
    if not base:
        return 0, "配置里没有 worker_url"
    headers = {"User-Agent": UA, "x-bridge-key": cfg.get("bridge_key", "")}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
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


def daemon_pids():
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-f", "line_daemon.py"],
                           capture_output=True, text=True, timeout=10)
        return [x for x in (r.stdout or "").split("\n") if x.strip()]
    except Exception:
        return []


def handler_cmd(cfg):
    h = cfg.get("handler") or {}
    cmd = h.get("cmd") or []
    if isinstance(cmd, str):
        cmd = [cmd]
    return cmd


def which(exe):
    if not exe:
        return None
    if os.path.sep in exe:
        return exe if os.access(exe, os.X_OK) else None
    r = subprocess.run(["/usr/bin/which", exe], capture_output=True, text=True)
    return (r.stdout or "").strip() or None
