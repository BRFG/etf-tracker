#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日增量采集（每晚 23:30 采当日初值 + 次日早 08:45 采终值，用 conda-excel python）

三个必须机制：
  1. 追到最新：读库最新日期 -> 逐日追到目标日（自动补周末/节假日/关机漏跑）
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
import random
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
                      fetch_kline_batch, fetch_cffex_day, make_session,
                      open_db, WATCH, FUT_PRODUCTS)

FAILS = []      # 致命失败：本次未采到，必须告警
NOTES = []      # 提示信息
BUILT = []      # 本次成功重建的站点脚本名
BUILD_ERR = []  # 重建本身报错的站点（页面可能不完整，必须阻止发布）
ALIGN_LAG = False  # 两市末日不一致但属「晚间时序差」（非致命）：沪已到 T、深 23:20 初值未出

# 退出码约定（workflow 依赖它决定是否发布）：
#   0 = 全部成功
#   1 = 致命失败，不发布（份额没采到 / 重建本身报错，页面可能不完整）
#   2 = 部分降级，可发布（例如东财限流导致国家队站暂不重建，行业站已是最新）

# 已确认剔除、不进映射的标的（深市货币 ETF），避免每天误报"未归类"
KNOWN_SKIP = {"159001", "159003", "159005"}


def beijing_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)


def is_evening() -> bool:
    """当前是否晚间场次（>=20:00）。

    两个场次的**采集范围**不同，不只是目标日不同：
      · 晚间：从「库内最新日 +1」往后推进 —— 目的是把当日新数据写进来；
      · 早间：从「库内最新日」原样重抓 —— 目的是把 T 日的初值覆盖成终值。
    分界点 20:00 比深市 23:20 早很多，Actions 延迟 1~2 小时也不会误判。
    """
    return beijing_now().hour >= 20


def beijing_target_date() -> date:
    """本次采集的目标日（北京时间）。

    定时是「每晚 23:30 采当日初值 + 次日早 08:45 采终值」：
      · 23:30 跑：沪市 23:00、深市 23:20 已发布，目标日 = **当天**；
      · 08:45 跑：当日尚未收盘，目标日 = **昨天**（只做终值覆盖，不新增日期）。
    若极端延迟跨了零点，当天日期已 +1，hour<20 取"昨天"恰好仍是原目标日 T。
    """
    now = beijing_now()
    return now.date() if is_evening() else now.date() - timedelta(days=1)


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
    end = beijing_target_date()
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
    last_d = date.fromisoformat(sh_last)
    # 早间场次必须**重抓库内最新日**，而不是从次日往后推：
    # 官方口径是「深市 T 日晚间仅供参考，以 T+1 早间更新的规模为准」，沪市同属
    # 清算后数据也要再确认一次。若一律从 last+1 起，早间这场在「库内已到 T」时
    # d 会直接越过 end，沪市一行不采、终值永远没被覆盖过——早间场次等于白跑。
    # min(last_d, end) 同时覆盖两种情况：昨晚成功（重抓 T）与昨晚漏跑（补 T-1~T）。
    d = last_d + timedelta(days=1) if is_evening() else min(last_d, end)
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
        return False
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
        # 注意：这里**不再**做「早间终值校正」的全量重拉。日 K 线（OHLCV/VWAP）
        # 是当日收盘后定稿的数据，晚间 23:30 采到的与次日早间采到的完全一致；
        # 早间再全量重拉 18 只只会多打一倍东财请求、加速触发频率限流，毫无收益。

    if not todo:
        print(f"[K线] {len(WATCH)} 只均已追至 {target}，无区间缺口")
        return True

    ok, bad, graded = 0, [], []
    # 批量快照优先：push2 实时接口一次请求拿 todo 全体的当日收盘价+VWAP，
    # 走的是与 push2his 不同的域名/接口，不受「每分钟 6 次」频率限流影响。
    # 每日增量的常态是「18 只都只缺最新一天」，批量一次即可追平，push2his
    # 只用来兜底批量拿不到的标的（停牌/无行情）与历史大缺口。
    snap = fetch_kline_batch(sess, [e["code"] for e in todo])
    for e in list(todo):
        code = e["code"]
        if code in snap:
            close, vwap, amt = snap[code]
            conn.execute("INSERT OR REPLACE INTO kline VALUES(?,?,?,?,?)",
                         (target, code, close, vwap, amt))
            conn.commit()
            ok += 1
    hist_todo = [e for e in todo if e["code"] not in snap]
    if snap:
        print(f"[K线] 批量快照追平 {ok} 只（1 次请求），"
              f"剩余 {len(hist_todo)} 只走历史接口")

    streak = 0                      # 连续取不到目标日数据的只数（限流判据）
    # push2his 对单个出口 IP 的频率限流实测约为「每分钟 6 次」：连续请求约 6~7 只后
    # 开始 Connection reset。熔断不该放弃剩余，否则缺口永远补不平。冷却后继续，
    # 把剩余的拆成多轮跑完（批量快照没拿到的通常是少数，这里一般很快）。
    cool_wait = [90, 180, 300]      # 冷却阶梯（秒），逐轮加长
    cool_i = 0
    for i, e in enumerate(hist_todo):
        code = e["code"]
        if code in why:
            print(f"  · {code} {why[code]}")
        # 截止日必须传 target：fetch_kline 默认只到「北京昨天」，
        # 晚间 23:30 场次若不传，永远拿不到当日 K 线，K 线会永久落后一天。
        rows = fetch_kline(sess, code, end=date.fromisoformat(target))
        reached = bool(rows) and max(r[0] for r in rows) >= target
        if rows:
            conn.executemany("INSERT OR REPLACE INTO kline VALUES(?,?,?,?,?)", rows)
            conn.commit()
        if reached:
            ok += 1
            streak = 0
        else:
            # 库里已有目标日收盘价时，本次只是没做成终值确认，数据本身完整，
            # 不该因此拦下整个国家队站的重建（降级为提示）。
            have_target = conn.execute(
                "SELECT 1 FROM kline WHERE code=? AND date=?", (code, target)).fetchone()
            if have_target:
                print(f"    ⚠ {code} 本次未取到 {target}，沿用库内已有收盘价", file=sys.stderr)
                graded.append(code)
                streak = 0
            else:
                if rows:
                    print(f"    ⚠ {code} 返回末日 {max(r[0] for r in rows)} 仍早于 {target}"
                          f"（东财尚未发布当日 K 线）", file=sys.stderr)
                bad.append(code)
                streak += 1
        # 连续 3 只取不到 → 判定频率限流：冷却后再继续，而不是放弃剩余。
        if streak >= 3:
            if cool_i < len(cool_wait):
                wait = cool_wait[cool_i]
                cool_i += 1
                print(f"    ⏳ 连续 {streak} 只被限流，冷却 {wait}s 后继续"
                      f"（剩余 {len(hist_todo) - i - 1} 只）", file=sys.stderr)
                time.sleep(wait)
                streak = 0
            else:
                fail(f"K线冷却 {len(cool_wait)} 轮仍被限流，中止本轮"
                     f"（剩余 {len(hist_todo) - i - 1} 只留待下次运行）")
                break
        time.sleep(1.5 + random.random())
    print(f"[K线] 需补 {len(todo)} 只，追平 {ok} 只"
          + (f"，终值未确认 {len(graded)} 只" if graded else ""))
    if bad:
        fail(f"K线 {len(bad)} 只未追至 {target}（{','.join(bad)}），"
             f"下次运行自动重试")
        return False
    return True


