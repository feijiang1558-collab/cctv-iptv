#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CCTV 精简直播源生成器
------------------------------------------------------------------
从公开 M3U 源池拉取候选源 -> 解析出 CCTV-1/2/3/4/8/13 六个频道
-> 并发实测可用性与首包延迟 -> 每个频道选最优源 -> 生成 cctv.m3u

特点：
  * 只输出 6 个央视高清频道，不含广播电台、聚合广告频道
  * 每次运行重新实测，失效源自动被可用源替换
  * 若任一频道无可用源，保留上一次结果，避免把列表改坏

用法：
  python update.py
可选：
  在同目录建 pools.txt，每行一个自定义 M3U 源池地址，会与内置源池合并。
"""

import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE_DIR, "cctv.m3u")
POOLS_FILE = os.path.join(BASE_DIR, "pools.txt")

# 目标频道：key 用于精确匹配，name 为输出显示名，logo 为台标
TARGETS = [
    {"key": "CCTV1",  "display": "CCTV-1 综合",    "num": "1"},
    {"key": "CCTV2",  "display": "CCTV-2 财经",    "num": "2"},
    {"key": "CCTV3",  "display": "CCTV-3 综艺",    "num": "3"},
    {"key": "CCTV4",  "display": "CCTV-4 中文国际", "num": "4"},
    {"key": "CCTV8",  "display": "CCTV-8 电视剧",   "num": "8"},
    {"key": "CCTV13", "display": "CCTV-13 新闻",   "num": "13"},
]

# 内置公开源池（均为社区公开维护的 M3U 列表，仅作为候选地址来源）
DEFAULT_POOLS = [
    "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
    "https://raw.githubusercontent.com/yuanzl77/IPTV/main/live.m3u8",
    "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
]

TIMEOUT = 8          # 单源实测超时（秒）
MAX_WORKERS = 16     # 并发检测线程数
TOP_N = 1            # 每频道输出源数量（保持列表精简）

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def log(msg):
    print("[%s] %s" % (datetime.now(tz=timezone(timedelta(hours=8))).strftime("%H:%M:%S"), msg), flush=True)


def http_get(url, extra_headers=None, max_bytes=None, timeout=TIMEOUT):
    """返回 (status_code, bytes_prefix, headers, elapsed_ms)。只读前 max_bytes 字节。"""
    headers = {"User-Agent": UA}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            data = resp.read(max_bytes) if max_bytes else resp.read()
            ms = int((time.time() - start) * 1000)
            return code, data, dict(resp.headers), ms
    except urllib.error.HTTPError as e:
        return e.code, b"", {}, int((time.time() - start) * 1000)
    except Exception:
        return 0, b"", {}, int((time.time() - start) * 1000)


def load_pools():
    pools = list(DEFAULT_POOLS)
    if os.path.exists(POOLS_FILE):
        with open(POOLS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line not in pools:
                    pools.append(line)
    return pools


def fetch_pool(url):
    code, data, _, _ = http_get(url, max_bytes=None, timeout=12)
    if code != 200:
        log("源池拉取失败 HTTP %s : %s" % (code, url))
        return ""
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def parse_attr(line):
    """解析 EXTINF 行的属性与显示名。"""
    attrs = {}
    display = ""
    if "," in line:
        head, display = line.split(",", 1)
    else:
        head = line
    for m in re.finditer(r'([\w\-]+)="([^"]*)"', head):
        attrs[m.group(1)] = m.group(2)
    return attrs, display.strip()


def norm(s):
    return re.sub(r"[\s\-_·]", "", s or "").upper()


def match_target(attrs, display, url):
    """
    精确判定条目属于哪个目标频道。
    用 tvg-id / tvg-name 优先，其次显示名；避免 CCTV1 误匹配 CCTV10/13。
    """
    fields = [attrs.get("tvg-id", ""), attrs.get("tvg-name", ""), display]
    for t in TARGETS:
        num = t["num"]
        # 数字后必须跟非数字，排除 CCTV1 命中 CCTV13/CCTV10 等
        pat = re.compile(r"CCTV[_\-]?0*" + num + r"(?![0-9])", re.I)
        for f in fields:
            if pat.search(norm(f)):
                # 排除明显的广播/电台/广告聚合条目
                if re.search(r"广播|电台|RADIO|广告", norm(f)):
                    return None
                return t["key"]
    return None


def parse_m3u(text, source_tag):
    entries = []
    lines = text.splitlines()
    cur = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            attrs, display = parse_attr(line)
            key = match_target(attrs, display, "")
            cur = {
                "key": key,
                "attrs": attrs,
                "display": display,
                "headers": {},
            }
            ua = attrs.get("http-user-agent") or attrs.get("user-agent")
            ref = attrs.get("http-referer") or attrs.get("referer")
            if ua:
                cur["headers"]["User-Agent"] = ua
            if ref:
                cur["headers"]["Referer"] = ref
        elif line and not line.startswith("#") and cur is not None:
            if cur["key"] and line.startswith(("http://", "https://")):
                cur["url"] = line
                cur["source"] = source_tag
                entries.append(cur)
            cur = None
    return entries


def dedup(entries):
    seen = set()
    out = []
    for e in entries:
        u = e["url"]
        if u not in seen:
            seen.add(u)
            out.append(e)
    return out


def probe(entry):
    """实测单个候选源：可访问、非错误页、有数据流；返回带分数的条目。"""
    url = entry["url"]
    code, data, headers, ms = http_get(url, entry.get("headers"), max_bytes=2048, timeout=TIMEOUT)
    ok = False
    if code == 200 and len(data) > 0:
        head = data[:512].lstrip().lower()
        # 排除错误页 / HTML 占位页
        if not (head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"<error")):
            if b"#extm3u" in data.lower():
                # HLS 索引：确认含分片或子流
                ok = bool(re.search(rb"#EXT-X-STREAM-INF|#EXTINF|\.ts|\.m3u8", data, re.I))
            else:
                ok = True
    score = 0
    if ok:
        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
        name_blob = norm(entry.get("display", "") + " " + entry.get("attrs", {}).get("tvg-name", ""))
        if re.search(r"高清|1080|HD|H265|4K", name_blob):
            score += 200
        if "video" in ctype or "mpegurl" in ctype or "octet-stream" in ctype:
            score += 50
        if url.startswith("https://"):
            score += 30
        score += max(0, 300 - min(ms, 300))
    return dict(entry, ok=ok, latency=ms, score=score)


def select(entries):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(probe, e) for e in entries]
        results = [f.result() for f in as_completed(futures)]
    chosen = {}
    for t in TARGETS:
        cands = [r for r in results if r["ok"] and r["key"] == t["key"]]
        cands.sort(key=lambda r: (-r["score"], r["latency"]))
        chosen[t["key"]] = cands[:TOP_N]
    return chosen, results


def build_m3u(chosen):
    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        '#EXTM3U x-tvg-url="https://epg.aptv.app/pp.xml.gz"',
        "#EXT-X-VERSION:3",
        "# @generated %s (Asia/Shanghai)" % now,
        "# @channels CCTV-1 CCTV-2 CCTV-3 CCTV-4 CCTV-8 CCTV-13",
        "",
    ]
    missing = []
    for t in TARGETS:
        cands = chosen.get(t["key"], [])
        if not cands:
            missing.append(t["display"])
            continue
        for e in cands:
            extra = ""
            if "User-Agent" in e.get("headers", {}):
                extra += ' http-user-agent="%s"' % e["headers"]["User-Agent"]
            if "Referer" in e.get("headers", {}):
                extra += ' http-referer="%s"' % e["headers"]["Referer"]
            lines.append(
                '#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="https://gitee.com/mytv-android/myTVlogo/raw/main/img/%s.png" group-title="央视高清"%s,%s'
                % (t["key"], t["key"], t["key"], extra, t["display"])
            )
            lines.append(e["url"])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n", missing


def main():
    log("开始采集央视 6 频道直播源")
    pools = load_pools()
    log("源池数量：%d" % len(pools))

    texts = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_pool, p): p for p in pools}
        for f in as_completed(futs):
            t = f.result()
            if t:
                texts.append(t)

    entries = []
    for idx, text in enumerate(texts):
        entries.extend(parse_m3u(text, "pool#%d" % idx))
    entries = dedup(entries)
    log("解析到目标频道候选源：%d 条" % len(entries))

    if not entries:
        log("错误：未获取到任何候选源，保留现有 cctv.m3u")
        return 2

    by_channel = {}
    for e in entries:
        by_channel.setdefault(e["key"], []).append(e)
    for t in TARGETS:
        log("  %s 候选 %d 条" % (t["display"], len(by_channel.get(t["key"], []))))

    chosen, results = select(entries)
    ok_count = sum(1 for r in results if r["ok"])
    log("实测可用源：%d / %d" % (ok_count, len(entries)))

    content, missing = build_m3u(chosen)
    if missing:
        log("警告：以下频道无可用源 -> %s" % "、".join(missing))
        if os.path.exists(OUT_FILE):
            log("为保证可用性，保留上一次 cctv.m3u 不做覆盖")
            return 3
        log("首次运行且无历史文件，仍写出（缺失频道将在列表中缺省）")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    log("已写出 %s" % OUT_FILE)

    for t in TARGETS:
        for e in chosen.get(t["key"], []):
            log("选中 %-12s %4dms  %s" % (t["key"], e["latency"], e["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
