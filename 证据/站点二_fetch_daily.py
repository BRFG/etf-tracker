#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家队ETF动向追踪 - 每日数据抓取脚本

数据来源:
  份额: 上交所官方(query.sse.com.cn, 支持历史日期) + 深交所官方(fund.szse.cn, 仅当日快照)
  行情: 东方财富 push2 实时批量行情 / push2his 历史K线(回补用)

用法:
  python3 fetch_daily.py                          # 抓取最近交易日(收盘后运行)
  python3 fetch_daily.py --date 20260717          # 抓取指定日期(沪市份额+行情; 深市仅当日有效)
  python3 fetch_daily.py --backfill 20260401 20260718   # 回补历史(仅沪市份额, 深市份额无历史接口)

依赖: pip install requests openpyxl
"""
import argparse
import datetime as dt
import io
import json
import logging
import os
import re
import sys
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
HIST_PATH = os.path.join(DATA_DIR, "history.json")
OUT_PATH = os.path.join(DATA_DIR, "data.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gjd")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def load_config():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
TIMEOUT = CFG.get("request_timeout", 20)
WATCH = {e["code"]: e for e in CFG["watchlist"]}


def make_session():
    s = requests.Session()
    # trust_env=False 可绕开系统代理(国内服务器直连更稳); 若服务器必须走代理, 在config.json里改为 false
    s.trust_env = not CFG.get("ignore_system_proxy", True)
    s.headers.update({"User-Agent": UA})
    return s


def beijing_now():
    """北京时间(不依赖服务器时区)"""
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def cst_today():
    return beijing_now().date()


def get_json(sess, url, params=None, headers=None, retries=3):
    for i in range(retries):
        try:
            r = sess.get(url, params=params, headers=headers or {}, timeout=TIMEOUT)
            r.raise_for_status()
            text = r.text.strip()
            if text.startswith("(") and text.endswith(")"):
                text = text[1:-1]
            return json.loads(text)
        except Exception as e:
            log.warning("GET %s 失败(%d/%d): %s", url, i + 1, retries, e)
            time.sleep(1 + i)
    return None


# ---------------------------------------------------------------- 数据源

def fetch_sse_shares(sess, date):
    """上交所ETF份额, 按日期。返回 {code: 份额(亿份)}; 非交易日/失败返回 {}"""
    url = "https://query.sse.com.cn/commonQuery.do"
    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
        "STAT_DATE": date.strftime("%Y-%m-%d"),
    }
    headers = {"Referer": "https://www.sse.com.cn/"}
    data = get_json(sess, url, params, headers)
    out = {}
    for row in (data or {}).get("result", []):
        code = str(row.get("SEC_CODE", "")).strip()
        vol = row.get("TOT_VOL")  # 单位: 万份
        if code and vol not in (None, "", "-"):
            try:
                out[code] = float(vol) / 10000.0  # -> 亿份
            except ValueError:
                pass
    return out


def fetch_szse_shares(sess):
    """深交所ETF当前份额快照(xlsx)。返回 {code: (份额亿份, 净值或None)}"""
    url = "https://fund.szse.cn/api/report/ShowReport"
    params = {"SHOWTYPE": "xlsx", "CATALOGID": "1000_lf", "TABKEY": "tab1",
              "random": "0.383942"}
    headers = {"Referer": "https://fund.szse.cn/marketdata/fundslist/index.html"}
    try:
        r = sess.get(url, params=params, headers=headers, timeout=TIMEOUT * 2)
        r.raise_for_status()
        from openpyxl import load_workbook
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

        ci_code, ci_scale, ci_nav = col("基金代码", "代码"), col("当前规模"), col("净值")
        out = {}
        for row in rows:
            try:
                code = str(row[ci_code]).strip().split(".")[0].zfill(6)
                scale = float(str(row[ci_scale]).replace(",", ""))
                nav = None
                if ci_nav is not None and row[ci_nav] not in (None, "", "-"):
                    nav = float(str(row[ci_nav]).replace(",", ""))
                out[code] = (scale / 1e8, nav)  # 份 -> 亿份
            except (TypeError, ValueError, IndexError):
                continue
        return out
    except Exception as e:
        log.warning("深交所份额抓取失败: %s", e)
        return {}


def em_secid(code):
    return ("1." if WATCH[code]["exch"] == "SH" else "0.") + code


def fetch_em_quotes(sess, codes):
    """东财批量实时行情。返回 {code: {close, vwap, amount_yi}}"""
    fields = "f2,f5,f6,f12"
    for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
        url = f"https://{host}/api/qt/ulist.np/get"
        params = {"fltt": "2", "invt": "2", "fields": fields,
                  "secids": ",".join(em_secid(c) for c in codes)}
        data = get_json(sess, url, params, retries=2)
        diff = ((data or {}).get("data") or {}).get("diff")
        if not diff:
            continue
        if isinstance(diff, dict):
            diff = list(diff.values())
        out = {}
        for d in diff:
            code = str(d.get("f12", ""))
            close, vol, amt = d.get("f2"), d.get("f5"), d.get("f6")
            if code not in WATCH or close in (None, "-"):
                continue
            close = float(close)
            vwap = None
            amount_yi = None
            try:
                vol, amt = float(vol), float(amt)
                amount_yi = amt / 1e8
                if vol > 0:
                    vwap = amt / (vol * 100.0)  # 成交量单位:手(100份)
            except (TypeError, ValueError):
                pass
            out[code] = {"close": close, "vwap": vwap, "amount_yi": amount_yi}
        if out:
            return out
    log.warning("东财行情抓取失败")
    return {}


def fetch_em_kline(sess, code, beg, end):
    """东财历史日K(回补用)。返回 {date_str: {close, vwap, amount_yi}}"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": em_secid(code), "klt": "101", "fqt": "0",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "beg": beg.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
    }
    data = get_json(sess, url, params)
    out = {}
    for line in ((data or {}).get("data") or {}).get("klines", []):
        p = line.split(",")
        try:
            d, close, vol, amt = p[0], float(p[2]), float(p[5]), float(p[6])
            out[d] = {"close": close,
                      "vwap": amt / (vol * 100.0) if vol > 0 else None,
                      "amount_yi": amt / 1e8}
        except (ValueError, IndexError):
            continue
    return out


