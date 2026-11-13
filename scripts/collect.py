#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日增量采集（计划任务 08:45 运行，用 conda-excel python）

三个必须机制：
  1. 追到最新：读库最新日期 -> 逐日追到昨日（自动补周末/节假日/关机漏跑）
  2. 0 条 = 非交易日；None / 异常 = 失败，**绝不静默跳过**
  3. 深市初值可被终值覆盖：每日重抓最近 5 个交易日（INSERT OR REPLACE 幂等）

失败处理原则（审查日志 P1-1/2/3/5）：
  - 沪份额失败 -> 立即停止推进（不越过缺口），下次运行从缺口处续；退出码 1
  - 深市窗口失败 -> 重试后仍失败则记为失败，退出码 1（窗口本身幂等，下次自动覆盖）
  - 交易日只认「沪深份额日期并集」这个已确认日历；期货/K线的推进上限卡在日历最大日，
    绝不越过未确认的日期（否则会被误判为休市而留下永久缺口）
  - 期货：当日 404 且当日不在日历内 = 休市跳过；在日历内却无数据 = 失败，停止推进
  - K 线：逐只判断，末日落后**或区间内有缺口**都补；不再"一只成功全组 by pass"
  - 结束时校验沪深日期集合（全库）与末日一致性
  - 采集不完整时不重建页面，避免"沪停在旧日、深到新日"的混合数据上首页

用法:
  python collect.py            # 采集 + 重建两站
  python collect.py --no-build # 只采集不重建
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "数据库", "etf.db")
PY = sys.executable

from build_db import (fetch_sse_day, fetch_szse_window, fetch_kline,  # noqa: E402
                      fetch_cffex_day, make_session, open_db, WATCH,
                      FUT_PRODUCTS)

FAILS = []      # 致命失败：本次未采到，必须告警
NOTES = []      # 提示信息

# 已确认剔除、不进映射的标的（深市货币 ETF），避免每天误报"未归类"
KNOWN_SKIP = {"159001", "159003", "159005"}


def beijing_yesterday() -> date:
    return (datetime.utcnow() + timedelta(hours=8)).date() - timedelta(days=1)


def fail(msg):
    FAILS.append(msg)
    print(f"  ✗ {msg}", file=sys.stderr)


def trading_calendar(conn) -> set:
    """已确认的交易日历 = 沪深份额日期的并集。

    只包含"已经采到份额"的日期，即**已确认**的收盘日；未采到的日期不在其中。
    刻意不用"当日库内有没有份额"来判断单日是否交易日——那会与本次采集形成循环依赖：
    沪采集失败 -> 该日无份额 -> 被误判为休市 -> 期货跳过该日并推进游标 -> 永久缺口。
    调用方应改用 cap = 日历最大日 来限制推进，而不是逐日猜。
    """
    return {r[0] for r in conn.execute("SELECT DISTINCT date FROM shares")}


