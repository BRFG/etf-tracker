#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
深交所 ETF/LOF/REITs 日频份额 —— 历史回补脚本

接口（页面 https://www.szse.cn/market/fund/volume/etf/index.html 背后的报表服务）:
    https://www.szse.cn/api/report/ShowReport
        ?SHOWTYPE=xlsx&CATALOGID=scsj_fund_jjgm&TABKEY=tab1
        &txtStart=YYYY-MM-DD&txtEnd=YYYY-MM-DD&jjlb=ETF

关键约束（均已实测）:
  1. 单次查询跨度硬上限 6 个月：正好 6 个月可用，多 1 天直接返回空表。
     → 本脚本按「6 个月」自动切窗。
  2. xlsx 导出的「基金规模(份)」单位是【份】；
     同一报表的 JSON 输出（SHOWTYPE=JSON）单位是【万份】。两者差 1e4 倍，别混。
  3. openpyxl 用 read_only=True 读这个 xlsx 会得到 max_row=1 的假空表
     （维度记录缺失），必须用普通模式 load_workbook(io.BytesIO(...))。
  4. 数据是 T 日晚间初值，T+1 早间会修正。官方说明原文：
     「T日晚间更新的T日规模仅供参考，以T+1日早间更新的T日规模为准。」
     → 落库后建议 T+1 重抓最近 5 个交易日覆盖。
  5. 最早可回溯到 2016 年底（2016-06 为空），非 2021 起。
  6. xlsx 导出有【65535 行】上限（Excel 65536 含表头）。6 个月窗口对 ETF 来说
     行数约 = 772 只 x ~120 交易日 ≈ 9 万行，会被静默截断且不报错。
     → 本脚本检测到 65535 行即自动二分拆窗重抓。

用法:
    python szse_backfill.py --start 2026-04-01 --end 2026-11-10
    python szse_backfill.py --start 2021-11-01 --end 2026-11-10 --symbol ETF --out all.csv