def fetch_cffex_member(sess, date):
    """中金所前20会员持仓排名 -> 目标会员(默认中信期货)各合约多空持仓
    CSV列: 交易日,合约,排名, 成交量会员,量,增减, 多头会员,持买,增减, 空头会员,持卖,增减
    注意: 同一行的三组排名相互独立(各自排序), 需按会员名分别匹配。
    返回 {品种: {合约: {"long":手,"long_chg":手,"short":手,"short_chg":手}}}"""
    fcfg = CFG.get("futures", {})
    member = fcfg.get("member", "中信期货")
    products = list(fcfg.get("products", {"IH": 0, "IF": 0, "IC": 0, "IM": 0}))
    name_of = lambda s: re.split(r"[(（]", s)[0].strip()
    out = {}
    for var in products:
        url = (f"http://www.cffex.com.cn/sj/ccpm/{date.strftime('%Y%m')}/"
               f"{date.strftime('%d')}/{var}_1.csv")
        text = None
        for i in range(2):
            try:
                r = sess.get(url, timeout=TIMEOUT)
                if r.status_code == 404:
                    break  # 非交易日或尚未发布
                r.raise_for_status()
                text = r.content.decode("gbk", errors="replace")
                break
            except Exception as e:
                log.warning("CFFEX %s 失败(%d/2): %s", var, i + 1, e)
                time.sleep(1)
        if not text:
            continue
        prod = {}
        for line in text.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 12 or not p[0].isdigit():
                continue  # 表头
            contract = p[1].upper()
            if not (contract.startswith(var) and contract[len(var):].isdigit()):
                continue  # 排除品种汇总行等
            c = prod.setdefault(contract, {})
            try:
                if name_of(p[6]) == member:
                    c["long"], c["long_chg"] = int(p[7]), int(p[8])
                if name_of(p[9]) == member:
                    c["short"], c["short_chg"] = int(p[10]), int(p[11])
            except ValueError:
                continue
        prod = {k: v for k, v in prod.items() if v}  # 仅保留该会员上榜的合约
        if prod:
            out[var] = prod
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------- 存储与派生

