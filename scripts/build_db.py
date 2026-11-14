#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
首次建库 / 断点续跑：沪深 ETF 份额 -> SQLite 原始库
  沪：query.sse.com.cn 按日（幂等，0条=非交易日/未发布，周末实测返0条）
  深：www.szse.cn scsj_fund_jjgm 日频 xlsx（6个月切窗 + 65535行自动二分）
  另含：东财日K（16只国家队）+ 中金所期货（IH/IF/IC/IM，中信期货席位）
统一单位：亿份。库为原始层，只做 INSERT OR REPLACE（幂等覆盖），派生层另算。

用法（conda-excel 环境）:
  python build_db.py            # 全部：深2016-12-01起 + 沪2016-12-01起 + K线 + 期货
  python build_db.py --sh-only  # 只跑沪（可反复跑，已入库日期自动跳过）
"""
import argparse
import calendar
import io
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

import requests
from openpyxl import load_workbook

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DB_PATH = os.path.join(ROOT, "数据库", "etf.db")
CFG = json.load(open(os.path.join(ROOT, "证据", "站点二_config.json"), encoding="utf-8"))
WATCH = CFG["watchlist"]
FUT_MEMBER = CFG["futures"]["member"]
FUT_PRODUCTS = list(CFG["futures"]["products"])

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

SH_START = date(2016, 12, 1)   # 行业站起点（深市可回溯最早）
SZ_START = date(2016, 12, 1)
GJD_START = date(2023, 12, 1)  # 国家队站K线起点（多留1个月做首日flow基准）
FUT_START = date(2024, 1, 1)   # 期货起点（国家队站起点）


def yesterday() -> date:
    return (datetime.utcnow() + timedelta(hours=8)).date() - timedelta(days=1)


# ---------------------------------------------------------------- SQLite

def open_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS shares(
        date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
        exch TEXT NOT NULL, shares_yi REAL NOT NULL,
        PRIMARY KEY(date, code))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS kline(
        date TEXT NOT NULL, code TEXT NOT NULL,
        close REAL, vwap REAL, amount_yi REAL,
        PRIMARY KEY(date, code))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS futures(
        date TEXT NOT NULL, product TEXT NOT NULL, contract TEXT NOT NULL,
        long INTEGER, long_chg INTEGER, short INTEGER, short_chg INTEGER,
        PRIMARY KEY(date, product, contract))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_code ON shares(code)")
    conn.commit()
    return conn


def sh_dates_done(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM shares WHERE exch='SH'")}


def sz_dates_done(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM shares WHERE exch='SZ'")}


# ---------------------------------------------------------------- 采集

def make_session():
    s = requests.Session()
    s.trust_env = False  # 直连，不走系统代理
    s.headers.update({"User-Agent": UA})
    return s


def fetch_sse_day(sess, d: date):
    """上交所某日全部 ETF 份额。返回 [(code, name, 亿份)]；0条返回 []"""
    url = "https://query.sse.com.cn/commonQuery.do"
    params = {
        "isPagination": "true", "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
        "STAT_DATE": d.strftime("%Y-%m-%d"),
    }
    headers = {"Referer": "https://www.sse.com.cn/"}
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            out = []
            for row in data.get("result", []):
                code = str(row.get("SEC_CODE", "")).strip()
                vol = row.get("TOT_VOL")  # 万份
                name = str(row.get("SEC_NAME", "")).strip()
                if code and vol not in (None, "", "-"):
                    out.append((code, name, float(vol) / 10000.0))
            return out
        except Exception as ex:
            print(f"  ! 沪 {d} 第{attempt+1}次失败: {ex}", file=sys.stderr)
            time.sleep(1 + attempt)
    return None  # None=失败(区别于[]=非交易日)


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


XLSX_ROW_CAP = 65535


def fetch_szse_window(sess, s: date, e: date):
    """深交所 xlsx 窗口。返回 [(日期, 代码, 简称, 亿份)]；None=失败"""
    url = "https://www.szse.cn/api/report/ShowReport"
    params = {
        "SHOWTYPE": "xlsx", "CATALOGID": "scsj_fund_jjgm", "TABKEY": "tab1",
        "txtStart": s.isoformat(), "txtEnd": e.isoformat(),
        "jjlb": "ETF", "random": str(random.random()),
    }
    headers = {"Referer": "https://www.szse.cn/market/fund/volume/etf/index.html"}
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, headers=headers, timeout=120)
            r.raise_for_status()
            if not r.content:
                raise ValueError("空响应体")
            wb = load_workbook(io.BytesIO(r.content))  # 必须普通模式
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
                    dd = str(row[ci_d]).strip()[:10]
                    code = str(row[ci_c]).strip().split(".")[0].zfill(6)
                    size = float(str(row[ci_s]).replace(",", "").strip())
                except (TypeError, ValueError, IndexError):
                    continue
                if len(dd) != 10:
                    continue
                name = str(row[ci_n]).strip() if ci_n is not None else ""
                out.append((dd, code, name, size / 1e8))
            return out
        except Exception as ex:
            print(f"  ! 深 {s}~{e} 第{attempt+1}次失败: {ex}", file=sys.stderr)
            time.sleep(2 + 2 * attempt)
    return None


def fetch_szse_range(sess, s: date, e: date, depth=0):
    rows = fetch_szse_window(sess, s, e)
    if rows is None:
        return []
    if len(rows) < XLSX_ROW_CAP or s >= e:
        return rows
    mid = s + timedelta(days=(e - s).days // 2)
    print(f"{'  '*depth}  触发65535截断，二分 {s}~{mid} / {mid+timedelta(days=1)}~{e}")
    left = fetch_szse_range(sess, s, mid, depth + 1)
    time.sleep(0.5)
    right = fetch_szse_range(sess, mid + timedelta(days=1), e, depth + 1)
    return left + right


def fetch_kline(sess, code, end=None):
    """end 缺省为北京昨天；晚间 23:30 场次要传当天，否则永远拿不到当日 K 线。"""
    secid = ("1." if code[0] == "5" else "0.") + code
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "klt": "101", "fqt": "0",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "beg": GJD_START.strftime("%Y%m%d"),
        "end": (end or yesterday()).strftime("%Y%m%d"),
    }
    for attempt in range(5):
        try:
            r = sess.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            out = []
            for line in ((data or {}).get("data") or {}).get("klines", []):
                p = line.split(",")
                try:
                    d, close, vol, amt = p[0], float(p[2]), float(p[5]), float(p[6])
                    vwap = amt / (vol * 100.0) if vol > 0 else None
                    out.append((d, code, close, vwap, amt / 1e8))
                except (ValueError, IndexError):
                    continue
            return out
        except Exception as ex:
            # 连接层被直接掐断（RemoteDisconnected 是东财限流/封 IP 的典型表现），
            # 退避多久都难恢复，重试 5 次只会让封禁更久、把本机调试拖成 2 分钟以上；
            # 缩短为 2 次尝试，把重试预算留给下一轮运行（全链路幂等）。
            # 超时、5xx、解析失败等才是瞬时故障，仍按原退避重试。
            hard = isinstance(ex, (requests.ConnectionError,
                                   requests.exceptions.ChunkedEncodingError))
            print(f"  ! K线 {code} 第{attempt+1}次失败: {ex}", file=sys.stderr)
            if hard and attempt >= 1:
                break
            time.sleep(2 if hard else 3 * (attempt + 1))
    return []


def fetch_cffex_day(sess, d: date):
    """中金所某日 -> [(product, contract, long, long_chg, short, short_chg)] 仅中信期货"""
    name_of = lambda s: re.split(r"[(（]", s)[0].strip()
    out = []
    for var in FUT_PRODUCTS:
        url = (f"http://www.cffex.com.cn/sj/ccpm/{d.strftime('%Y%m')}/"
               f"{d.strftime('%d')}/{var}_1.csv")
        text = None
        for i in range(2):
            try:
                r = sess.get(url, timeout=30)
                if r.status_code == 404:
                    break
                r.raise_for_status()
                text = r.content.decode("gbk", errors="replace")
                break
            except Exception as ex:
                print(f"  ! CFFEX {var} {d} 失败: {ex}", file=sys.stderr)
                time.sleep(1)
        if not text:
            continue
        # 列: 交易日,合约,排名, 成交量会员,成交量,增减, 持买会员,持买量,增减, 持卖会员,持卖量,增减
        # 同一合约的「成交量/持买/持卖」三组排名互相独立，中信期货可能出现在
        # 不同的排名行号上 → 必须先按合约聚合，否则后一行会覆盖前一行的另一半。
        agg = {}
        for line in text.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 12 or not p[0].isdigit():
                continue
            contract = p[1].upper()
            if not (contract.startswith(var) and contract[len(var):].isdigit()):
                continue
            try:
                cur = agg.setdefault(contract, [None, None, None, None])
                if name_of(p[6]) == FUT_MEMBER:      # 持买单排名
                    cur[0], cur[1] = int(p[7]), int(p[8])
                if name_of(p[9]) == FUT_MEMBER:      # 持卖单排名
                    cur[2], cur[3] = int(p[10]), int(p[11])
            except ValueError:
                continue
        for contract, (lon, lonc, sho, shoc) in agg.items():
            if lon is not None or sho is not None:
                out.append((d.strftime("%Y-%m-%d"), var, contract,
                            lon, lonc, sho, shoc))
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- 主流程

def run_sh(conn):
    sess = make_session()
    end = yesterday()
    done = sh_dates_done(conn)
    d, n_days, n_rows = SH_START, 0, 0
    while d <= end:
        if d.weekday() >= 5 or d.isoformat() in done:
            d += timedelta(days=1)
            continue
        rows = fetch_sse_day(sess, d)
        if rows is None:
            print(f"  沪 {d} 失败，跳过（重跑续传）", file=sys.stderr)
        elif rows:
            conn.executemany(
                "INSERT OR REPLACE INTO shares VALUES(?,?,?,?,?)",
                [(d.isoformat(), c, n, "SH", v) for c, n, v in rows])
            conn.commit()
            n_days += 1
            n_rows += len(rows)
            if n_days % 100 == 0:
                print(f"  沪 已入库 {n_days} 交易日 / {n_rows} 行（至 {d}）", flush=True)
        time.sleep(0.25)
        d += timedelta(days=1)
    print(f"[沪] 完成：本轮新增 {n_days} 个交易日 / {n_rows} 行")


def run_sz(conn):
    sess = make_session()
    end = yesterday()
    done = sz_dates_done(conn)
    # 找到还没完成的窗口：按6个月切，全跳过已有大量日期的窗（逐窗重抓成本低，直接重抓并REPLACE）
    n_rows = 0
    cur = SZ_START
    windows = []
    while cur <= end:
        w_end = min(end, add_months(cur, 6) - timedelta(days=1))
        windows.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    print(f"[深] 共 {len(windows)} 个窗口（6个月切窗）")
    for idx, (s, e) in enumerate(windows, 1):
        # 窗口内已全部入库则跳过
        days_in = {s + timedelta(days=i) for i in range((e - s).days + 1)}
        if days_in <= done:
            continue
        rows = fetch_szse_range(sess, s, e)
        if not rows:
            print(f"  深 {s}~{e} 整窗失败或无数据，可重跑续传", file=sys.stderr)
            continue
        seen = set()
        batch = []
        for dd, code, name, v in rows:
            if dd in done:  # 已有该日数据（终值已入库），不覆盖
                continue
            key = (dd, code)
            if key in seen:
                continue
            seen.add(key)
            batch.append((dd, code, name, "SZ", v))
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO shares VALUES(?,?,?,?,?)", batch)
            conn.commit()
            n_rows += len(batch)
        new_days = sorted({b[0] for b in batch})
        print(f"  [{idx}/{len(windows)}] {s}~{e}: {len(rows)} 行, 入库 {len(batch)}"
              f"（{new_days[0]}~{new_days[-1]}）" if batch else
              f"  [{idx}/{len(windows)}] {s}~{e}: 无新数据", flush=True)
        time.sleep(0.7)
    print(f"[深] 完成：本轮新增 {n_rows} 行")


def run_kline(conn):
    sess = make_session()
    have = {r[0] for r in conn.execute("SELECT DISTINCT code FROM kline")}
    for e in WATCH:
        code = e["code"]
        if code in have:
            continue
        rows = fetch_kline(sess, code)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO kline VALUES(?,?,?,?,?)", rows)
            conn.commit()
        print(f"[K线] {code} {e['name']}: {len(rows)} 天", flush=True)
        time.sleep(0.4)


def run_cffex(conn, force=False):
    sess = make_session()
    end = yesterday()
    done = set()
    if not force:
        done = {r[0] for r in conn.execute("SELECT DISTINCT date FROM futures")}
    d, n_days, n_rows = FUT_START, 0, 0
    while d <= end:
        if d.weekday() >= 5 or d.isoformat() in done:
            d += timedelta(days=1)
            continue
        rows = fetch_cffex_day(sess, d)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO futures VALUES(?,?,?,?,?,?,?)", rows)
            conn.commit()
            n_days += 1
            n_rows += len(rows)
        d += timedelta(days=1)
    print(f"[期货] 完成：本轮新增 {n_days} 个交易日 / {n_rows} 行（{FUT_MEMBER}）")


def report(conn):
    print("\n===== 建库对账报告 =====")
    for ex in ("SH", "SZ"):
        r = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT date), COUNT(DISTINCT code),"
            f" MIN(date), MAX(date) FROM shares WHERE exch=?", (ex,)).fetchone()
        print(f"[{ex}] {r[0]} 行 / {r[1]} 个交易日 / {r[2]} 只 / {r[3]} ~ {r[4]}")
    r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) FROM kline").fetchone()
    print(f"[K线] {r[0]} 行 / {r[1]} 只 / {r[2]} ~ {r[3]}")
    r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT date), MIN(date), MAX(date) FROM futures").fetchone()
    print(f"[期货] {r[0]} 行 / {r[1]} 个交易日 / {r[2]} ~ {r[3]}")
    # 沪深每日条数最近10天
    print("\n最近10个交易日条数:")
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM shares ORDER BY date DESC LIMIT 10")]
    for dd in sorted(days):
        sh = conn.execute("SELECT COUNT(*) FROM shares WHERE date=? AND exch='SH'", (dd,)).fetchone()[0]
        sz = conn.execute("SELECT COUNT(*) FROM shares WHERE date=? AND exch='SZ'", (dd,)).fetchone()[0]
        print(f"  {dd}: 沪 {sh} / 深 {sz}")
    # 拆分检测: 单日变动率超±30%
    print("\n单日份额变动超±30%（疑似拆分/折算，需人工核对）:")
    n = 0
    for code, d1, v0, v1 in conn.execute("""
            SELECT a.code, a.date, b.shares_yi, a.shares_yi
            FROM shares a JOIN shares b
              ON a.code=b.code AND b.date=(
                  SELECT MAX(date) FROM shares c
                  WHERE c.code=a.code AND c.date<a.date)
            WHERE a.shares_yi IS NOT NULL AND b.shares_yi>0.1
              AND ABS(a.shares_yi-b.shares_yi)/b.shares_yi>0.3
            ORDER BY a.date"""):
        print(f"  {code} {d1}: {v0:.2f} -> {v1:.2f} 亿份")
        n += 1
        if n >= 50:
            print("  ...（只列前50条）")
            break
    if n == 0:
        print("  无")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sh-only", action="store_true")
    ap.add_argument("--sz-only", action="store_true")
    ap.add_argument("--kline-only", action="store_true")
    ap.add_argument("--cffex-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略已有日期，强制重采（期货专用）")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    conn = open_db()
    if args.report_only:
        report(conn)
        return
    all_run = not (args.sh_only or args.sz_only or args.kline_only or args.cffex_only)
    t0 = time.time()
    if all_run or args.sh_only:
        print(f"=== 沪市回补 {SH_START} ~ {yesterday()} ===", flush=True)
        run_sh(conn)
    if all_run or args.sz_only:
        print(f"=== 深市回补 {SZ_START} ~ {yesterday()} ===", flush=True)
        run_sz(conn)
    if all_run or args.kline_only:
        print(f"=== 东财K线回补（16只, {GJD_START}起）===", flush=True)
        run_kline(conn)
    if all_run or args.cffex_only:
        print(f"=== 中金所期货回补 {FUT_START} ~ {yesterday()} ===", flush=True)
        run_cffex(conn, force=args.force)
    report(conn)
    print(f"\n总耗时 {time.time()-t0:.0f}s, 库文件: {DB_PATH}")


if __name__ == "__main__":
    main()
