#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查 LINE 官方帳號推播額度（免費方案 200 則/月），並可低額告警。

【2026-10-13 實測確認的計費規則】——這是本通道最重要的省錢常識：
  LINE 按「收件人數」計費，不是按 API 呼叫次數。
    · 回覆（reply token）        → 不計費、無限。所有「回應」都該走這條。
    · 主動 push 給私聊（1 人）    → 1 則。
    · 主動 push 給群組（N 人）    → N 則。群裡 5 人 = 每次 5 則（實測 110→115）。

  所以「盡量免費」的做法只有一句：能回覆就回覆（免費），少主動推群。

用法：
  python3 line_quota.py              # 人看得懂的摘要
  python3 line_quota.py --json       # 機器可讀
  python3 line_quota.py --floor 25   # 剩額 ≤25 時退出碼 3
  python3 line_quota.py --alert      # 剩額 ≤floor（預設 25）時經 tele 告警，
                                     #   每天最多一次（狀態檔去重）

退出碼：0 正常；1 查詢失敗；3 剩額偏低（僅在給了 --floor 或 --alert 時）。
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _line_common as L  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_STATE = os.path.join(SKILL_DIR, "line_quota_alert_state.json")
TG_SEND = os.path.expanduser("~/.workbuddy/skills/telegram-bridge/scripts/tg_send.py")


def fetch():
    """返回 dict：{ok,total,used,remaining,pct,err}。"""
    return L.quota()


def alert_once(remaining, total, floor):
    """剩額偏低時經 tele 告警（免費），每天最多一次。返回 (sent, why)。"""
    today = time.strftime("%Y-%m-%d")
    state = {}
    try:
        with open(ALERT_STATE, "r", encoding="utf-8") as fh:
            state = json.load(fh) or {}
    except (OSError, ValueError):
        state = {}
    if state.get("last_date") == today:
        return False, "今天已告警过（去重）"

    msg = ("【小姚 · 通道提醒】\n"
           "LINE 主動推播額度只剩 %d/%d 則（低於警示線 %d）。\n"
           "額度一旦用完，當月所有「主動推」會靜默失敗。\n"
           "接下來我會：① 能回覆就回覆（免費）；② 技術通知改走這裡（tele）。\n"
           "要補的話只有升級方案一途（免費方案不能加購），你可以先不管。"
           % (remaining, total, floor))
    if not os.path.isfile(TG_SEND):
        return False, "找不到 tg_send.py"
    try:
        p = subprocess.run(["/usr/bin/python3", TG_SEND, msg],
                           capture_output=True, text=True, timeout=40)
        if p.returncode != 0:
            return False, "tele 发送失败：%s" % (p.stderr or p.stdout or "").strip()[:120]
    except Exception as e:
        return False, "tele 发送异常：%s" % e

    try:
        with open(ALERT_STATE, "w", encoding="utf-8") as fh:
            json.dump({"last_date": today, "remaining": remaining,
                       "at": int(time.time())}, fh, ensure_ascii=False)
    except OSError:
        pass
    return True, "已告警"


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    do_alert = "--alert" in args
    floor = 25
    if "--floor" in args:
        i = args.index("--floor")
        try:
            floor = int(args[i + 1])
        except Exception:
            pass

    q = fetch()
    if not q.get("ok"):
        print("查額度失敗：%s" % q.get("err"))
        return 1

    total, used, left = q["total"], q["used"], q["remaining"]
    pct = q["pct"]
    if as_json:
        print(json.dumps({"total": total, "used": used, "remaining": left,
                          "pct": pct}, ensure_ascii=False))
    else:
        print("LINE 推播額度：%d / %d 已用（剩 %d 則，%.0f%%）" % (used, total, left, pct))
        print("提醒：群組主動推播 1 次 = 群組人數（本群 5 人 = 5 則）；回覆免費。")

    low = left <= floor
    if low:
        if do_alert:
            sent, why = alert_once(left, total, floor)
            print("低額告警：%s" % why)
        else:
            print("⚠️ 剩額偏低（≤%d），主動推請節制" % floor)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
