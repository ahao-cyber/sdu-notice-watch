#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端失约检测 —— 别让「今天到底发没发」等到明天才知道。

GitHub 的 schedule 触发器会延迟、甚至整天不触发（新仓库尤其容易）。
本机这个脚本每天跑一次，只做一件事：看云端当天的日报在不在、投递成没成功。
 - 云端已送达   → 什么都不做（安静）
 - 云端失约     → 本机自己用公网源补一份日报并邮件发出去
 - 读不到云端   → 网络/代理不通，**不能**当成失约（否则会把云端已经发过的
                  通知再发一遍 = 用户收到重复邮件）。这种情况只记日志，不补发。

判据必须可靠：只有 `git fetch` 真的成功、看到了 origin/main 的最新状态，
才允许下「云端失约」的结论。Clash 没开时 ~/.gitconfig 里写死的
http.proxy=127.0.0.1:7897 会让 fetch 直接失败，所以这里带一次
「禁用代理」的重试（校园网直连 GitHub 是通的）。

用法：
  python cloud-audit.py                     # 检测今天；云端失约则本机顶上
  python cloud-audit.py --dry-run           # 只看结论，不抓取不发送
  python cloud-audit.py --date 2026-01-01   # 检测指定日期（测试/补查用）
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_LOCAL = BASE / "state-local.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                           # noqa: BLE001
        pass


def today_cn() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _git(*args, no_proxy: bool = False, timeout: int = 180):
    """跑一条 git 命令。no_proxy=True 时临时抹掉 git 里写死的 HTTP 代理。

    `-c http.proxy=` 这类「空值」写法是 git 官方支持的临时覆盖，
    不改用户全局配置，也不影响别的 git 操作。
    """
    cmd = ["git"]
    if no_proxy:
        cmd += ["-c", "http.https://github.com.proxy=",
                "-c", "http.proxy=", "-c", "https.proxy="]
    cmd += list(args)
    try:
        return subprocess.run(cmd, cwd=BASE, capture_output=True, timeout=timeout)
    except Exception as e:                                      # noqa: BLE001
        class _R:                                                # 极小替身
            returncode = 1
            stdout = b""
            stderr = f"{type(e).__name__}: {e}".encode("utf-8", "replace")
        return _R()


def _lastline(raw: bytes) -> str:
    lines = [x for x in raw.decode("utf-8", "replace").splitlines() if x.strip()]
    return lines[-1].strip() if lines else "（无输出）"


def fetch_origin():
    """拉一次 origin。返回 (成功?, 说明)。

    先按全局配置（有 Clash 时走代理），失败再禁用代理重试一次
    （校园网里 Clash 被守护脚本退掉、系统直连 GitHub 是通的）。
    """
    r = _git("fetch", "-q", "origin")
    if r.returncode == 0:
        return True, "正常"
    first = _lastline(r.stderr)
    r2 = _git("fetch", "-q", "origin", no_proxy=True)
    if r2.returncode == 0:
        return True, f"按配置代理失败（{first}），禁用代理重试成功"
    return False, f"代理与直连都失败：{first} / {_lastline(r2.stderr)}"


def cloud_daily(day: str):
    """取那天云端日报的正文 + 取数结论。

    返回 (status, txt)
      status = "ok"      云端已成功投递
             = "missed"  确实看到云端状态了，但当天没日报 / 没投递成功
             = "unknown" 连不上 GitHub，本轮无法判断（不许当失约处理）
    """
    ok, how = fetch_origin()
    if not ok:
        return "unknown", None, f"取不到 GitHub —— {how}"
    if how != "正常":
        print(f"[检测] git fetch 说明：{how}")

    r = _git("show", f"origin/main:daily/{day}.md")
    if r.returncode != 0:
        return "missed", None, "云端没有当天的日报文件（工作流没跑或没提交）"
    txt = r.stdout.decode("utf-8", "replace")
    if "## 投递" not in txt:
        return "missed", txt, "云端日报里没有投递记录（跑到一半就断了）"
    if ("mail：成功" in txt) or ("github：成功" in txt):
        return "ok", txt, "云端已投递"
    return "missed", txt, "云端两个通道都没投递成功"


def merge_cloud_state(target: Path = STATE_LOCAL) -> int:
    """把云端 state.json 里「已发过」的条目并进本机状态文件。

    兜底补发用的是 state-local.json，与云端的 state.json 互不相通。
    如果直接补发，云端前一晚/前几天已经发过的公网条目在本机看来全是新的
    → 邮件里会再次出现同样的通知（2026-10-16 起就是这样重发了 10 条旧通知）。
    所以补发之前先对齐一次，返回新增的条数。
    """
    r = _git("show", "origin/main:state.json")
    if r.returncode != 0:
        print("[对齐] 读不到云端 state.json，跳过（补发仍会按本机状态判断）")
        return 0
    try:
        cloud = json.loads(r.stdout.decode("utf-8", "replace"))
    except Exception as e:                                      # noqa: BLE001
        print(f"[对齐] 云端 state.json 解析失败：{type(e).__name__}，跳过")
        return 0
    cloud_seen = cloud.get("seen", {}) or {}
    local = {"seen": {}, "runs": []}
    if target.exists():
        try:
            local = json.loads(target.read_text(encoding="utf-8"))
        except Exception:                                       # noqa: BLE001
            pass
    seen = local.setdefault("seen", {})
    added = 0
    for k, v in cloud_seen.items():
        if k not in seen:
            seen[k] = v
            added += 1
    if added:
        target.write_text(json.dumps(local, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[对齐] 云端已发条目 {len(cloud_seen)} 条，补进本机状态 {added} 条（避免重发旧通知）")
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description="云端失约检测")
    ap.add_argument("--date", default=None, help="要检查的日期，默认今天")
    ap.add_argument("--dry-run", action="store_true", help="只出结论，不抓取不发送")
    a = ap.parse_args()

    day = a.date or today_cn()
    status, _txt, why = cloud_daily(day)
    print(f"[检测] {day}：{why}")

    if status == "ok":
        return 0

    if status == "unknown":
        print("[检测] 连不上 GitHub，无法判断云端是否投递 → 本轮不补发，等下次（避免重复发送）")
        return 0

    cmd = ["watch.py", "--only-cloud", "--send", "--skip-empty-send",
           "--state", "state-local.json", "--suffix=-cloudfallback"]
    print("[兜底] 云端失约，本机用 13 个公网源补发一份：" + " ".join(cmd))
    if a.dry_run:
        print("[兜底] --dry-run：不执行")
        return 0

    merge_cloud_state()

    return subprocess.run([sys.executable] + cmd, cwd=BASE, timeout=900).returncode


if __name__ == "__main__":
    sys.exit(main())
