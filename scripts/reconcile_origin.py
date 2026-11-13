#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业站 12 大类逐类对账：本站库内数据 vs 原站分片（P0-5 闭环）。

关键前提（实测得出，勿混用两套口径）：
  1. 原站是**纯沪市**（773 只，159 开头 0 只）；本站按需求并入深市，故
     「本站全量」天然大于原站，两者的差 = 深市份额，不是数据错误。
     真正可比的是「本站沪市部分（用原站映射 mapping_origin）」。
  2. 原站**债券 10 类是「份额×100」口径**（≈面值/规模），本站统一为份额（亿份），
     两者恒差 100 倍，判定时按单位差异单列，不算差异。

分类结果：
  OK        全程一致（容差 = max(0.6 亿份, 0.3%)）
  SPIKE     个别单日跳变后回归（份额折算/拆分，用户已裁决不做复权）
  DRIFT     末日仍不一致（真差异，需要查）
  UNIT×100  债券类单位口径不同

用法: python scripts/reconcile_origin.py [--refetch]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "数据库", "etf.db")
CACHE = os.path.join(ROOT, "证据", "原站分片")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")
SHARDS = ["1_宽基市场", "2_风格策略", "3_科技", "4_制造", "5_能源",
          "6_资源", "7_消费", "7_医药", "8_金融地产", "9_债券"]
BOND_SHARD = "9_债券"


def grab(text, name):
    """取 `var X=[...]` 或 `,X=[...]` 的数组值（括号配对、字符串感知）。"""
    m = re.search(r"(?:var\s+|[,;])%s\s*=\s*" % name, text)
    if not m:
        return None
    i = m.end()
    op = text[i]
    cl = {"[": "]", "{": "}"}.get(op)
    if not cl:
        return None
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


def load_shard(cid, refetch=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, cid + ".js")
    if refetch or not os.path.exists(path):
        url = ("https://etf-flow-d0j.pages.dev/charts/"
               + urllib.parse.quote(cid) + ".js")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            open(path, "wb").write(r.read())
        time.sleep(0.4)
    return open(path, encoding="utf-8").read()


def agg(conn, conn_dates, sub2codes):
    """{子类: {日期: 合计}}"""
    idx = {d: i for i, d in enumerate(conn_dates)}
    out = {}
    for sub, codes in sub2codes.items():
        arr = [0.0] * len(conn_dates)
        q = ",".join("?" * len(codes))
        for d, v in conn.execute(
                f"SELECT date, shares_yi FROM shares WHERE code IN ({q})", codes):
            i = idx.get(d)
            if i is not None:
                arr[i] += v
        out[sub] = arr
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    final = json.load(open(os.path.join(ROOT, "scripts", "mapping_final.json"),
                           encoding="utf-8"))
    origin = json.load(open(os.path.join(ROOT, "scripts", "mapping_origin.json"),
                            encoding="utf-8"))
    sh_only = {sub: [e["code"] for e in es] for sub, es in origin.items()}
    ours_all = {}
    for cat, subs in final.items():
        for s in subs:
            ours_all[s["name"]] = [e["code"] for e in s["etfs"]]
    ours_cat = {s["name"]: cat for cat, subs in final.items() for s in subs}

    conn = sqlite3.connect(DB)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM shares WHERE exch='SH' ORDER BY date")]
    idx = {d: i for i, d in enumerate(dates)}
    A_sha = agg(conn, dates, sh_only)
    A_all = agg(conn, dates, {k: v for k, v in ours_all.items() if k in sh_only})

    rows, cat_tot = [], {}
    for cid in SHARDS:
        text = load_shard(cid, args.refetch)
        D0, S0 = grab(text, "D0"), grab(text, "S0")
        if not D0 or not S0:
            print(f"  ! {cid} 解析失败", file=sys.stderr)
            continue
        common = [d for d in D0 if d in idx]
        print(f"\n== {cid} == 子类 {len(S0)}，共同日期 {len(common)}"
              f"（{common[0]} ~ {common[-1]}）")
        for s in S0:
            sub, tv = s["name"], s["values"]
            if sub not in A_sha:
                rows.append({"shard": cid, "sub": sub, "verdict": "本站无此子类"})
                print(f"  ? {sub}: 本站无此子类")
                continue
            ds, da = [], []
            for d in common:
                i = idx[d]
                j = D0.index(d)
                if tv[j] is None:
                    continue
                ds.append(A_sha[sub][i] - tv[j])
                da.append(A_all[sub][i] - tv[j])
            if not ds:
                rows.append({"shard": cid, "sub": sub, "verdict": "无重叠数据"})
                continue
            last = tv[D0.index(common[-1])] if tv[D0.index(common[-1])] else 0
            # 原站/本站 比值：债券类 ≈100（原站按每份 100 元面值折成规模）
            end_ratio = last / A_sha[sub][idx[common[-1]]] if A_sha[sub][idx[common[-1]]] else None
            unit = "×100" if end_ratio and 90 < end_ratio < 110 else ""
            tol = max(0.6, 0.003 * abs(last))
            bad = [(common[k], round(ds[k], 4)) for k in range(len(ds))
                   if abs(ds[k]) > tol]
            if unit:
                verdict = "UNIT×100"
            elif not bad:
                verdict = "OK"
            elif abs(ds[-1]) <= tol:
                verdict = "SPIKE"
            else:
                verdict = "DRIFT"
            r = {"shard": cid, "sub": sub, "verdict": verdict,
                 "n": len(ds), "last_ours_sh": round(A_sha[sub][idx[common[-1]]], 3),
                 "last_orig": tv[D0.index(common[-1])],
                 "last_diff": round(ds[-1], 4), "n_bad": len(bad),
                 "max_abs": round(max(abs(x) for x in ds), 4),
                 "sz_added": round(A_all[sub][idx[common[-1]]]
                                   - A_sha[sub][idx[common[-1]]], 3),
                 "bad_sample": bad[:3]}
            rows.append(r)
            t = cat_tot.setdefault(cid, {"sha": 0.0, "all": 0.0, "orig": 0.0})
            if not unit:
                t["sha"] += A_sha[sub][idx[common[-1]]]
                t["all"] += A_all[sub][idx[common[-1]]]
                t["orig"] += tv[D0.index(common[-1])]
            print(f"  {verdict:8s} {sub}: 末 我沪 {r['last_ours_sh']:.1f}"
                  f" / 原 {r['last_orig']} / 差 {r['last_diff']:+.2f}"
                  f" | 深市并入 +{r['sz_added']:.1f}"
                  + (f" | 超差{r['n_bad']}天 例{r['bad_sample']}" if bad else ""))

    print("\n== 大类合计（沪市同口径，最后共同日）==")
    for cid, t in cat_tot.items():
        d = t["sha"] - t["orig"]
        print(f"  {cid}: 我沪 {t['sha']:.1f} / 原 {t['orig']:.1f} / 差 {d:+.1f}"
              f" / 含深 {t['all']:.1f}")

    from collections import Counter
    cnt = Counter(r["verdict"] for r in rows)
    print("\n结论：", dict(cnt))
    out = os.path.join(ROOT, "证据", "对账_原站分类.json")
    json.dump({"rows": rows, "cat_tot": cat_tot}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"明细 -> {out}")
    return 1 if cnt.get("DRIFT") else 0


if __name__ == "__main__":
    sys.exit(main())
