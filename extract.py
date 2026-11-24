# -*- coding: utf-8 -*-
"""
从 APTV 官方公开池提取：全部 CCTV + 10 个热门省级卫视
每个频道固定 3 条线路（不做测速，顺序即优先级）：
  第1条：官方国内源 101.35.240.114:88（用户国内网络最快最稳，固定在首位）
  第2/3条：从 Guovin 源池自动取两条海外源作备用（可能有广告，仅备用）
输出：cctv.m3u
"""
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "cctv.m3u")

# APTV 官方池
OFFICIAL_SOURCES = [
    "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
    "https://ghproxy.net/https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
    "https://gh-proxy.com/https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
]
# Guovin 源池（备用线路来源）
BACKUP_SOURCES = [
    "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://ghproxy.net/https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://gh-proxy.com/https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
]

CCTV_RE = re.compile(r"^CCTV(\d{1,2}|5\+|4K)$", re.I)
# 10 个热门省级卫视（按全国知名度，用户 2026-11-24 确认）
HOT_TV = ["湖南卫视", "浙江卫视", "江苏卫视", "东方卫视", "北京卫视",
          "山东卫视", "广东卫视", "深圳卫视", "安徽卫视", "河南卫视"]
# Guovin 池频道名 -> 本列表名
TV_NAME_MAP = {"CCTV-5+": "CCTV5+"}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def fetch(urls):
    errs = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read().decode("utf-8", "replace")
            if "#EXTM3U" in data and "#EXTINF" in data:
                return data
            errs.append(url + " -> 内容非 M3U")
        except Exception as e:
            errs.append(url + " -> " + str(e)[:100])
    raise RuntimeError("源池拉取失败: " + " | ".join(errs))


def parse(text):
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.strip().startswith("#EXTINF"):
            for j in range(i + 1, min(i + 4, len(lines))):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    if nxt.startswith("http://") or nxt.startswith("https://"):
                        out.append((line.strip(), nxt))
                    break
    return out


def channel_name(extinf):
    return extinf.split(",", 1)[-1].strip() if "," in extinf else ""


def norm(name):
    """归一化为官方池频道名（CCTV-1 -> CCTV1）。"""
    name = TV_NAME_MAP.get(name, name)
    m = re.match(r"CCTV-(\d{1,2}|\d\+|4K)", name, re.I)
    if m:
        return "CCTV" + m.group(1).upper()
    return name


def sort_key(name):
    m = re.match(r"CCTV(\d{1,2}|5\+|4K)", name, re.I)
    if m:
        token = m.group(1).upper()
        if token == "4K":
            return (0, 999)
        if token == "5+":
            return (0, 5)
        return (0, int(token))
    return (1, HOT_TV.index(name) if name in HOT_TV else 99)


def build(official, backup):
    # 官方源：每个目标频道第一条
    targets = []
    seen = set()
    for extinf, url in official:
        name = channel_name(extinf)
        is_cctv = bool(CCTV_RE.match(name))
        is_hot = name in HOT_TV
        if (is_cctv or is_hot) and name not in seen:
            seen.add(name)
            extinf = extinf.replace("https:/aptv.app/", "https://aptv.app/")
            url = url.replace("https:/aptv.app/", "https://aptv.app/")
            targets.append((name, extinf, url))
    targets.sort(key=lambda x: sort_key(x[0]))

    # 备用源：从 Guovin 池按归一化名匹配，取前2条不同的源
    backup_map = {}
    for extinf, url in backup:
        name = norm(channel_name(extinf))
        if name not in seen:
            continue
        backup_map.setdefault(name, [])
        if url not in backup_map[name]:
            backup_map[name].append(url)

    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    lines = [
        "#EXTM3U",
        "# 全部CCTV + 10热门卫视 | 每频道3条线路(第1条官方国内源最快最稳，第2/3条海外备用可能有广告)",
        "# 自动生成时间(北京时间): " + now,
        "# 主频道 %d 个" % len(targets),
    ]
    for name, extinf, url in targets:
        lines.append(extinf)
        lines.append(url)
        cands = backup_map.get(name, [])[:2]
        for idx, burl in enumerate(cands, 1):
            lines.append('#EXTINF:-1 tvg-name="%s" group-title="备用线路",%s 备用%d' % (name, name, idx))
            lines.append(burl)
        if len(cands) < 2:
            print("WARN: %s 备用源仅 %d 条" % (name, len(cands)))
    return "\n".join(lines) + "\n", [x[0] for x in targets], backup_map


def main():
    official = parse(fetch(OFFICIAL_SOURCES))
    if len(official) < 30:
        raise RuntimeError("官方池解析条数异常(%d)" % len(official))
    backup = parse(fetch(BACKUP_SOURCES))
    if len(backup) < 100:
        raise RuntimeError("备用池解析条数异常(%d)" % len(backup))
    content, names, backup_map = build(official, backup)
    cctv_n = len([n for n in names if CCTV_RE.match(n)])
    tv_n = len(names) - cctv_n
    if cctv_n < 15 or tv_n < 9:
        raise RuntimeError("频道数异常(CCTV=%d, 卫视=%d)" % (cctv_n, tv_n))

    if os.path.exists(OUT):
        with open(OUT, "r", encoding="utf-8") as f:
            if f.read() == content:
                print("无变化，跳过写入")
                return
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    total_bak = sum(min(len(v), 2) for v in backup_map.values())
    print("生成 cctv.m3u: %d 主频道 (CCTV %d + 卫视 %d) + %d 备用线路" % (
        len(names), cctv_n, tv_n, total_bak))
    print("卫视: " + ", ".join(n for n in names if n in HOT_TV))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR: " + str(e), file=sys.stderr)
        sys.exit(1)
