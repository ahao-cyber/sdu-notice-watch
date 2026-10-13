#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
山大通知监控 —— 每日抓取 / 去重 / 分级 / 生成日报

用法：
  python watch.py --check      # 体检：每个源是否可达、能否解析出条目
  python watch.py --baseline   # 建基线：把当前所有条目记为已见，不生成日报
  python watch.py              # 正常跑：抓取 → 去重 → 生成 daily/YYYY-MM-DD.md
  python watch.py --send       # 正常跑并发送邮件（需 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_TO）
  python watch.py --only-restricted   # 只抓被 IP 权限限制的源（本机在校内网时用）

退出码：0 正常；2 有「非受限源」抓取或解析失败（便于云端把红色跑出来）
"""
import argparse
import gzip
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urljoin

# --- 编码兜底：Windows 任务计划里 stdout 会被按 GBK 打开，打印 ✓/⚠ 会抛
# UnicodeEncodeError 把整轮抓取打死（2026-10-12 22:30 那次就是这么挂的）---
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                           # noqa: BLE001
        pass

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "sources.json"
STATE = BASE / "state.json"
DAILY = BASE / "daily"


def state_path(name: str) -> Path:
    """云端用 state.json；本机补充任务用 state-local.json，两者互不覆盖"""
    p = Path(name)
    return p if p.is_absolute() else BASE / p

# ---------------------------------------------------------------- 基础工具


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    for a, b in (("&amp;", "&"), ("&nbsp;", " "), ("&ldquo;", "“"),
                 ("&rdquo;", "”"), ("&lsquo;", "‘"), ("&rsquo;", "’"),
                 ("&lt;", "<"), ("&gt;", ">"), ("&#34;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def now_cn() -> datetime:
    """统一按北京时间算日期 —— 云端（GitHub）跑在 UTC，不修会把日报日期写错一天"""
    return datetime.now(timezone(timedelta(hours=8)))


def today() -> date:
    return now_cn().date()


def d2s(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def norm_date(raw: str, assume_year: int) -> str | None:
    """把各种写法归一成 YYYY-MM-DD"""
    raw = (raw or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return raw
    m = re.match(r"^(\d{2})\.(\d{2})\s*(\d{4})$", raw)          # 10.08 2026
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4})\.(\d{2})$", raw)                     # 2026.10
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = re.match(r"^(\d{2})-(\d{2})$", raw)                      # 10-09
    if m:
        return f"{assume_year}-{m.group(1)}-{m.group(2)}"
    return None


def norm_date_parts(day: str, year: str, month: str, assume_year: int) -> str | None:
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except Exception:
        return None


def fix_future(d: str, assume_year: int) -> str:
    """VSB 列表常不写年份。若推出来的日期比今天还晚，说明是去年的"""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return d
    if dt > today() + timedelta(days=3):
        try:
            return dt.replace(year=assume_year - 1).strftime("%Y-%m-%d")
        except ValueError:
            return d
    return d


# ---------------------------------------------------------------- 抓取


class Fetch:
    def __init__(self, ua: str, timeout: int, retries: int):
        self.ua = ua
        self.timeout = timeout
        self.retries = retries

    def get(self, url: str):
        """返回 (html, final_url, error)。被 IP 权限挡住会体现在 final_url 里"""
        last = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": self.ua,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    txt = raw.decode("utf-8", errors="replace")
                    if "锟" in txt[:3000]:                      # GBK 兜底
                        txt = raw.decode("gb18030", errors="replace")
                    return txt, r.geturl(), None
            except Exception as e:                              # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                if attempt < self.retries:
                    time.sleep(2 + attempt * 2)
        return "", url, last


def blocked_reason(html_text: str, final_url: str) -> str | None:
    if "ipauth.htm" in final_url or "/auth/" in final_url:
        return "校内网 IP 权限（ipauth）"
    head = html_text[:4000]
    if "您没有访问当前栏目的权限" in head or "没有访问当前栏目" in head:
        return "校内网 IP 权限"
    return None


# ---------------------------------------------------------------- 各源解析器
# 统一返回 [(YYYY-MM-DD, 标题, 链接), ...]


def p_bkjx_index(h, base, y):
    out = []
    pat = (r'<a href="([^"]+)"[^>]*title=""[^>]*>\s*<div class="notice_t"><i>(.*?)</i></div>'
           r'.*?<div class="time">\s*<span>(\d{2})</span>\s*<i>(\d{4})\.(\d{2})</i>'
           r'.*?<div class="nr">(.*?)</div>')
    for m in re.finditer(pat, h, re.S):
        href, tag, dd, yy, mm, t = m.groups()
        out.append((f"{yy}-{mm}-{dd}", clean(t), urljoin(base, href), clean(tag)))
    return out


def p_bkjx_list(h, base, y):
    out = []
    pat = (r'<a[^>]*href="([^"]+)"[^>]*>\s*<div class="time">\s*<span>(\d{2})\.(\d{2})</span>'
           r'\s*<i>(\d{4})</i>.*?<h5>(.*?)</h5>')
    for m in re.finditer(pat, h, re.S):
        href, mm, dd, yy, t = m.groups()
        out.append((f"{yy}-{mm}-{dd}", clean(t), urljoin(base, href), ""))
    return out


def p_cs_title_dates(h, base, y):
    out = []
    pat = (r'<a[^>]*href="([^"]+)"[^>]*title="([^"]{6,})"[^>]*>.*?</a>\s*'
           r'<span class="dates">(\d{4}-\d{2}-\d{2})</span>')
    for m in re.finditer(pat, h, re.S):
        href, t, d = m.groups()
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_vsb_li_title_date(h, base, y):
    out = []
    pat = (r'<li[^>]*>\s*<a href="([^"]+)"[^>]*title="([^"]{6,})"[^>]*>.*?</a>\s*'
           r'<span[^>]*>(\d{4}-\d{2}-\d{2})</span>')
    for m in re.finditer(pat, h, re.S):
        href, t, d = m.groups()
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_vsb_li_date_title(h, base, y):
    """VSB 列表：<li><span class="fr">2026-09-29</span><a href title>（日期在标题前）"""
    out = []
    pat = (r'<li[^>]*>\s*<span[^>]*class="fr"[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</span>\s*'
           r'<a href="([^"]+)"[^>]*title="([^"]{6,})"')
    for m in re.finditer(pat, h, re.S):
        d, href, t = m.groups()
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_news_h3_em(h, base, y):
    """新闻网列表：<a href><h3>标题</h3><em>2025-01-22</em></a>"""
    out = []
    pat = (r'<a[^>]*href="([^"]+)"[^>]*>\s*<h3>\s*(.*?)\s*</h3>\s*'
           r'<em>\s*(\d{4}-\d{2}-\d{2})\s*</em>')
    for m in re.finditer(pat, h, re.S):
        href, t, d = m.groups()
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_qdxq(h, base, y):
    out = []
    pat = (r'<a[^>]*href="([^"]+)"[^>]*>\s*<div class="time">\s*<span>(\d{1,2})</span>'
           r'\s*<span>(\d{1,2})月</span>\s*</div>\s*<div class="section">\s*'
           r'<p class="title">(.*?)</p>')
    for m in re.finditer(pat, h, re.S):
        href, dd, mm, t = m.groups()
        d = fix_future(f"{y}-{int(mm):02d}-{int(dd):02d}", y)
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_online_home(h, base, y):
    out = []
    pat = (r'<a[^>]*href="([^"]+)"[^>]*>\s*<div class="block-notice date">\s*(\d{2}-\d{2})\s*</div>\s*'
           r'<div class="block-notice title">(.*?)</div>')
    for m in re.finditer(pat, h, re.S):
        href, md, t = m.groups()
        out.append((f"{y}-{md}", clean(t), urljoin(base, href), ""))
    return out


def p_date_then_title(h, base, y):
    out = []
    pat = r'<li[^>]*>\s*<span>(\d{4}-\d{2}-\d{2})</span>\s*<a href="([^"]+)"[^>]*title="([^"]{6,})"'
    for m in re.finditer(pat, h, re.S):
        d, href, t = m.groups()
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_lib_article(h, base, y):
    out = []
    pat = r'<a href="([^"]+)"[^>]*title="([^"]{6,})"[^>]*>.*?<span class="text">\s*(\d{4}-\d{2}-\d{2})'
    for m in re.finditer(pat, h, re.S):
        href, t, d = m.groups()
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_news_p_span(h, base, y):
    out = []
    pat = r'<a[^>]*href="([^"]+)"[^>]*>\s*<p>(.*?)</p>\s*<span>(\d{4}-\d{2}-\d{2})</span>'
    for m in re.finditer(pat, h, re.S):
        href, t, d = m.groups()
        out.append((d, clean(t), urljoin(base, href), ""))
    return out


def p_youth_list(h, base, y):
    out = []
    pat = (r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*<div class="date">\s*<p>(\d{1,2})</p>\s*'
           r'<span>(\d{4})-(\d{2})</span>')
    for m in re.finditer(pat, h, re.S):
        href, t, dd, yy, mm = m.groups()
        t = clean(t)
        if len(t) < 8:
            continue
        out.append((f"{yy}-{mm}-{int(dd):02d}", t, urljoin(base, href), ""))
    return out


def p_onlineqd_home(h, base, y):
    out = []
    pat = (r'<div class="xgtz_sj"><p>(\d{2})</p><i>(\d{4})-(\d{2})</i></div>\s*'
           r'<a href="([^"]+)"[^>]*title="([^"]*)"')
    for m in re.finditer(pat, h, re.S):
        dd, yy, mm, href, t = m.groups()
        t = clean(t)
        if len(t) < 6:
            continue
        out.append((f"{yy}-{mm}-{dd}", t, urljoin(base, href), ""))
    return out


def p_ygb(h, base, y):
    """研究生之家：只认站内正文页（info/ 或 content），丢掉公众号转载噪音"""
    out = []
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', h, re.S):
        href, txt = m.groups()
        if "mp.weixin.qq.com" in href:
            continue
        if not re.search(r"(info/\d+/\d+\.htm|content\.jsp|nr_content)", href):
            continue
        t = clean(txt)
        if len(t) < 8:
            continue
        tail = h[m.end():m.end() + 400]
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", tail)
        if dm:
            out.append((dm.group(1), t, urljoin(base, href), ""))
    return out


PARSERS = {
    "bkjx_index": p_bkjx_index,
    "bkjx_list": p_bkjx_list,
    "cs_title_dates": p_cs_title_dates,
    "vsb_li_title_date": p_vsb_li_title_date,
    "vsb_li_date_title": p_vsb_li_date_title,
    "news_h3_em": p_news_h3_em,
    "qdxq": p_qdxq,
    "online_home": p_online_home,
    "date_then_title": p_date_then_title,
    "lib_article": p_lib_article,
    "news_p_span": p_news_p_span,
    "youth_list": p_youth_list,
    "onlineqd_home": p_onlineqd_home,
    "ygb": p_ygb,
}


# ---------------------------------------------------------------- 分级


def classify(title: str, src_priority: str, rules: dict) -> tuple[str, list[str]]:
    hits_a = [k for k in rules.get("A", []) if k in title]
    hits_b = [k for k in rules.get("B", []) if k in title]
    if hits_a:
        return "A", hits_a[:3]
    if hits_b:
        return "B", hits_b[:3]
    return "C", []


def item_key(title: str, url: str) -> str:
    return hashlib.sha1((title + "|" + url).encode("utf-8")).hexdigest()[:16]


def already_delivered_today(suffix: str) -> bool:
    """当天日报里已经有「## 投递」节 = 今天已经发过了。兜底定时器靠这个避免一天发多封。"""
    p = DAILY / f"{d2s(today())}{suffix}.md"
    if not p.exists():
        return False
    try:
        return "## 投递" in p.read_text(encoding="utf-8")
    except Exception:                                           # noqa: BLE001
        return False