def collect_futures(conn, sess):
    """期货席位：只在**已确认的交易日**上推进，绝不越过未确认的日期。

    上限取「已确认交易日历最大日」与「昨日」的较小值：
    若今日份额采集失败，日历最大日停在前一天，游标自然停住，
    下次运行会重新从同一天开始，不会因为"某天被误判为休市"而留下永久缺口。
    """
    cal = trading_calendar(conn)
    if not cal:
        NOTES.append("期货跳过：份额库为空，无法确定交易日历")
        return False
    last = conn.execute("SELECT MAX(date) FROM futures").fetchone()[0]
    if not last:
        NOTES.append("期货跳过：futures 表为空，请先用 build_db.py --cffex-only 回补")
        return False
    # 上限同样取目标日：晚上 23:30 跑时中金所当日席位已发布（16:30~17:00 更新），
    # 早上 08:45 跑时目标日=昨天，席位早已采过，两者都不会越过未确认的日期。
    cap = min(date.fromisoformat(max(cal)), beijing_target_date())
    d = date.fromisoformat(last) + timedelta(days=1)
    if d > cap:
        print(f"[期货] 已追平至 {last}（已确认交易日历止于 {max(cal)}）")
        return True
    print(f"[期货] 从 {d} 追至 {cap}（已确认交易日历 {len(cal)} 天）")
    n = 0
    fut_ok = True
    while d <= cap:
        ds = d.isoformat()
        if d.weekday() < 5 and ds in cal:
            rows = fetch_cffex_day(sess, d)
            if not rows:
                # 份额已有该日 => 确定是交易日；拿不到席位数据就是真失败，停止推进
                fail(f"中金所 {d} 为交易日但无数据，已停止推进（下次续采）")
                fut_ok = False
                break
            # 品种齐全性：fetch_cffex_day 内部对单个品种失败是 continue，
            # 会把「IH 成功、IM 失败」当成整日成功 → 席位合计少一个品种且无告警。
            have_p = {r[1] for r in rows}
            miss_p = [p for p in FUT_PRODUCTS if p not in have_p]
            if miss_p:
                fail(f"中金所 {d} 品种不全（缺 {','.join(miss_p)}），"
                     f"已拒绝入库并停止推进（下次续采）")
                fut_ok = False
                break
            conn.executemany(
                "INSERT OR REPLACE INTO futures VALUES(?,?,?,?,?,?,?)", rows)
            conn.commit()
            n += 1
        d += timedelta(days=1)
    print(f"[期货] 新增 {n} 个交易日")
    return fut_ok


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
        # 晚间 23:30 场次的正常时序差：沪市 23:00、深市 23:20 才发布初值，
        # 任一方稍有延迟就会出现「一边已到 T、另一边停在 T-1」。这不是故障，
        # 而是官方公布的更新时序；此时不重建（避免发布沪深错配的页面），
        # 次日早间场次会自动补齐并把终值写进去。
        global ALIGN_LAG
        ahead, behind = max(sh_max, sz_max), min(sh_max, sz_max)
        lag = sorted(d for d in trading_calendar(conn) if d > behind)
        if is_evening() and len(lag) == 1:
            side = "深市" if sz_max < sh_max else "沪市"
            print(f"  ⏳ {side} {ahead} 初值尚未发布（落后至 {behind}），"
                  f"晚间场次属正常时序差，明早自动补齐", file=sys.stderr)
            NOTES.append(f"{side} {ahead} 尚未发布，本次不重建（明早补终值）")
            ALIGN_LAG = True
            return False
        fail(f"两市末日不一致（沪 {sh_max} / 深 {sz_max}）：本次有市场未追平，暂不重建页面")
        return False
    return True


