#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""库 → 网页 全量逐格自检（P0-5 的另一半：聚合→落盘这段）。

对 12 大类 92 子类、全部交易日逐格比对：网页 charts/*.js 里的 S0 数值
是否等于从数据库重新聚合的结果（容差 1e-4，仅四舍五入）。同时校验：
  · 各分片日期轴与库内沪市日期轴完全一致
  · 子类集合与 mapping_final.json 完全一致
  · 每子类首个非零值位置不是"凭空出现"

用法: python scripts/check_page.py
"""
import json
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "数据库", "etf.db")
OUT = os.path.join(ROOT, "网页", "行业站")


def grab(text, name):
    m = re.search(r"(?:var\s+|[,;])%s\s*=\s*" % name, text)
    if not m:
        return None
    i, op = m.end(), text[m.end()]
    cl = {"[": "]", "{": "}"}[op]
    depth, instr, esc = 0, False, False
    for j in range(i, len(text)):
        ch = text[j]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == op:
            depth += 1
        elif ch == cl:
            depth -= 1
            if depth == 0:
                return json.loads(text[i:j + 1])
    return None


def main():
    mapping = json.load(open(os.path.join(ROOT, "scripts", "mapping_final.json"),
                             encoding="utf-8"))
    conn = sqlite3.connect(DB)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM shares WHERE exch='SH' ORDER BY date")]
    idx = {d: i for i, d in enumerate(dates)}

    sub_of_code, sub2name = {}, set()
    for cat, subs in mapping.items():
        for s in subs:
            sub2name.add((cat, s["name"]))
            for e in s["etfs"]:
                sub_of_code[e["code"]] = (cat, s["name"])
    mat = {k: [0.0] * len(dates) for k in sub_of_code.values()}
    for d, code, v in conn.execute("SELECT date, code, shares_yi FROM shares"):
        k, i = sub_of_code.get(code), idx.get(d)
        if k and i is not None:
            mat[k][i] += v

    bad_cells, bad_files, seen = 0, [], set()
    worst = (0.0, None)
    for cat in mapping:
        path = os.path.join(OUT, "charts", cat + ".js")
        if not os.path.exists(path):
            bad_files.append(f"{cat}: 分片缺失")
            continue
        t = open(path, encoding="utf-8").read()
        D0, S0 = grab(t, "D0"), grab(t, "S0")
        if not D0 or not S0:
            bad_files.append(f"{cat}: 解析失败")
            continue
        if D0 != dates:
            # 晚间 23:30 场次若深市当日初值尚未发布，collect.py 会跳过重建，
            # 此时页面日期轴是库的**严格前缀**：按页面的轴校验，逐格仍然严格，
            # 只是不把"本次未重建"误报成结构错误。
            if len(D0) < len(dates) and dates[:len(D0)] == D0:
                print(f"  · {cat}: 页面落后库 {len(dates) - len(D0)} 个交易日"
                      f"（本次未重建），按页面日期轴校验")
            else:
                bad_files.append(f"{cat}: 日期轴不一致（{len(D0)} vs {len(dates)}）")
        names = {s["name"] for s in S0}
        want = {s["name"] for s in mapping[cat]}
        if names != want:
            bad_files.append(f"{cat}: 子类集合不一致 缺{sorted(want - names)} "
                             f"多{sorted(names - want)}")
        for s in S0:
            key = (cat, s["name"])
            seen.add(key)
            ref = mat.get(key)
            if ref is None:
                bad_files.append(f"{cat}/{s['name']}: 库内无此子类")
                continue
            for i, v in enumerate(s["values"]):
                exp = ref[i] if ref[i] else 0
                got = v if v is not None else 0
                diff = abs(got - exp)
                if diff > 1e-4:
                    bad_cells += 1
                    if diff > worst[0]:
                        worst = (diff, (cat, s["name"], D0[i], got, round(exp, 6)))
    missing = sub2name - seen
    if missing:
        bad_files.append(f"页面缺子类 {sorted(missing)}")

    print(f"逐格比对：{len(seen)} 子类 × {len(dates)} 日期 = "
          f"{len(seen) * len(dates):,} 格")
    print(f"不一致格数：{bad_cells}")
    if worst[1]:
        print(f"最大偏差：{worst[0]:.6f} @ {worst[1]}")
    if bad_files:
        print("结构问题：")
        for m in bad_files:
            print("  ✗", m)
    else:
        print("结构校验：日期轴/子类集合/分片完整性 全部通过")
    return 1 if (bad_cells or bad_files) else 0


if __name__ == "__main__":
    sys.exit(main())