# ---------------------------------------------------------------- 主流程


def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def run(args) -> int:
    if getattr(args, "skip_if_delivered", False) and already_delivered_today(args.suffix):
        print(f"[跳过] daily/{d2s(today())}{args.suffix}.md 今天已投递过，不再重复发送")
        return 0

    cfg = load_json(CONFIG, {})
    state_file = state_path(getattr(args, "state", "state.json"))
    st = load_json(state_file, {"seen": {}, "runs": []})
    settings = cfg.get("settings", {})
    fetcher = Fetch(settings.get("user_agent", "Mozilla/5.0"), int(settings.get("timeout", 25)),
                    int(settings.get("retries", 2)))
    rules = cfg.get("tag_rules", {})
    window = int(settings.get("window_days", 30))
    y = today().year
    cutoff = today() - timedelta(days=window)

    sources = cfg.get("sources", [])
    if args.only_restricted:
        sources = [s for s in sources if s.get("restricted")]
    elif args.only_cloud:
        sources = [s for s in sources if not s.get("restricted")]

    results, problems, restricted_hit = {}, [], []
    for s in sources:
        sid, name = s["id"], s["name"]
        parser = PARSERS.get(s.get("parser", ""))
        html_text, final_url, err = fetcher.get(s["url"])
        if err and not html_text:
            results[sid] = dict(name=name, status="fetch_fail", error=err, items=[])
            if not s.get("restricted"):
                problems.append(f"{name} 抓取失败：{err}")
            print(f"  ✗ {name}: 抓取失败 {err}")
            continue
        blk = blocked_reason(html_text, final_url)
        if blk:
            results[sid] = dict(name=name, status="blocked", error=blk, items=[])
            restricted_hit.append(name)
            print(f"  ⚠ {name}: {blk}（校外/云端抓不到，属预期）")
            continue
        if parser is None:
            results[sid] = dict(name=name, status="no_parser", items=[])
            problems.append(f"{name} 没配解析器")
            continue
        try:
            raw_items = parser(html_text, s["url"], y)
        except Exception as e:                                  # noqa: BLE001
            results[sid] = dict(name=name, status="parse_error", error=str(e), items=[])
            problems.append(f"{name} 解析异常：{e}")
            print(f"  ✗ {name}: 解析异常 {e}")
            continue
        seen_urls, items = set(), []
        for row in raw_items:
            d, t, u = row[0], row[1], row[2]
            tag = row[3] if len(row) > 3 else ""
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d or ""):
                continue
            if (t, u) in seen_urls or len(t) < 6:
                continue
            seen_urls.add((t, u))
            items.append(dict(date=d, title=t, url=u, tag=tag))
        # 条目质量过滤：去掉导航文字混入、超长标题、列表页自身链接
        nav_words = ("团委简介", "办公文件", "青字号文", "劳动教育简报", "青年大学习简报",
                     "拓展培养管理系统", "学习强国", "返回首页", "旧版回顾", "网站首页",
                     "机构设置", "部门简介")
        good = []
        for it in items:
            t = it["title"]
            if len(t) > 70 or any(w in t for w in nav_words):
                continue
            if re.search(r"/(index|default)\.(jsp|htm|html)$", it["url"]):
                continue
            if re.search(r"/(gsl|xyhd|zytz|bkjy|tzgg|xygg)\.htm$", it["url"]):
                continue
            good.append(it)
        items = good
        items.sort(key=lambda x: x["date"], reverse=True)
        status = "ok" if items else "empty"
        if not items and not s.get("restricted") and not s.get("allow_empty"):
            problems.append(f"{name} 解析出 0 条（可能改版）")
        results[sid] = dict(name=name, status=status, items=items,
                            latest=items[0]["date"] if items else None,
                            priority=s.get("priority", "C"), restricted=bool(s.get("restricted")))
        print(f"  {'✓' if items else '⚠'} {name}: {len(items)} 条"
              f"{'（最新 ' + items[0]['date'] + '）' if items else '（空/可能改版）'}")

    # 去重 + 分级
    fresh = []
    seen = st.setdefault("seen", {})
    for sid, r in results.items():
        for it in r["items"]:
            k = item_key(it["title"], it["url"])
            if k in seen or datetime.strptime(it["date"], "%Y-%m-%d").date() < cutoff:
                continue
            tier, why = classify(it["title"], r.get("priority", "C"), rules)
            fresh.append(dict(**it, source=r["name"], sid=sid, tier=tier, why=why,
                              priority=r.get("priority", "C")))
    # 同文多源合并
    merged = {}
    for it in fresh:
        key = re.sub(r"\s+", "", it["title"])[:40]
        if key in merged:
            other = merged[key]
            other["also"] = list(dict.fromkeys(other.get("also", []) + [it["source"]]))
            continue
        merged[key] = it
    fresh = sorted(merged.values(), key=lambda x: (x["tier"], x["date"]), reverse=False)
    fresh.sort(key=lambda x: x["date"], reverse=True)
    fresh.sort(key=lambda x: {"A": 0, "B": 1, "C": 2}[x["tier"]])

    if args.baseline:
        n = 0
        for sid, r in results.items():
            for it in r["items"]:
                k = item_key(it["title"], it["url"])
                if k not in seen:
                    seen[k] = dict(title=it["title"], date=it["date"], source=r["name"],
                                   first_seen=d2s(today()))
                    n += 1
        st["runs"].append(dict(ts=now_cn().isoformat(timespec="seconds"), mode="baseline",
                               sources=len(results), recorded=n, problems=problems))
        state_file.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n[基线] 记下 {n} 条已见条目，{state_file.name} 已更新。不生成日报。")
        return 0

    digest = render_digest(results, fresh, problems, restricted_hit)
    DAILY.mkdir(exist_ok=True)
    out = DAILY / f"{d2s(today())}{args.suffix}.md"
    out.write_text(digest, encoding="utf-8")
    print(f"\n日报已写入 {out}（新条目 {len(fresh)} 条：必看 "
          f"{sum(1 for x in fresh if x['tier']=='A')} / 值得看 "
          f"{sum(1 for x in fresh if x['tier']=='B')} / 其他 "
          f"{sum(1 for x in fresh if x['tier']=='C')}）")

    for it in fresh:
        k = item_key(it["title"], it["url"])
        seen.setdefault(k, dict(title=it["title"], date=it["date"], source=it["source"],
                                first_seen=d2s(today())))
    st["runs"] = (st.get("runs", []) + [dict(ts=now_cn().isoformat(timespec="seconds"),
                                             mode="run", sources=len(results),
                                             fresh=len(fresh), problems=problems)])[-60:]
    state_file.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.send:
        n_a = sum(1 for x in fresh if x["tier"] == "A")
        title = (f"【山大通知】{d2s(today())} 共 {len(fresh)} 条"
                 + (f"（必看 {n_a}）" if n_a else ""))
        if args.skip_empty_send and not fresh:
            print("[投递] 本次没有新条目，按 --skip-empty-send 跳过发送")
        else:
            if args.issue:
                create_issue(title, digest)
            push_serverchan(title, digest)
            send_mail(digest, fresh, problems, restricted_hit)
        # 把投递结果补写进当日日报文件（日报一定会被提交，便于事后核对）
        if DELIVERY_NOTES:
            with open(out, "a", encoding="utf-8") as f:
                f.write("\n## 投递\n\n")
                for n in DELIVERY_NOTES:
                    f.write(f"- {n}\n")

    if problems:
        print("\n⚠ 问题：")
        for p in problems:
            print("   -", p)
        return 2
    return 0


