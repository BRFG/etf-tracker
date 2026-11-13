#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按妙想MCP查询到的「跟踪标的指数」复核归类审查区的 27 只 ETF，并就地修正
mapping_final.json。产出 review_flags.json = 已复核记录（含跟踪指数）。
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MF = os.path.join(ROOT, "scripts", "mapping_final.json")

# 代码 -> (跟踪指数, 目标(大类, 子类), 展示名)  目标为 None 表示位置不变
FIX = {
    # —— 原「未归类」2 只，现已定位 ——
    "510140": ("上证指数", ("1_宽基市场", "A股全指"), "上证综指ETF华夏"),
    "526070": ("恒指港股通指数", ("1_宽基市场", "港股宽基"), "恒指港股通ETF博时"),
    # —— 深证成指 = 深市 500 只大市值股，应按大盘宽基而非全指 ——
    "159903": ("深证成指", ("1_宽基市场", "A股大盘"), "深证成指ETF南方"),
    "159943": ("深证成指", ("1_宽基市场", "A股大盘"), "深证成指ETF大成"),
    # —— CS稀金属 → 稀有金属（原放有色金属） ——
    "561970": ("CS稀金属", ("6_资源", "稀有金属"), "稀有金属ETF博时"),
    # —— 2026-11-12 用户定稿：新经济ETF银华按风格策略归「综合策略」（原在科技综合） ——
    "159822": ("标普中国新经济", ("2_风格策略", "综合策略"), "新经济ETF银华"),
    # —— 原站/规则均漏收的政金债 ETF ——
    "511520": ("中债政金债", ("10_债券", "中长期利率债"), "政金债ETF"),
    # —— 位置正确，仅补全可读名称 ——
    "589490": ("科创成长", None, "科创成长ETF华夏"),
    "515440": ("中证A股", None, "A股ETF南方"),
    "513660": ("恒生指数", None, "恒生ETF华夏"),
    "159965": ("央视50", None, "央视50ETF国联"),
    "159912": ("深证300", None, "深证300ETF汇添富"),
    "159902": ("中小100", None, "中小100ETF华夏"),
    "159627": ("中证A100", None, "A100ETF华夏"),
    "159630": ("中证A100", None, "A100ETF汇添富"),
    "159631": ("中证A100", None, "A100ETF招商"),
    "159661": ("中证A100", None, "A100ETF嘉实"),
    "159686": ("中证A100", None, "A100ETF易方达"),
    "159923": ("中证A100", None, "中证A100ETF大成"),
    "159997": ("CS电子", None, "电子ETF天弘"),
    "159701": ("CS物联网", None, "物联网ETF招商"),
    "159709": ("物联网50", None, "物联网ETF工银"),
    "159895": ("CS物联网", None, "物联网ETF易方达"),
    "159896": ("CS物联网", None, "物联网ETF南方"),
    "159106": ("民企300", None, "民企300ETF前海开源"),
    "159973": ("民企100", None, "民企ETF弘毅远方"),
}


def main():
    m = json.load(open(MF, encoding="utf-8"))
    sub_of = {}
    for cat, subs in m.items():
        for s in subs:
            for e in s["etfs"]:
                sub_of[e["code"]] = (cat, s)

    recs = {}
    for code, (idx, target, disp) in FIX.items():
        cur = sub_of.get(code)
        # 1) 从原位置移除（若换格子）
        if target and cur and (cur[0], cur[1]["name"]) != target:
            cur[1]["etfs"] = [e for e in cur[1]["etfs"] if e["code"] != code]
            cur = None
        # 2) 名称补全
        if cur:
            for e in cur[1]["etfs"]:
                if e["code"] == code and disp:
                    e["name"] = disp
            recs[code] = {"idx": idx, "cat": cur[0], "sub": cur[1]["name"],
                          "name": disp or "", "action": "复核确认"}
        # 3) 新增到目标格子
        if target:
            cat, sub = target
            entry = next(s for s in m[cat] if s["name"] == sub)
            exch = "SH" if code.startswith(("5", "6")) else "SZ"
            # 幂等：先移除同代码旧条目，再追加（避免重跑产生重复）
            entry["etfs"] = [e for e in entry["etfs"] if e["code"] != code]
            entry["etfs"].append({"code": code, "name": disp, "exch": exch})
            entry["etfs"].sort(key=lambda e: e["code"])
            recs[code] = {"idx": idx, "cat": cat, "sub": sub,
                          "name": disp, "action": "新归类"}

    json.dump(m, open(MF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(recs, open(os.path.join(ROOT, "scripts", "review_flags.json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)
    n = sum(len(s["etfs"]) for v in m.values() for s in v)
    print(f"已修正 {len(recs)} 只；映射合计 {n} 只 / "
          f"{sum(len(v) for v in m.values())} 子类")
    for c, r in sorted(recs.items()):
        print(f"  {c} {r['name']:<18} {r['idx']:<14} -> {r['cat']} / {r['sub']}  [{r['action']}]")


if __name__ == "__main__":
    main()