def collect_shares(conn, sess):
    """返回 (沪市是否完整采到, 深市是否完整采到)。"""
    end = beijing_yesterday()
    sh_last = conn.execute("SELECT MAX(date) FROM shares WHERE exch='SH'").fetchone()[0]
    sz_last = conn.execute("SELECT MAX(date) FROM shares WHERE exch='SZ'").fetchone()[0]
    if not sh_last or not sz_last:
        fail("份额库为空：增量脚本不做全量回补，请先运行 build_db.py")
        return False, False
    print(f"[份额] 沪最新 {sh_last} / 深最新 {sz_last} / 目标追至 {end}")

    # ---------- 沪：逐日，失败即停 ----------
    n_sh = 0
    sh_ok = True
    # 只数守卫：接口偶发只回半页（HTTP 200 + 部分行）时，若照单入库就会把
    # 几百只 ETF 的份额算成"清零"，页面出现巨额假流出。实测 2415 个交易日
    # 里从无单日只数下跌超 15%，故 0.8 倍阈值不会误伤真实退市。
    prev_cnt = conn.execute("SELECT COUNT(*) FROM shares WHERE date=? AND exch='SH'",
                            (sh_last,)).fetchone()[0]
    d = date.fromisoformat(sh_last) + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            rows = fetch_sse_day(sess, d)      # 内部已重试 3 次
            if rows is None:
                fail(f"沪份额 {d} 采集失败，已停止推进（下次运行从此日续采）")
                sh_ok = False
                break
            if rows and prev_cnt and len(rows) < prev_cnt * 0.8:
                fail(f"沪份额 {d} 只回 {len(rows)} 只，仅为前一日 {prev_cnt} 只的"
                     f" {len(rows)/prev_cnt:.0%}（疑似只返回半页），已拒绝入库并停止推进")
                sh_ok = False
                break
            if rows:                            # [] = 非交易日
                conn.executemany(
                    "INSERT OR REPLACE INTO shares VALUES(?,?,?,?,?)",
                    [(d.isoformat(), c, n, "SH", v) for c, n, v in rows])
                conn.commit()
                n_sh += 1
                prev_cnt = len(rows)
        time.sleep(0.2)
        d += timedelta(days=1)
    print(f"[沪] 新增 {n_sh} 个交易日")

    # ---------- 深：窗口直取（含终值覆盖）----------
    # 深交所 ShowReport 的跨度上限是 6 个月（多 1 天直接返 0 行，不报错），
    # 所以按 ≤170 天切段追赶：关机一两个月不会越界，关机半年也能一次补完。
    sz_ok = True
    start = min(date.fromisoformat(sz_last) + timedelta(days=1), end - timedelta(days=5))
    seg_lo, n_sz = start, 0
    while seg_lo <= end:
        seg_hi = min(end, seg_lo + timedelta(days=170))
        rows = fetch_szse_window(sess, seg_lo, seg_hi)   # 内部已重试 3 次
        if rows is None:
            fail(f"深份额 {seg_lo}~{seg_hi} 采集失败（窗口幂等，下次运行自动覆盖）")
            sz_ok = False
            break
        if not rows:
            print(f"[深] 窗口 {seg_lo}~{seg_hi}: 0 行（无交易日或接口未发布）")
        else:
            batch = [(dd, code, name, "SZ", v) for dd, code, name, v in rows]
            cnt = {}
            for r in batch:
                cnt[r[0]] = cnt.get(r[0], 0) + 1
            prev = conn.execute(
                "SELECT date, COUNT(*) FROM shares WHERE exch='SZ' AND date<? "
                "GROUP BY date ORDER BY date DESC LIMIT 1", (min(cnt),)).fetchone()
            bad = []
            for dd in sorted(cnt):
                if prev and cnt[dd] < prev[1] * 0.8:
                    bad.append((dd, prev[1], cnt[dd]))
                prev = (dd, cnt[dd])
            if bad:
                fail(f"深份额 {bad[0][0]} 只回 {bad[0][2]} 只，仅为前一日"
                     f" {bad[0][1]} 只的 {bad[0][2]/bad[0][1]:.0%}（疑似残表），"
                     f"已拒绝入库（下次运行自动覆盖）")
                sz_ok = False
                break
            conn.executemany("INSERT OR REPLACE INTO shares VALUES(?,?,?,?,?)", batch)
            conn.commit()
            n_sz += len(batch)
            print(f"[深] 窗口 {seg_lo}~{seg_hi}: {len(batch)} 行（含终值覆盖）")
        if seg_hi >= end:
            break
        seg_lo = seg_hi + timedelta(days=1)
        time.sleep(0.5)
    if n_sz == 0:
        print("[深] 无新增行")

    # ---------- 新代码未映射告警 ----------
    mapping = json.load(open(os.path.join(ROOT, "scripts", "mapping_final.json"),
                             encoding="utf-8"))
    known = {e["code"] for cat in mapping.values() for s in cat for e in s["etfs"]}
    last_day = conn.execute("SELECT MAX(date) FROM shares").fetchone()[0]
    for ex in ("SH", "SZ"):
        # 带上名称：货币 ETF 按"名称含货币"识别，不能按 511 号段一刀切
        # ——511 号段里 3/4 是债券 ETF（511010 国债、511520 政金债…），屏蔽会漏掉新上市债券 ETF
        rows = conn.execute("SELECT DISTINCT code, name FROM shares WHERE date=? AND exch=?",
                            (last_day, ex)).fetchall()
        new_codes = [c for c, n in rows
                     if c not in known and c not in KNOWN_SKIP
                     and "货币" not in (n or "")]
        if new_codes:
            NOTES.append(f"[{ex}] 新代码 {len(new_codes)} 只未归类: {new_codes[:10]}")
            print(f"  ⚠️ [{ex}] 未映射新代码 {len(new_codes)} 只: {new_codes[:10]}"
                  f"（需人工归类到 mapping_final.json）", file=sys.stderr)
    return sh_ok, sz_ok