def render_digest(results, fresh, problems, restricted_hit) -> str:
    t = today()
    L = [f"# 山大通知日报 · {d2s(t)}", ""]
    a = [x for x in fresh if x["tier"] == "A"]
    b = [x for x in fresh if x["tier"] == "B"]
    c = [x for x in fresh if x["tier"] == "C"]
    if not fresh:
        L += ["今天没有新的通知。", ""]
    for label, arr in (("★ 必看", a), ("· 值得看", b), ("· 其他", c)):
        if not arr:
            continue
        L.append(f"## {label}（{len(arr)}）")
        L.append("")
        for x in arr:
            extra = f"（命中：{'、'.join(x['why'])}）" if x.get("why") else ""
            also = f"｜另见：{'、'.join(x['also'])}" if x.get("also") else ""
            L.append(f"- **{x['date']}** [{x['title']}]({x['url']})")
            L.append(f"  - 来源：{x['source']}{extra}{also}")
        L.append("")
    L += ["## 覆盖情况", ""]
    ok = [r for r in results.values() if r["status"] == "ok"]
    empt = [r for r in results.values() if r["status"] == "empty"]
    blk = [r for r in results.values() if r["status"] == "blocked"]
    bad = [r for r in results.values() if r["status"] in ("fetch_fail", "parse_error", "no_parser")]
    L.append(f"- 正常：{len(ok)} 个源；无新条目：{len(empt)}")
    if blk:
        L.append(f"- 校内权限（本机补抓）：{'、'.join(r['name'] for r in blk)}")
    if bad:
        L.append(f"- **异常（要看）**：{'、'.join(r['name'] for r in bad)}")
    if problems:
        L.append("- 需注意：" + "；".join(problems))
    L.append("")
    L.append(f"_生成时间：{now_cn().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(L)


def md_to_html(md: str) -> str:
    out, in_ul = [], False
    for line in md.splitlines():
        if line.startswith("# "):
            out.append(f"<h2 style='margin:16px 0 8px'>{html.escape(line[2:])}</h2>")
        elif line.startswith("## "):
            out.append(f"<h3 style='margin:14px 0 6px;color:#333'>{html.escape(line[3:])}</h3>")
        elif line.strip().startswith("- "):
            if not in_ul:
                out.append("<ul style='padding-left:20px;margin:4px 0'>")
                in_ul = True
            body = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<a href='\2'>\1</a>", line.strip()[2:])
            out.append(f"<li style='margin:4px 0'>{body}</li>")
        elif line.strip().startswith("  - "):
            out.append(f"<li style='list-style:none;color:#777;font-size:13px'>"
                       f"{html.escape(line.strip()[2:])}</li>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if line.strip():
                out.append(f"<p style='margin:6px 0'>{html.escape(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return ("<div style=\"font-family:-apple-system,'Microsoft YaHei',sans-serif;"
            "font-size:15px;line-height:1.6;max-width:680px\">" + "".join(out) + "</div>")


def push_serverchan(title: str, markdown: str) -> None:
    """Server酱 Turbo：微信推送。key 从环境变量 SERVERCHAN_KEY 或 mail.local.json 的 serverchan_key 读"""
    cfg = load_json(BASE / "mail.local.json", {})
    key = os.environ.get("SERVERCHAN_KEY") or cfg.get("serverchan_key")
    if not key:
        return
    try:
        data = urllib.parse.urlencode({"title": title, "desp": markdown}).encode()
        req = urllib.request.Request(f"https://sctapi.ftqq.com/{key}.send", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", errors="replace")
        ok = '"code":0' in body or '"code": 0' in body
        print(f"[微信推送] {'已发送' if ok else '返回异常：' + body[:120]}")
        log_delivery("wechat", ok, "serverchan" if ok else body[:80])
    except Exception as e:                                      # noqa: BLE001
        print(f"[微信推送] 失败：{type(e).__name__}: {e}")
        log_delivery("wechat", False, f"{type(e).__name__}: {e}")


DELIVERY_NOTES: list[str] = []


def log_delivery(channel: str, ok: bool, detail: str = "") -> None:
    """投递回执：既写 delivery.log，也收集起来写进当日日报，便于事后核对"""
    try:
        line = (f"{now_cn().strftime('%Y-%m-%d %H:%M')} | {channel:8s} | "
                f"{'OK' if ok else 'FAIL'} | {detail}\n")
        with open(BASE / "delivery.log", "a", encoding="utf-8") as f:
            f.write(line)
        print(f"[回执] {line.strip()}")
    except Exception:                                           # noqa: BLE001
        pass
    DELIVERY_NOTES.append(f"{channel}：{'成功' if ok else '未成功'} {detail}".strip())


def create_issue(title: str, body_md: str) -> None:
    """把日报发成 GitHub Issue，并在正文里 @ 仓库所有者 —— GitHub 会给本人发邮件提醒。
    这样送达不需要任何邮箱授权码。仅在 GitHub Actions 里可用（靠 GITHUB_TOKEN / GITHUB_REPOSITORY）"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        log_delivery("github", False, "缺少 GITHUB_TOKEN / GITHUB_REPOSITORY（工作流须显式传入）")
        print("[GitHub] 缺少 GITHUB_TOKEN，跳过创建通知")
        return
    owner = repo.split("/")[0]
    payload = {"title": title, "body": f"@{owner} 今日日报：\n\n{body_md}"}
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "sdu-notice-watch",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
        print(f"[GitHub] 已创建通知 #{resp.get('number')}：{resp.get('html_url')}")
        log_delivery("github", True, f"#{resp.get('number')} {title}")
    except Exception as e:                                      # noqa: BLE001
        print(f"[GitHub] 创建通知失败：{type(e).__name__}: {e}")
        log_delivery("github", False, f"{type(e).__name__}: {e}")


def load_local_mail() -> dict:
    """邮件凭据：优先环境变量，其次同目录 mail.local.json（该文件不进仓库）"""
    path = BASE / "mail.local.json"
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:                                  # noqa: BLE001
            print(f"[邮件] mail.local.json 格式有问题，读不了：{type(e).__name__}: {e}")
            print("     常见原因：漏了逗号、引号不成对。可以贴到 jsonlint 之类的校验网站看一眼。")
            cfg = {}
    return {
        "host": os.environ.get("SMTP_HOST") or cfg.get("host", "smtp.qq.com"),
        "port": int(os.environ.get("SMTP_PORT") or cfg.get("port", 465)),
        "user": os.environ.get("SMTP_USER") or cfg.get("user"),
        "pass": os.environ.get("SMTP_PASS") or cfg.get("pass"),
        "to": os.environ.get("MAIL_TO") or cfg.get("to"),
    }


def send_mail(digest: str, fresh: list, problems: list, restricted_hit: list) -> None:
    c = load_local_mail()
    host, port, user, pwd, to = c["host"], c["port"], c["user"], c["pass"], c["to"] or c["user"]
    if not (user and pwd):
        print("[邮件] 未配置 SMTP_USER / SMTP_PASS（或 mail.local.json），跳过发送")
        log_delivery("mail", False, "未配置 SMTP_USER/SMTP_PASS，跳过")
        return
    n_a = sum(1 for x in fresh if x["tier"] == "A")
    subject = f"【山大通知】{d2s(today())} 共 {len(fresh)} 条" + (f"（必看 {n_a}）" if n_a else "")
    msg = MIMEText(md_to_html(digest), "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = f"{Header('山大通知监控', 'utf-8').encode()} <{user}>"
    msg["To"] = to
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
        print("[邮件] 已发送（收件地址不写进日志）")
        log_delivery("mail", True, "已发送")
    except Exception as e:                                      # noqa: BLE001
        print(f"[邮件] 发送失败：{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="山大通知监控")
    ap.add_argument("--check", action="store_true", help="只体检各源")
    ap.add_argument("--baseline", action="store_true", help="建基线，不生成日报")
    ap.add_argument("--send", action="store_true", help="生成日报并投递（GitHub通知/微信/邮件）")
    ap.add_argument("--issue", action="store_true", help="把日报发成 GitHub Issue（云端主渠道，无需邮箱授权码）")
    ap.add_argument("--skip-empty-send", action="store_true", help="没有新条目时就不发（本机补充任务用）")
    ap.add_argument("--skip-if-delivered", action="store_true",
                    help="今天已投递过就整体跳过（兜底定时器用，避免一天发多封）")
    ap.add_argument("--only-restricted", action="store_true", help="只抓校内权限源（本机用）")
    ap.add_argument("--only-cloud", action="store_true", help="只抓云端可达源")
    ap.add_argument("--local", action="store_true", help="本机模式：只抓校内权限源 + 日报写 -local 后缀")
    ap.add_argument("--suffix", default="", help="日报文件名后缀，如 -local")
    ap.add_argument("--state", default="state.json", help="状态文件名（本机补充任务用 state-local.json）")
    args = ap.parse_args()
    if args.local:
        args.only_restricted, args.suffix = True, "-local"
    if args.only_restricted and not args.suffix:
        args.suffix = "-local"          # 避免覆盖云端生成的那份日报
    if args.check:
        args.baseline = False
    print(f"== 山大通知监控 {now_cn().strftime('%Y-%m-%d %H:%M')} ==")
    rc = run(args)
    print(f"exit={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
