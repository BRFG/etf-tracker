#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取站点一 10 个分片JS，提取 大类->子类 归属 + 各子类颜色，合成完整映射"""
import json
import os
import re
import sys
import urllib.request
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

CATS = ["1_宽基市场", "2_风格策略", "3_科技", "4_制造", "5_能源",
        "6_资源", "7_消费", "7_医药", "8_金融地产", "9_债券"]

texts = {}
for cid in CATS:
    url = ("https://etf-flow-d0j.pages.dev/charts/"
           + urllib.parse.quote(cid) + ".js")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            texts[cid] = r.read().decode("utf-8")
        print(f"  拉取 {cid}: {len(texts[cid])} B")
    except Exception as ex:
        print(f"  ! {cid} 失败: {ex}", file=sys.stderr)

# 解析每个分片: S0=[{"name":"人工智能","color":"#4976CC","values":[...]},...]
result = {}
for cid, text in texts.items():
    m = re.search(r"var\s+S0\s*=\s*(\[.*?\])\s*;", text, re.S)
    if m:
        arr = json.loads(m.group(1))
        subs = [{"name": s["name"], "color": s.get("color")} for s in arr]
    else:  # 兜底：S0/S1 里 name 按首次出现顺序去重
        names, seen = [], set()
        for nm in re.findall(r'"name":"([^"]+)"', text):
            if nm not in seen:
                seen.add(nm)
                names.append(nm)
        colors = dict(re.findall(r'"name":"([^"]+)","color":"(#[0-9A-Fa-f]{6})"', text))
        subs = [{"name": nm, "color": colors.get(nm)} for nm in names]
    result[cid] = subs
    print(f"{cid}: {len(subs)} 个子类 -> {[s['name'] for s in subs]}")

mapping = json.load(open(os.path.join(ROOT, "scripts", "mapping_origin.json"),
                         encoding="utf-8"))
out = {}
n = 0
for cid, subs in result.items():
    for s in subs:
        name = s["name"]
        if name not in mapping:
            print(f"  ! 子类[{name}] 在 ETFS 中不存在!", file=sys.stderr)
            continue
        out.setdefault(cid, []).append({
            "name": name, "color": s["color"],
            "etfs": mapping[name]})
        n += len(mapping[name])
with open(os.path.join(ROOT, "scripts", "mapping_full.json"), "w",
          encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"\n合计: {sum(len(v) for v in out.values())} 子类 / {n} 只 ETF")
print("已输出 scripts/mapping_full.json")

# LOF 检出（501/502）
lof = [(c['code'], c['name']) for v in out.values() for s in v for c in s['etfs']
       if c['code'][:3] in ('501', '502')]
print(f"\n待剔除 LOF: {len(lof)} 只 -> {[c[0] for c in lof]}")
