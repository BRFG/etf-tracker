"""远端连通性诊断：在 GitHub runner（境外 IP）上逐个探测数据源。

用途：确认三家交易所 / 东方财富是否对境外 IP 限流。
手动触发 .github/workflows/probe.yml 即可，输出每项的状态、耗时、行数。

本地也能跑（此时反映的是本地网络），便于对照。
"""
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_db import (fetch_cffex_day, fetch_kline, fetch_sse_day,  # noqa: E402
                      fetch_szse_window, make_session)


def probe(name, fn):
    t0 = time.time()
    try:
        rows = fn()
    except Exception as e:                                  # noqa: BLE001
        print(f"[FAIL] {name:28s} {time.time()-t0:6.1f}s  异常: {type(e).__name__}: {e}")
        return False
    dt = time.time() - t0
    n = len(rows) if rows is not None else -1
    if n > 0:
        sample = rows[0] if isinstance(rows[0], (tuple, list)) else str(rows[0])[:60]
        print(f"[ OK ] {name:28s} {dt:6.1f}s  {n} 行  样例={str(sample)[:80]}")
        return True
    print(f"[WARN] {name:28s} {dt:6.1f}s  返回 {n} 行（0 行可能是非交易日，需换日期复测）")
    return False


def main():
    sess = make_session()
    # 选两个近期交易日做探测（周中，避开周末）
    days = [date(2026, 11, 12), date(2026, 11, 11), date(2026, 11, 10)]

    print("=" * 72)
    print("远端连通性诊断  ", time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 72)

    ok_sh = False
    for d in days:
        print(f"\n-- 上交所 {d} --")
        if probe(f"沪份额 {d}", lambda d=d: fetch_sse_day(sess, d)):
            ok_sh = True
            break

    print(f"\n-- 深交所 {days[2]}~{days[0]} --")
    probe("深份额 窗口(5日)",
          lambda: fetch_szse_window(sess, days[2] - timedelta(days=4), days[0]))

    ok_cffex = False
    for d in days:
        print(f"\n-- 中金所 {d} --")
        if probe(f"期货席位 {d}", lambda d=d: fetch_cffex_day(sess, d)):
            ok_cffex = True
            break

    print("\n-- 东方财富 K线 --")
    probe("K线 510300", lambda: fetch_kline(sess, "510300"))

    print("\n" + "=" * 72)
    verdict = []
    if ok_sh:
        verdict.append("沪 OK")
    else:
        verdict.append("沪 疑似被拦")
    if ok_cffex:
        verdict.append("期货 OK")
    else:
        verdict.append("期货 疑似被拦")
    print("结论：" + " / ".join(verdict) + "（深市与 K 线看上面 OK/WARN）")
    print("=" * 72)


if __name__ == "__main__":
    main()
