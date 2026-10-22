#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动推一条消息到 Jack 的 LINE。

走云端 worker 的 /reply（LINE push API），**不经过本机网关** ——
所以 WorkBuddy 忙、桥接排队时这条依然发得出去。

用法：
  python3 line_send.py "内容"
  python3 line_send.py --title "每日 AI 新闻" "正文……"
  python3 line_send.py --file /path/to/report.md     # 从文件读
  echo "内容" | python3 line_send.py                  # 管道
  python3 line_send.py --dry-run "先预览不真发"
  python3 line_send.py --to Uxxxx "发给指定 userId"
  python3 line_send.py --group "发到 Jack 的『记录事情』LINE 群（line.json 的 group_id）"
                       # 群推失败会自动回退到 Jack 私聊，不会静默丢失
  python3 line_send.py --force "…"   # 默认拒绝主动推；要真发必须显式 --force

【铁律一 — 2026-10-13 23:12 Jack 指示】「測試報告一律去 tele，line 你回復就不扣，
  以後就這樣，先不主動，保持暢通」
  → **本脚本默认拒绝一切主动推送**（私聊、群组都不发），只保留「回复」通道（免费）。
     任务／测试／报告类通知一律走 tele：
       python3 ~/.workbuddy/skills/telegram-bridge/scripts/tg_send.py "内容"
     确实要主动发 LINE（Jack 当场要求）才加 --force，此时才会真的扣额度。

【铁律二 — 计费实测】LINE 按「收件人数」扣额度：
  私聊 push = 1 则    群组 push = 群组人数（本群 5 人 = 5 则）   回覆(reply token) = 免费
  即使 --force，发前仍会查额度：剩额不足会**拒绝发送**，免得把额度烧穿。

成功时输出「已送达 LINE（HTTP 200/…ms）」。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _line_common as L  # noqa: E402

# LINE 文字消息单条上限 5000 字符；留余量，按段落切。
MAX_LEN = 4500

# 【2026-10-13 23:12 Jack 指示】LINE 全面停止主动推送，只留「回复」（免费）。
#   测试报告 / 通知 / 提醒 一律走 tele。要主动发 LINE 必须显式 --force。
#   这行是硬开关，不是提示 —— 我（AI）也不该在没被要求时绕过它。
ACTIVE_PUSH_DISABLED = True

TG_SEND = os.path.expanduser("~/.workbuddy/skills/telegram-bridge/scripts/tg_send.py")

# 额度保命线：剩额低于此值时拒绝推送（除非 --force）。
# 理由：额度一旦耗尽，当月「主动推」全部静默失败；宁可少发，不可发不出。
QUOTA_FLOOR = 30
# 群组一条消息按人数计费；line.json 没写 group_size 时的保守估计（本群实测 5 人）。
GROUP_SIZE_DEFAULT = 5