def rebuild(scripts):
    for script in scripts:
        r = subprocess.run([PY, os.path.join(ROOT, "scripts", script)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"[重建] {script}: {'OK' if ok else 'FAIL'}")
        if ok:
            BUILT.append(script)
        else:
            FAILS.append(f"重建 {script} 失败")
            BUILD_ERR.append(script)
            print(r.stderr[-2000:], file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true")
    # 本机 IP 被东财限流时，只为验证份额链路而跳过 K 线（生产不传这个参数）。
    # 跳过时沿用库内已有 K 线，国家队站的市值会用到旧价，故仅用于调试。
    ap.add_argument("--skip-kline", action="store_true",
                    help="跳过 K 线采集（仅本机调试用，生产环境不要用）")
    args = ap.parse_args()

    conn = open_db()
    sess = make_session()
    sh_ok, sz_ok = collect_shares(conn, sess)
    if args.skip_kline:
        print("[K线] ⏭ 已按 --skip-kline 跳过（沿用库内已有收盘价）")
        NOTES.append("K线本次跳过（--skip-kline）")
        # 跳过不等于可用：K 线必须已覆盖最后一个交易日，否则国家队站的市值会用旧价，
        # 按「任一缺价即置空」的严格口径它就不该重建。
        kl_max = conn.execute("SELECT MAX(date) FROM kline").fetchone()[0]
        cal = trading_calendar(conn)
        kline_ok = bool(kl_max and cal and kl_max >= max(cal))
        if not kline_ok:
            NOTES.append(f"K线止于 {kl_max}，落后于交易日历末日，国家队站不重建")
    else:
        kline_ok = collect_kline(conn, sess)
    fut_ok = collect_futures(conn, sess)
    alive = check_align(conn)
    check_futures(conn, trading_calendar(conn))

    if not args.no_build:
        # 按站点分别判断，而不是"一处失败两站都不发"：
        #   · 行业站只吃份额，只要沪深份额齐全且对齐就能重建；
        #   · 国家队站还要收盘价（净申赎/市值）与期货席位，缺一项就会出半截数据。
        # 这样晚间场次即便东财当日 K 线还没出，行业站也能当晚更新，国家队站次日早补齐。
        base_ok = sh_ok and sz_ok and alive
        todo = []
        if base_ok:
            todo.append("build_industry.py")
        if base_ok and kline_ok and fut_ok:
            todo.append("build_gjd.py")
        if todo:
            rebuild(todo)
        if len(todo) < 2:
            miss = [s for s in ("build_industry.py", "build_gjd.py") if s not in todo]
            print(f"[重建] ⏸ 跳过 {', '.join(miss)}：其依赖数据本次未全部就绪，"
                  f"该站页面保留上一版（避免混入半截数据）", file=sys.stderr)
            NOTES.append(f"未重建 {', '.join(miss)}：依赖数据未就绪，保留上一版")

    print("\n" + "=" * 60)
    if NOTES:
        for m in NOTES:
            print(f"[提示] {m}")
    if FAILS:
        print(f"[结果] ❌ {len(FAILS)} 项失败（数据缺口已避免，下次运行自动续采）:")
        for m in FAILS:
            print(f"   - {m}")

    # 退出码决定 workflow 是否发布：
    #   · 份额没采到 / 重建报错 / 两市末日不一致（非时序差）→ 页面不可信，1（不发布）
    #   · 只有 K 线或期货未就绪 → 已重建的站是新数据，其余站保持上一版，2（照常发布）
    #   · 晚间时序差（ALIGN_LAG）→ 本次本就没有新数据可发，不致命，落到 0/2
    if BUILD_ERR or (not alive and not ALIGN_LAG) or not (sh_ok and sz_ok):
        log_run(conn, "FAIL")
        sys.exit(1)
    if FAILS:
        print(f"[结果] ⚠️ 部分降级：{len(BUILT)} 个站已更新并发布，"
              f"其余站保留上一版（退出码 2，不阻断发布）")
        log_run(conn, "PARTIAL")
        sys.exit(2)
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