def collect_kline(conn, sess):
    """逐只判断：末日落后 或 区间内有缺口，都补（P1-3）。

    K 线接口每次返回完整区间，重新拉一次即可自愈，所以不必做增量拼接。
    目标日取**已确认的最后一个交易日**（份额日历最大日），而不是"昨天"：
    周末/节假日运行时不会把 18 只全部重拉一遍，也不会把"还没收盘"当成缺口。
    """
    cal = trading_calendar(conn)
    if not cal:
        NOTES.append("K线跳过：份额库为空，无法确定交易日历")
        return
    target = max(cal)
    cal_list = sorted(d for d in cal if d >= "2023-12-01")

    todo, why = [], {}
    for e in WATCH:
        code = e["code"]
        have = {r[0] for r in conn.execute("SELECT date FROM kline WHERE code=?", (code,))}
        if not have:
            todo.append(e)
            why[code] = "无任何K线"
            continue
        first, last = min(have), max(have)
        miss = [d for d in cal_list if first <= d <= last and d not in have]
        if last < target:
            todo.append(e)
            why[code] = f"末日 {last} 落后" + (f"、区间缺 {len(miss)} 天" if miss else "")
        elif miss:
            todo.append(e)
            why[code] = f"区间缺 {len(miss)} 天（{miss[0]} ~ {miss[-1]}）"

    if not todo:
        print(f"[K线] {len(WATCH)} 只均已追至 {target}，无区间缺口")
        return

    ok, bad = 0, []
    for e in todo:
        if e["code"] in why:
            print(f"  · {e['code']} {why[e['code']]}")
        rows = fetch_kline(sess, e["code"])
        if rows:
            conn.executemany("INSERT OR REPLACE INTO kline VALUES(?,?,?,?,?)", rows)
            conn.commit()
            ok += 1
        else:
            bad.append(e["code"])
        time.sleep(1.2)
    print(f"[K线] 需补 {len(todo)} 只，成功 {ok} 只")
    if bad:
        fail(f"K线 {len(bad)} 只采集失败（{','.join(bad)}），下次运行自动重试")


def collect_futures(conn, sess):
    """期货席位：只在**已确认的交易日**上推进，绝不越过未确认的日期。

    上限取「已确认交易日历最大日」与「昨日」的较小值：
    若今日份额采集失败，日历最大日停在前一天，游标自然停住，
    下次运行会重新从同一天开始，不会因为"某天被误判为休市"而留下永久缺口。
    """
    cal = trading_calendar(conn)
    if not cal:
        NOTES.append("期货跳过：份额库为空，无法确定交易日历")
        return
    last = conn.execute("SELECT MAX(date) FROM futures").fetchone()[0]
    if not last:
        NOTES.append("期货跳过：futures 表为空，请先用 build_db.py --cffex-only 回补")
        return
    cap = min(date.fromisoformat(max(cal)), beijing_yesterday())
    d = date.fromisoformat(last) + timedelta(days=1)
    if d > cap:
        print(f"[期货] 已追平至 {last}（已确认交易日历止于 {max(cal)}）")
        return
    print(f"[期货] 从 {d} 追至 {cap}（已确认交易日历 {len(cal)} 天）")
    n = 0
    while d <= cap:
        ds = d.isoformat()
        if d.weekday() < 5 and ds in cal:
            rows = fetch_cffex_day(sess, d)
            if not rows:
                # 份额已有该日 => 确定是交易日；拿不到席位数据就是真失败，停止推进
                fail(f"中金所 {d} 为交易日但无数据，已停止推进（下次续采）")
                break
            # 品种齐全性：fetch_cffex_day 内部对单个品种失败是 continue，
            # 会把「IH 成功、IM 失败」当成整日成功 → 席位合计少一个品种且无告警。
            have_p = {r[1] for r in rows}
            miss_p = [p for p in FUT_PRODUCTS if p not in have_p]
            if miss_p:
                fail(f"中金所 {d} 品种不全（缺 {','.join(miss_p)}），"
                     f"已拒绝入库并停止推进（下次续采）")
                break
            conn.executemany(
                "INSERT OR REPLACE INTO futures VALUES(?,?,?,?,?,?,?)", rows)
            conn.commit()
            n += 1
        d += timedelta(days=1)
    print(f"[期货] 新增 {n} 个交易日")


def check_futures(conn, cal):
    """期货席位日期连续性校验（P1-2 的补充）。

    futures 的游标是 "MAX(date)+1"，一旦历史上出现中间缺口，
    后续运行**永远不会回头补**（游标已越过），也不会有任何报错。
    这里把缺口显式打出来，属于校验项而非采集项：发现缺口需手工回补。
    """
    fu = {r[0] for r in conn.execute("SELECT DISTINCT date FROM futures")}
    if not fu:
        return
    lo, hi = min(fu), min(max(fu), max(cal))
    miss = [d for d in sorted(cal) if lo <= d <= hi and d not in fu]
    extra = sorted(fu - cal)
    print(f"[期货] {lo}~{max(fu)} 共 {len(fu)} 日"
          f"，日历内缺口 {len(miss)} 天" + (f"（{miss[:5]}…）" if miss else ""))
    if miss:
        NOTES.append(f"期货历史缺口 {len(miss)} 天: {miss[:5]}"
                     f"{'…' if len(miss) > 5 else ''}（需手工回补）")
        print("  ⚠️ 期货存在历史缺口，游标不会自动回补", file=sys.stderr)
    if extra:
        NOTES.append(f"期货有非交易日数据 {len(extra)} 天: {extra[:5]}"
                     f"（中金所假日排期与沪深不同，一般可忽略）")


