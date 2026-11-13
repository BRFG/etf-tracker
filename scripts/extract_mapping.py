#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 证据/站点一_index.html 提取 86 类映射表(大类->子类->ETF清单)"""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "证据", "站点一_index.html"), encoding="utf-8").read()

# 1) ETFS: {"子类": ["515070 华夏...", ...]}
m = re.search(r"var\s+ETFS\s*=\s*(\{.*?\})\s*;", html, re.S)
if not m:
    sys.exit("ETFS 未找到")
etfs = json.loads(m.group(1))
print(f"ETFS: {len(etfs)} 个子类, 共 {sum(len(v) for v in etfs.values())} 只标的")

# 2) 大类->子类: 从 charts 分片名和 tab 结构找。先看页面里的 tabs/目录映射
tabs = re.findall(r"data-cid=\"([^\"]+)\"[^>]*>([^<]+)<", html)
print(f"tabs: {tabs[:20]}")
# 备选: ensureCat / charts/xx.js 引用
cats = sorted(set(re.findall(r"charts/([^\"']+)\.js", html)))
print(f"charts 引用: {cats}")

# 3) 大类顺序与名称（页面 tab 文本）
for kw in ("宽基市场", "风格策略", "大类"):
    i = html.find(kw)
    print(f"\n--- {kw} 上下文 ---\n{html[max(0,i-300):i+500]}")
    break

# 输出映射初稿
out = {}
for k, v in etfs.items():
    codes = []
    for item in v:
        code = item.split()[0]
        name = item[len(code):].strip()
        codes.append({"code": code, "name": name})
    out[k] = codes
with open(os.path.join(ROOT, "scripts", "mapping_origin.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"\n已输出 scripts/mapping_origin.json")
# 号段统计
from collections import Counter
seg = Counter(c["code"][:3] for v in out.values() for c in v)
print("号段分布:", dict(seg.most_common()))