"""
import argparse
import calendar
import csv
import io
import os
import random
import sys
import time
from datetime import date, datetime, timedelta

import requests
from openpyxl import load_workbook

URL = "https://www.szse.cn/api/report/ShowReport"

META = {
    "ETF": {
        "jjlb": "ETF",
        "referer": "https://www.szse.cn/market/fund/volume/etf/index.html",
    },
    "LOF": {
        "jjlb": "LOF",
        "referer": "https://www.szse.cn/market/fund/volume/lof/index.html",
    },
    "REITS": {
        "jjlb": "不动产基金",
        "referer": "https://www.szse.cn/market/fund/volume/reits/index.html",
    },
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def split_windows(start: date, end: date):
    """按 6 个月切窗，保证每窗跨度 <= 6 个月（接口硬限制）"""
    cur = start
    while cur <= end:
        w_end = min(end, add_months(cur, 6) - timedelta(days=1))
        yield cur, w_end
        cur = w_end + timedelta(days=1)


XLSX_ROW_CAP = 65535  # xlsx 导出上限，达到即视为被截断


def fetch_range(sess: requests.Session, s: date, e: date, symbol: str, depth: int = 0):
    """取区间数据；若触到 65535 行上限则二分拆窗重抓"""
    rows = fetch_window(sess, s, e, symbol)
    if rows is None:
        return []
    if len(rows) < XLSX_ROW_CAP or s >= e:
        return rows
    # 被截断：二分重取
    mid = s + timedelta(days=(e - s).days // 2)
    pad = "  " * depth
    print(f"{pad}    触发 65535 行截断，二分拆为 {s}~{mid} / {mid + timedelta(days=1)}~{e}")
    left = fetch_range(sess, s, mid, symbol, depth + 1)
    time.sleep(0.5)
    right = fetch_range(sess, mid + timedelta(days=1), e, symbol, depth + 1)
    return left + right


def fetch_window(sess: requests.Session, s: date, e: date, symbol: str):
    """返回 [(日期, 代码, 简称, 份额_份)]，失败返回 None"""
    meta = META[symbol]
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "scsj_fund_jjgm",
        "TABKEY": "tab1",
        "txtStart": s.isoformat(),
        "txtEnd": e.isoformat(),
        "jjlb": meta["jjlb"],
        "random": str(random.random()),
    }
    headers = {"Referer": meta["referer"], "User-Agent": UA}
    for attempt in range(3):
        try:
            r = sess.get(URL, params=params, headers=headers, timeout=90)
            r.raise_for_status()
            if not r.content:
                raise ValueError("空响应体")
            # 注意：必须普通模式，read_only=True 会误判为只有 1 行
            wb = load_workbook(io.BytesIO(r.content))
            ws = wb.active
            rows = ws.iter_rows(values_only=True)
            header = [str(c or "").strip() for c in next(rows)]

            def col(*names):
                for n in names:
                    for i, h in enumerate(header):
                        if n in h:
                            return i
                return None

            ci_d, ci_c = col("日期"), col("基金代码", "代码")
            ci_n, ci_s = col("基金简称", "简称"), col("基金规模", "当前规模")
            if None in (ci_d, ci_c, ci_s):
                raise ValueError(f"表头不认识: {header}")

            out = []
            for row in rows:
                try:
                    d = str(row[ci_d]).strip()[:10]
                    code = str(row[ci_c]).strip().split(".")[0].zfill(6)
                    size = float(str(row[ci_s]).replace(",", "").strip())
                except (TypeError, ValueError, IndexError):
                    continue
                if len(d) != 10:
                    continue
                name = str(row[ci_n]).strip() if ci_n is not None else ""
                out.append((d, code, name, size))
            return out
        except Exception as ex:
            print(f"    ! {s}~{e} 第{attempt+1}次失败: {ex}", file=sys.stderr)
            time.sleep(2 + 2 * attempt)
    return None


def main():
    ap = argparse.ArgumentParser(description="深交所基金日频份额历史回补")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--symbol", default="ETF", choices=list(META))
    ap.add_argument("--out", default="", help="输出 CSV 路径")
    ap.add_argument("--gap", type=float, default=0.7, help="每窗间隔秒数")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if start > end:
        sys.exit("start 不能晚于 end")

    out = args.out or f"szse_{args.symbol.lower()}_share_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    sess = requests.Session()
    sess.trust_env = False  # 直连，避免系统代理干扰

    windows = list(split_windows(start, end))
    print(f"共 {len(windows)} 个窗口 -> {out}")

    seen = set()
    rows_all = []
    for idx, (s, e) in enumerate(windows, 1):
        print(f"[{idx}/{len(windows)}] {s} ~ {e}", flush=True)
        rows = fetch_range(sess, s, e, args.symbol)
        if not rows:
            print("    × 整窗失败或无数据，跳过（可稍后单独重跑该区间）", file=sys.stderr)
            continue
        new = 0
        for d, code, name, size in rows:
            key = (code, d)
            if key in seen:
                continue
            seen.add(key)
            rows_all.append((d, code, name, size / 1e8))  # 份 -> 亿份
            new += 1
        print(f"    -> {len(rows)} 行，新增 {new}（累计 {len(rows_all)}）")
        if idx < len(windows):
            time.sleep(args.gap)

    if not rows_all:
        sys.exit("没有取到任何数据")

    rows_all.sort(key=lambda r: (r[0], r[1]))
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "基金代码", "基金简称", "份额_亿份"])
        for r in rows_all:
            w.writerow([r[0], r[1], r[2], f"{r[3]:.6f}"])

    days = len({r[0] for r in rows_all})
    codes = len({r[1] for r in rows_all})
    print(f"\n完成: {len(rows_all)} 行 / {days} 个交易日 / {codes} 只基金")
    print(f"日期范围: {min(r[0] for r in rows_all)} ~ {max(r[0] for r in rows_all)}")
    print(f"输出: {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