def load_hist():
    if os.path.exists(HIST_PATH):
        with open(HIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"records": {}}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def record_day(hist, date_str, sse, szse, quotes, futures=None):
    """把某交易日各来源数据合并进 history.json 的 records[date]"""
    rec = hist["records"].setdefault(date_str, {})
    if futures:
        rec["_fut"] = futures
    n = 0
    for code, meta in WATCH.items():
        row = rec.setdefault(code, {})
        if meta["exch"] == "SH" and code in sse:
            row["shares_yi"] = round(sse[code], 4)
            row["src"] = "sse"
        elif meta["exch"] == "SZ" and code in szse:
            row["shares_yi"] = round(szse[code][0], 4)
            if szse[code][1]:
                row["nav"] = szse[code][1]
            row["src"] = "szse"
        q = quotes.get(code)
        if q:
            for k in ("close", "vwap", "amount_yi"):
                if q.get(k) is not None:
                    row[k] = round(q[k], 4)
        if "shares_yi" in row:
            n += 1
    return n


def rebuild_output(hist, sample=False):
    """从 history.json 派生前端用 data.json (含估算净流入)"""
    dates = sorted(d for d, rec in hist["records"].items()
                   if any("shares_yi" in r for r in rec.values()))
    series = {}
    for code, meta in WATCH.items():
        shares, flow, close = [], [], []
        prev = None
        for d in dates:
            row = hist["records"][d].get(code, {})
            s = row.get("shares_yi")
            px = row.get("vwap") or row.get("close") or row.get("nav")
            c = row.get("close") or row.get("nav")
            shares.append(round(s, 4) if s is not None else None)
            close.append(round(c, 4) if c is not None else None)
            f = None
            if s is not None and prev is not None and px:
                f = round((s - prev) * px, 4)  # 亿份*元 = 亿元
            flow.append(f)
            if s is not None:
                prev = s
        series[code] = {"shares": shares, "flow": flow, "close": close}

    # ---- 期货: 独立时间轴(期货16:30发布, ETF份额晚间发布, 两者解耦)
    fcfg = CFG.get("futures", {})
    products = list(fcfg.get("products", {}))
    fut_dates = sorted(d for d, rec in hist["records"].items() if rec.get("_fut"))
    fut_series = {p: {"long": [], "short": [], "net_short": []} for p in products}
    fut_latest = {"date": None, "contracts": {}}
    for d in fut_dates:
        fut = hist["records"][d]["_fut"]
        for p in products:
            cons = fut.get(p) or {}
            if cons:
                lo = sum(c.get("long", 0) for c in cons.values())
                sh = sum(c.get("short", 0) for c in cons.values())
                fut_series[p]["long"].append(lo)
                fut_series[p]["short"].append(sh)
                fut_series[p]["net_short"].append(sh - lo)
            else:
                for k in ("long", "short", "net_short"):
                    fut_series[p][k].append(None)
        fut_latest = {"date": d, "contracts": {
            p: sorted([[k, v.get("long", 0), v.get("long_chg", 0),
                        v.get("short", 0), v.get("short_chg", 0)]
                       for k, v in fut[p].items()])
            for p in products if fut.get(p)}}

    out = {
        "sample": sample,
        "updated": (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M") + " (北京时间)",
        "baseline": CFG["baseline"],
        "etfs": CFG["watchlist"],
        "dates": dates,
        "series": series,
        "futures": {"member": fcfg.get("member", "中信期货"), "products": products,
                    "names": fcfg.get("products", {}), "dates": fut_dates,
                    "series": fut_series, "latest": fut_latest},
    }
    save_json(OUT_PATH, out)
    return out


# ---------------------------------------------------------------- 主流程

def run_daily(target=None):
    sess = make_session()
    hist = load_hist()
    now = beijing_now()
    today = now.date()
    changed = False

    # ---- 期货(中金所约16:30发布): 与ETF份额解耦, 先按目标日直接抓取
    fut_date = target or today
    fut_today = fetch_cffex_member(sess, fut_date)
    if fut_today:
        hist["records"].setdefault(fut_date.strftime("%Y-%m-%d"), {})["_fut"] = fut_today
        changed = True
        log.info("中金所 %s %s: %s", fut_date,
                 CFG.get("futures", {}).get("member", ""),
                 {p: len(c) for p, c in fut_today.items()})

    # ---- ETF份额: 以上交所发布定位记录日(当日数据晚间约23:00前发布)
    date = target or today
    sse = {}
    for _ in range(6):
        sse = fetch_sse_shares(sess, date)
        if sse:
            break
        log.info("%s 上交所无数据(未发布或非交易日), 尝试前一日", date)
        date -= dt.timedelta(days=1)
    if not sse:
        log.error("连续6天无上交所份额数据")
        if changed:
            save_json(HIST_PATH, hist)
            rebuild_output(hist)
        sys.exit(1)
    date_str = date.strftime("%Y-%m-%d")
    log.info("ETF记录日 %s, 上交所返回 %d 只", date_str, len(sse))

    # 回退日已有记录则跳过ETF部分——防止把"当前"的深市快照/实时行情错写进旧日期
    skip_etf = False
    if target is None and date != today:
        existing = hist["records"].get(date_str, {})
        if any(isinstance(v, dict) and "shares_yi" in v for v in existing.values()):
            log.info("%s ETF已记录, 上交所尚未发布新数据(当日份额请23:00后抓取)", date_str)
            skip_etf = True

    if not skip_etf:
        # 深交所快照无日期参数, 仅在快照确定对应记录日时使用:
        #   晚间(>=18点): 快照=当日收盘  /  早间(<15点): 快照=上一交易日收盘
        szse = {}
        if (date == today and now.hour >= 18) or (date < today and now.hour < 15):
            szse = fetch_szse_shares(sess)
            if szse:
                log.info("深交所快照 %d 只", len(szse))
        elif date == today:
            log.info("18:00前深交所快照未必已更新为当日, 跳过深市份额(晚间运行自动补)")

        quotes = {}
        if date == today:
            quotes = fetch_em_quotes(sess, list(WATCH))
        if not quotes:  # 补录历史日或实时接口失败: 用该日K线, 避免行情错位
            for code in WATCH:
                k = fetch_em_kline(sess, code, date, date).get(date_str)
                if k:
                    quotes[code] = k
                time.sleep(0.3)

        # ETF记录日的期货数据若缺失一并补上(如次日早间补跑昨日的场景)
        fut_for_date = fut_today if date == fut_date else None
        if not fut_for_date and not hist["records"].get(date_str, {}).get("_fut"):
            fut_for_date = fetch_cffex_member(sess, date)

        n = record_day(hist, date_str, sse, szse, quotes, fut_for_date)
        changed = True
        log.info("记录 %s: %d/%d 只ETF", date_str, n, len(WATCH))

    if changed:
        save_json(HIST_PATH, hist)
        rebuild_output(hist)
        log.info("data/data.json 已更新")
    else:
        log.info("本次无新数据")


def run_backfill(beg, end):
    sess = make_session()
    hist = load_hist()
    # 行情: 每只ETF拉一次区间K线
    klines = {}
    for code in WATCH:
        klines[code] = fetch_em_kline(sess, code, beg, end)
        log.info("K线 %s: %d 天", code, len(klines[code]))
        time.sleep(0.4)
    # 份额: 沪市按日回补; 深市无历史接口, 跳过(从每日运行起积累)
    d = beg
    while d <= end:
        if d.weekday() < 5:
            sse = fetch_sse_shares(sess, d)
            if sse:
                ds = d.strftime("%Y-%m-%d")
                quotes = {c: klines[c][ds] for c in WATCH if ds in klines[c]}
                futures = fetch_cffex_member(sess, d)
                n = record_day(hist, ds, sse, {}, quotes, futures)
                log.info("%s: ETF %d 只, 期货 %d 品种", ds, n, len(futures))
            time.sleep(0.6)
        d += dt.timedelta(days=1)
    save_json(HIST_PATH, hist)
    rebuild_output(hist)
    log.info("回补完成 -> data/data.json")


def main():
    ap = argparse.ArgumentParser(description="国家队ETF动向追踪-数据抓取")
    ap.add_argument("--date", help="指定日期 YYYYMMDD")
    ap.add_argument("--backfill", nargs=2, metavar=("BEG", "END"),
                    help="回补区间 YYYYMMDD YYYYMMDD (仅沪市份额)")
    args = ap.parse_args()
    if args.backfill:
        beg = dt.datetime.strptime(args.backfill[0], "%Y%m%d").date()
        end = dt.datetime.strptime(args.backfill[1], "%Y%m%d").date()
        run_backfill(beg, end)
    else:
        target = dt.datetime.strptime(args.date, "%Y%m%d").date() if args.date else None
        run_daily(target)


if __name__ == "__main__":
    main()
