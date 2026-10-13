#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端失约检测 —— 别让「今天到底发没发」等到明天才知道。

GitHub 的 schedule 触发器会延迟、甚至整天不触发（新仓库尤其容易）。
本机这个脚本每天跑一次，只做一件事：看云端当天的日报在不在、投递成没成功。
 - 云端已送达 → 什么都不做（安静）
 - 云端失约   → 本机自己用公网源补一份日报并邮件发出去

用法：
  python cloud-audit.py                     # 检测今天；云端失约则本机顶上
  python cloud-audit.py --dry-run           # 只看结论，不抓取不发送
  python cloud-audit.py --date 2026-01-01   # 检测指定日期（测试/补查用）
"""
import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                           # noqa: BLE001
        pass


def today_cn() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def cloud_daily(day: str):
    """从 origin/main 取那天云端的日报正文；取不到返回 None"""
    try:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=BASE, timeout=120, check=False)
        r = subprocess.run(["git", "show", f"origin/main:daily/{day}.md"],
                           cwd=BASE, capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace")
    except Exception as e:                                      # noqa: BLE001
        print(f"[检测] 读云端日报失败：{type(e).__name__}: {e}")
        return None


def judge(txt):
    if txt is None:
        return False, "云端没有当天的日报文件（工作流没跑或没提交）"
    if "## 投递" not in txt:
        return False, "云端日报里没有投递记录（跑到一半就断了）"
    if ("mail：成功" in txt) or ("github：成功" in txt):
        return True, "云端已投递"
    return False, "云端两个通道都没投递成功"


def main() -> int:
    ap = argparse.ArgumentParser(description="云端失约检测")
    ap.add_argument("--date", default=None, help="要检查的日期，默认今天")
    ap.add_argument("--dry-run", action="store_true", help="只出结论，不抓取不发送")
    a = ap.parse_args()

    day = a.date or today_cn()
    ok, why = judge(cloud_daily(day))
    print(f"[检测] {day}：{why}")

    if ok:
        return 0

    cmd = [sys.executable, "watch.py", "--only-cloud", "--send",
           "--state", "state-local.json", "--suffix=-cloudfallback"]
    print("[兜底] 云端失约，本机用 13 个公网源补发一份：" + " ".join(cmd))
    if a.dry_run:
        print("[兜底] --dry-run：不执行")
        return 0
    return subprocess.run(cmd, cwd=BASE, timeout=900).returncode


if __name__ == "__main__":
    sys.exit(main())
