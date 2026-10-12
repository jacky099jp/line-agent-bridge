#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动推一条消息到 LINE（走云端中继，不依赖本机守护是否在跑）。

用法：
  python3 line_send.py "内容"
  python3 line_send.py --title "标题" "正文……"
  python3 line_send.py --file /path/to/report.md
  echo "内容" | python3 line_send.py
  python3 line_send.py --to Uxxxx "发给指定用户"
  python3 line_send.py --dry-run "先预览不真发"

默认发给「已认领的主人」（云端 /status 里的 ownerId）。
超长内容云端会自动分段（LINE 单条上限 5000 字符）。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


def main():
    args = sys.argv[1:]
    title = None
    src = None
    to_uid = None
    dry = False
    parts = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--title":
            i += 1
            title = args[i]
        elif a == "--file":
            i += 1
            src = args[i]
        elif a == "--to":
            i += 1
            to_uid = args[i]
        elif a == "--dry-run":
            dry = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            parts.append(a)
        i += 1

    if src:
        with open(src, "r", encoding="utf-8") as fh:
            text = fh.read()
    elif parts:
        text = " ".join(parts)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        sys.stderr.write('没有内容。用法：line_send.py "内容"\n')
        return 2

    text = text.strip()
    if not text:
        sys.stderr.write("内容为空，不发。\n")
        return 2
    if title:
        text = "%s\n\n%s" % (title, text)

    cfg = C.load_config()

    uid = to_uid
    if not uid:
        st, body = C.http(cfg, "/status")
        if st == 200 and isinstance(body, dict):
            uid = body.get("ownerId")
    if not uid:
        sys.stderr.write("不知道发给谁：云端还没认领主人。\n"
                         "先用你的 LINE 给机器人发一条消息，或用 --to Uxxxx 指定。\n")
        return 1

    if dry:
        print("【预览，未发送】目标=%s\n%s" % (uid[-8:], text[:500]))
        return 0

    t0 = time.time()
    st, body = C.http(cfg, "/reply", method="POST",
                      body={"userId": uid, "text": text}, timeout=30)
    ms = int((time.time() - t0) * 1000)
    ok = st == 200 and isinstance(body, dict) and body.get("ok")
    if ok:
        print("已送达 LINE（HTTP %s/%dms）" % (st, ms))
        return 0
    sys.stderr.write("发送失败：HTTP %s %s\n" % (st, str(body)[:200]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