def check_align(conn):
    """沪深日期集合对齐校验（P1-5），覆盖全库而非仅 2024 起。

    行业站「全部」用 2016-12 起全量，若只验 2024 起的 692 天，
    前 1723 天的缺口永远看不见。返回「末日是否一致」，用于阻止混数据重建。
    """
    sh = {r[0] for r in conn.execute("SELECT DISTINCT date FROM shares WHERE exch='SH'")}
    sz = {r[0] for r in conn.execute("SELECT DISTINCT date FROM shares WHERE exch='SZ'")}
    only_sh, only_sz = sorted(sh - sz), sorted(sz - sh)
    print(f"[对齐] 全库 沪 {len(sh)} 日 / 深 {len(sz)} 日"
          f"，仅沪 {len(only_sh)} / 仅深 {len(only_sz)}")
    if only_sh:
        NOTES.append(f"仅沪有数据: {only_sh[:8]}{'…' if len(only_sh) > 8 else ''}")
    if only_sz:
        NOTES.append(f"仅深有数据: {only_sz[:8]}{'…' if len(only_sz) > 8 else ''}")
    if only_sh or only_sz:
        print("  ⚠️ 两市日期不完全对齐（历史缺口），见上方清单", file=sys.stderr)

    sh_max = max(sh) if sh else None
    sz_max = max(sz) if sz else None
    if sh_max != sz_max:
        fail(f"两市末日不一致（沪 {sh_max} / 深 {sz_max}）：本次有市场未追平，暂不重建页面")
        return False
    return True


def rebuild():
    for script in ("build_industry.py", "build_gjd.py"):
        r = subprocess.run([PY, os.path.join(ROOT, "scripts", script)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"[重建] {script}: {'OK' if ok else 'FAIL'}")
        if not ok:
            FAILS.append(f"重建 {script} 失败")
            print(r.stderr[-2000:], file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true")
    args = ap.parse_args()

    conn = open_db()
    sess = make_session()
    sh_ok, sz_ok = collect_shares(conn, sess)
    collect_kline(conn, sess)
    collect_futures(conn, sess)
    alive = check_align(conn)
    check_futures(conn, trading_calendar(conn))

    if not args.no_build:
        # 采集不完整时不重建：否则页面只写「更新至 X」，实际是"沪停在旧日、深到新日"、
        # 或"份额到新日但价格/席位还停在前一天"的半截数据，且没有任何标记，比停更更危险。
        # 因此门槛是"零失败"，而不是只看沪深份额。
        if sh_ok and sz_ok and alive and not FAILS:
            rebuild()
        else:
            print("[重建] ⏸ 已跳过：本次采集未全部成功（或两市末日不一致），"
                  "页面保留上一版（避免混入半截数据）", file=sys.stderr)
            NOTES.append("页面未重建：采集未全部成功时保留上一版")

    print("\n" + "=" * 60)
    if NOTES:
        for m in NOTES:
            print(f"[提示] {m}")
    if FAILS:
        print(f"[结果] ❌ {len(FAILS)} 项失败（数据缺口已避免，下次运行自动续采）:")
        for m in FAILS:
            print(f"   - {m}")
        log_run(conn, "FAIL")
        sys.exit(1)
    print("[结果] ✅ 全部成功")
    log_run(conn, "OK")


def log_run(conn, status):
    """运行结果落盘（退出码 1 目前没有接收方，至少留痕可查）。"""
    try:
        pos = {ex: conn.execute("SELECT MAX(date) FROM shares WHERE exch=?",
                                (ex,)).fetchone()[0] for ex in ("SH", "SZ")}
        fu = conn.execute("SELECT MAX(date) FROM futures").fetchone()[0]
        kl = conn.execute("SELECT MAX(date) FROM kline").fetchone()[0]
        with open(os.path.join(ROOT, "数据库", "collect.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {status}"
                    f" 沪{pos['SH']} 深{pos['SZ']} 期货{fu} K线{kl}"
                    f" 失败{len(FAILS)}\n")
            for m in FAILS:
                f.write(f"    FAIL {m}\n")
            for m in NOTES:
                f.write(f"    NOTE {m}\n")
    except Exception as ex:            # 日志失败绝不能影响退出码
        print(f"[日志] 写入失败: {ex}", file=sys.stderr)


if __name__ == "__main__":
    main()