def split_text(text, limit=MAX_LEN):
    """按段落切成不超过 limit 的若干段。"""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n"):
        # 单段本身就超长 → 硬切
        while len(para) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = (cur + "\n" + para) if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def main():
    args = sys.argv[1:]
    title = None
    src_file = None
    to_uid = None
    use_group = False
    dry = False
    force = False
    parts = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--title":
            i += 1
            title = args[i]
        elif a == "--file":
            i += 1
            src_file = args[i]
        elif a == "--to":
            i += 1
            to_uid = args[i]
        elif a == "--group":
            use_group = True
        elif a == "--dry-run":
            dry = True
        elif a == "--force":
            force = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            parts.append(a)
        i += 1

    if src_file:
        with open(src_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    elif parts:
        text = " ".join(parts)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        sys.stderr.write("没有内容。用 `line_send.py \"内容\"` 或 --file / 管道输入。\n")
        return 2

    text = text.strip()
    if not text:
        sys.stderr.write("内容为空，不发。\n")
        return 2
    if title:
        text = "%s\n\n%s" % (title, text)

    ctx = L.line_ctx()
    d, cfg, url, key, owner_uid = ctx
    group_id = (cfg or {}).get("group_id") or ""
    fallback = None
    if to_uid:
        uid = to_uid
    elif use_group:
        if not group_id:
            sys.stderr.write("line.json 里没有 group_id，无法发群。\n")
            return 1
        uid = group_id
        fallback = owner_uid          # 群推失败（被移出群/额度用尽）→ 回退私聊，绝不静默丢
    else:
        uid = owner_uid
    if not (url and key and uid):
        sys.stderr.write("配置不全：worker_url=%s key=%s owner_uid=%s\n"
                         % (bool(url), bool(key), bool(uid)))
        return 1

    chunks = split_text(text)

    # 【额度守门】发前先算这次要花多少，剩额不够就拒发（除非 --force）。
    #   群推 1 条 = 群组人数（本群 5 则）；私聊 1 条 = 1 则。
    gsize = int((cfg or {}).get("group_size") or GROUP_SIZE_DEFAULT)
    cost = len(chunks) * (gsize if fallback else 1)
    q = L.quota(url=url, key=key)
    if q.get("ok"):
        left = q["remaining"]
        tag = "[群组×%d]" % gsize if fallback else "[私聊×1]"
        if dry:
            print("【额度】剩 %d/%d 则；本次预计消耗 %d 则%s"
                  % (left, q["total"], cost, tag))
        if not force and left - cost < QUOTA_FLOOR:
            sys.stderr.write(
                "拒绝发送：LINE 剩额只有 %d 则，本次要花 %d 则%s，"
                "发完将低于保命线 %d。\n"
                "（要硬发请加 --force；任务类通知建议改走 tele，那完全免费。）\n"
                % (left, cost, tag, QUOTA_FLOOR))
            return 4
    elif not force:
        sys.stderr.write("查不到额度（%s）：为免烧穿，暂不发送。要硬发请加 --force。\n"
                         % q.get("err"))
        return 4

    if dry:
        print("【预览，未发送】目标 %s=%s，共 %d 段："
              % ("群组" if fallback else "userId", uid, len(chunks)))
        for n, c in enumerate(chunks, 1):
            print("--- (%d/%d) ---\n%s" % (n, len(chunks), c))
        return 0

    # 【硬规则】默认不发 LINE（2026-10-13 Jack：只剩回复、先不主动、保持畅通）。
    #   --dry-run 上面已放行（只预览、不真发，不花钱）；这里拦的是真发送。
    if ACTIVE_PUSH_DISABLED and not force:
        sys.stderr.write(
            "未发送：LINE 已设为「不主动推」（2026-10-13 Jack 指示）。\n"
            "  · 任务／测试／报告类通知请改走 tele（免费无限）：\n"
            "      python3 %s \"内容\"\n"
            "  · 确需主动发 LINE 才加 --force（会扣额度：私聊 1 则 / 群 %d 则/段）。\n"
            % (TG_SEND, gsize))
        return 5

    total_ms = 0
    for n, c in enumerate(chunks, 1):
        if len(chunks) > 1:
            c = "%s\n(%d/%d)" % (c, n, len(chunks))
        t0 = time.time()
        payload = {"target": uid, "userId": uid, "text": c[:4900]}
        if fallback:
            payload["fallbackUserId"] = fallback
        st, body = L.http_json(url + "/reply", headers={"x-yao-key": key},
                               timeout=20, method="POST",
                               body=payload)
        ms = int((time.time() - t0) * 1000)
        total_ms += ms
        ok = st == 200 and isinstance(body, dict) and body.get("ok")
        if not ok:
            sys.stderr.write("第 %d/%d 段发送失败：HTTP %s %s\n" % (n, len(chunks), st, str(body)[:200]))
            return 1

    print("已送达 LINE（HTTP 200/%dms，共 %d 段，目标=%s）"
          % (total_ms, len(chunks), "群组" if fallback else uid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
