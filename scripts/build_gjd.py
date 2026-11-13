#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家队宽基ETF动向追踪 —— 网页生成器 (v2)
输入: 数据库/etf.db + 证据/站点二_config.json
输出: 网页/国家队站/ (index.html[内联数据] + echarts.min.js)

v2 改版（对齐原站 etf.maomaoci-a.com 的信息层级）:
  1. 顶部 5 张 KPI 卡：当日估算净流入 / 近5日 / 近20日 / 监控组合规模 / 中信期货净空头变动
  2. 「每日估算净申赎（监控组合合计）」合计图
  3. 「ETF 资金异动总览」快照表（含当日/5日/20日净流入分位，样本 2024-01 起）+ 全列可排序
  4. 期限按钮（1年/全部）移到 一、与 二、 之间
  5. 「总份额与净流入」按跟踪指数分组，每组一个下拉：合计 / 单只 ETF
  6. 一图一行（不再 2 列并排）
  7. 修复：官方持仓基准区块被重复插入
  8. 修复：期货明细 null（同一合约的多空会员排名分散在不同行，旧解析被覆盖）
"""
import json
import os
import shutil
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "数据库", "etf.db")
OUT = os.path.join(ROOT, "网页", "国家队站")
CFG = json.load(open(os.path.join(ROOT, "证据", "站点二_config.json"),
                     encoding="utf-8"))
WATCH = CFG["watchlist"]

START = "2024-01-02"   # 展示起点。份额库最早 2016-12-01，K 线自 2023-12-01，
                       # 多取 2023-12-29 一天作首日 flow 的基准点
W5, W20 = 5, 20


def pct_rank(latest, hist):
    valid = [x for x in hist if x is not None]
    if latest is None or len(valid) < 30:
        return None
    return round(100.0 * sum(1 for x in valid if x < latest) / len(valid), 1)


def rnd(x, d=4):
    return round(x, d) if x is not None else None


def main():
    conn = sqlite3.connect(DB)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM shares WHERE exch='SH' ORDER BY date")]
    dates = [d for d in dates if d >= "2023-12-29"]
    didx = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    disp = [i for i, d in enumerate(dates) if d >= START]

    # ---- 份额 & 价格 ----
    shares = {e["code"]: [None] * n for e in WATCH}
    for code, date, v in conn.execute(
            "SELECT code, date, shares_yi FROM shares WHERE date>=?", (dates[0],)):
        if code in shares and date in didx:
            shares[code][didx[date]] = v
    kline = {e["code"]: {} for e in WATCH}
    for code, date, close, vwap in conn.execute(
            "SELECT code, date, close, vwap FROM kline"):
        if code in kline:
            kline[code][date] = (close, vwap)

    # ---- series ----
    series, flow_stats = {}, {}
    for e in WATCH:
        code = e["code"]
        sh, fl, cl = [], [], []
        prev = None
        for i in range(n):
            s = shares[code][i]
            c, vw = kline[code].get(dates[i], (None, None))
            px = vw if vw else c
            f = None
            if s is not None and prev is not None and px:
                f = round((s - prev) * px, 2)
            cl.append(c)
            sh.append(rnd(s))
            fl.append(f)
            if s is not None:
                prev = s

        def win_sum(arr, w):
            out = [None] * n
            for i in range(w - 1, n):
                seg = arr[i - w + 1: i + 1]
                if all(x is not None for x in seg):
                    out[i] = round(sum(seg), 2)
            return out

        fl5, fl20 = win_sum(fl, W5), win_sum(fl, W20)
        series[code] = {
            "shares": sh, "flow": fl, "close": cl, "flow5": fl5, "flow20": fl20,
        }
        flow_stats[code] = {
            "flow_last": fl[-1], "flow_pct": pct_rank(fl[-1], fl),
            "flow5_last": fl5[-1], "flow5_pct": pct_rank(fl5[-1], fl5),
            "flow20_last": fl20[-1], "flow20_pct": pct_rank(fl20[-1], fl20),
            "close_last": cl[-1],
        }

    # ---- 组合合计（严格口径：任一只缺份额/缺价则该日置 None，绝不静默少算）----
    agg_flow, agg_value, agg_cov, gaps = [], [], [], []
    for i in range(n):
        v = 0.0
        cov = 0
        miss = []
        for e in WATCH:
            s_ = series[e["code"]]["shares"][i]
            c_ = series[e["code"]]["close"][i]
            if s_ is not None and c_:
                v += s_ * c_
                cov += 1
            else:
                miss.append(e["code"])
        agg_value.append(round(v, 1) if cov == len(WATCH) else None)
        agg_cov.append(cov)
        # flow 需要前一日份额：首日（i=0）天然无 flow，不算缺口
        fl_full = (i > 0 and cov == len(WATCH)
                   and all(series[e["code"]]["flow"][i] is not None for e in WATCH))
        agg_flow.append(round(sum(series[e["code"]]["flow"][i] for e in WATCH), 2)
                        if fl_full else None)
        if i > 0 and not fl_full:
            gaps.append((dates[i], cov, miss))

    print(f"[合计] {n - 1 - len(gaps)}/{n - 1} 个交易日 {len(WATCH)} 只齐全（首日仅作基准）")
    if gaps:
        print(f"[合计] ⚠️ {len(gaps)} 个交易日数据不全，合计已置空（图中断点）：")
        for d, cov, miss in gaps[:10]:
            print(f"    {d} 覆盖 {cov}/{len(WATCH)}，缺 {','.join(miss)}")
        if len(gaps) > 10:
            print(f"    …… 另有 {len(gaps) - 10} 天")

    # ---- 期货 ----
    fut_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM futures ORDER BY date")]
    products = list(CFG["futures"]["products"])
    fut_series = {p: {"long": [], "short": [], "net_short": []} for p in products}
    net_total = []
    for d in fut_dates:
        tot = 0
        has = False
        for p in products:
            r = conn.execute(
                "SELECT SUM(long), SUM(short) FROM futures WHERE date=? AND product=?",
                (d, p)).fetchone()
            lo, sho = r
            if lo is None and sho is None:
                for k in ("long", "short", "net_short"):
                    fut_series[p][k].append(None)
            else:
                lo, sho = lo or 0, sho or 0
                fut_series[p]["long"].append(lo)
                fut_series[p]["short"].append(sho)
                fut_series[p]["net_short"].append(sho - lo)
                tot += sho - lo
                has = True
        net_total.append(tot if has else None)

    fut_latest = {"date": fut_dates[-1], "contracts": {}}
    for p in products:
        rows = conn.execute(
            "SELECT contract, long, long_chg, short, short_chg FROM futures"
            " WHERE date=? AND product=? ORDER BY contract",
            (fut_dates[-1], p)).fetchall()
        if rows:
            fut_latest["contracts"][p] = [list(r) for r in rows]

    valid_net = [x for x in net_total if x is not None]
    net_chg = (valid_net[-1] - valid_net[-2]
               if len(valid_net) >= 2 else None)

    # ---- KPI（最新快照，不受期限影响）----
    kpi = {
        "date": dates[-1],
        "flow1": agg_flow[-1],
        "flow1_pct": pct_rank(agg_flow[-1], agg_flow),
        "flow5": round(sum(x for x in agg_flow[-W5:] if x is not None), 2)
        if all(x is not None for x in agg_flow[-W5:]) else None,
        "flow20": round(sum(x for x in agg_flow[-W20:] if x is not None), 2)
        if all(x is not None for x in agg_flow[-W20:]) else None,
        "value": agg_value[-1],
        "fut_net": valid_net[-1] if valid_net else None,
        "fut_net_chg": net_chg,
        "fut_date": fut_dates[-1],
        "fut_member": CFG["futures"]["member"],
    }
    kpi["flow5_pct"] = pct_rank(kpi["flow5"],
                                [sum(agg_flow[i - W5 + 1:i + 1])
                                 if all(x is not None for x in agg_flow[i - W5 + 1:i + 1])
                                 else None for i in range(W5 - 1, n)])

    data = {
        "updated": dates[-1],
        "baseline": CFG["baseline"],
        "etfs": WATCH,
        "dates": dates,
        "start_index": disp[0],
        "series": series,
        "flow_stats": flow_stats,
        "agg": {"flow": agg_flow, "value": agg_value, "cov": agg_cov},
        "kpi": kpi,
        "futures": {
            "member": kpi["fut_member"], "products": products,
            "names": CFG["futures"]["products"], "dates": fut_dates,
            "series": fut_series, "net_total": net_total, "latest": fut_latest,
        },
    }
    # 干净检出的仓库里没有 网页/国家队站/（页面不纳入版本库，由 CI 现场生成），
    # 必须先建目录，否则 shutil.copy 会 FileNotFoundError。
    os.makedirs(OUT, exist_ok=True)
    src = os.path.join(ROOT, "网页", "echarts.min.js")
    if os.path.exists(src):
        shutil.copy(src, os.path.join(OUT, "echarts.min.js"))

    html = render_html()
    # 数据内联，保证双击 index.html（file://）也能打开，不依赖 fetch
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = html.replace("<!--DATAJSON-->",
                        '<script type="application/json" id="data-json">'
                        + blob + "</script>")
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"KPI: 当日{kpi['flow1']} 5日{kpi['flow5']} 20日{kpi['flow20']} "
          f"规模{kpi['value']} 期货净空头{kpi['fut_net']}({kpi['fut_net_chg']})")
    print(f"生成完成 -> {OUT}")


def render_html():
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>国家队宽基ETF动向追踪</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--border:#d0d7de;--muted:#57606a;--text:#1f2328;--accent:#1f6feb;--warn:#d4a72c;
--red:#cf222e;--green:#1a7f37;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font:14px/1.7 "Microsoft YaHei","PingFang SC",sans-serif;padding:24px;max-width:1280px;margin:0 auto;}
h1{font-size:22px;} h2{font-size:17px;margin:0 0 10px;}
h2 small{font-weight:400;color:var(--muted);font-size:12px;margin-left:8px;}
.sub{color:var(--muted);font-size:13px;margin:4px 0 12px;}
.notice{background:#fff8c5;border:1px solid var(--warn);border-radius:8px;padding:8px 12px;font-size:12.5px;margin-bottom:14px;}
/* KPI */
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:14px 0 4px;}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;}
.kpi .k{font-size:12px;color:var(--muted);}
.kpi .v{font-size:20px;font-weight:700;margin-top:2px;letter-spacing:-0.3px;}
.kpi .s{font-size:11.5px;color:var(--muted);margin-top:1px;}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr);}}
/* 面板 */
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin:12px 0;}
.chart{width:100%;height:320px;}
.chart.big{height:360px;}
.chart.tall{height:420px;}
/* 表格 */
.tblwrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:auto;margin-bottom:10px;}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:1080px;}
th,td{padding:6px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap;}
#tbl td.gw{width:230px;white-space:normal;line-height:1.5;font-size:12px;}
th{background:#f0f2f5;position:sticky;top:0;z-index:1;}
th.l,td.l{text-align:left;}
th.sortable{cursor:pointer;user-select:none;}
th.sortable:hover{background:#e6e9ee;}
th .ar{color:#9aa4af;font-size:10px;margin-left:3px;}
th.sorted .ar{color:var(--accent);}
tbody tr:hover{background:#f6f9ff;}
.up{color:var(--red);font-weight:600;} .down{color:var(--green);font-weight:600;}
.pct-badge{display:inline-block;padding:0 6px;border-radius:8px;font-size:11.5px;font-weight:600;}
.pct-hi{background:#ffebe9;color:var(--red);} .pct-lo{background:#dafbe1;color:var(--green);}
.pct-mid{background:#f0f2f5;color:var(--muted);}
/* 期限按钮 */
.ranges{display:flex;align-items:center;gap:8px;margin:14px 0 4px;}
.ranges .lb{font-size:12.5px;color:var(--muted);}
.ranges button{cursor:pointer;background:var(--card);border:1px solid var(--border);border-radius:6px;font:600 12.5px/1.4 "Microsoft YaHei",sans-serif;padding:4px 14px;color:var(--muted);}
.ranges button.active{background:#218873;border-color:#218873;color:#fff;}
/* 分组图 */
.grp-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;}
.grp-head .t{font-weight:700;font-size:15px;border-left:4px solid var(--accent);padding-left:8px;}
select{font:13px "Microsoft YaHei",sans-serif;padding:3px 8px;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--text);cursor:pointer;}
.muted{color:var(--muted);font-size:12.5px;}
.note{color:var(--muted);font-size:12px;margin-top:6px;line-height:1.6;}
footer{color:var(--muted);font-size:12px;margin-top:24px;border-top:1px solid var(--border);padding-top:10px;}
</style></head><body>
<h1>国家队宽基ETF动向追踪</h1>
<div class="sub" id="sub">加载中…</div>
<div class="notice">⚠️ flow = 份额变动 × 当日成交均价(VWAP)，是资金流的近似而非精确值；份额变动不能归因于特定持有人。汇金持仓明细自 2026 年一季报起已低于 20% 披露线，国家队持仓基准固定为 2025 年年报。</div>

<div class="kpis" id="kpis"></div>

<div class="panel">
<h2>每日估算净申赎（监控组合合计）<small>柱：单日净流入(亿元)　线：区间累计(亿元)</small></h2>
<div class="chart big" id="chartTotal"></div>
</div>

<h2>一、ETF 资金异动总览（含当日净流入分位）</h2>
<div class="sub" style="margin:0 0 6px">点击表头排序。分位 = 该 ETF 在自身单日净流入中的百分位：≥95%（红框）= 大额流入，≤5%（绿框）= 大额流出。</div>
<div class="sub" style="margin:0 0 6px">分位样本为 <b>2024-01 以来</b>（2024-01-02 ~ 最新，约 690 个交易日）。汇金持仓量级近年持续扩大，不向前延伸样本，以免系统性低估近期分位。</div>
<div class="sub" style="margin:0 0 6px">「备注」列 = 2025 年年报披露的期末前十名持有人中 <b>汇金投资 + 汇金资管</b> 两户合计份额 / 占总份额比例 / 按 2025-12-31 收盘价估算的市值。</div>
<div class="tblwrap"><table id="tbl">
<thead><tr>
<th class="l sortable" data-k="code">代码<span class="ar">↕</span></th>
<th class="l sortable" data-k="name">名称<span class="ar">↕</span></th>
<th class="l sortable" data-k="index">跟踪指数<span class="ar">↕</span></th>
<th class="sortable" data-k="shares">最新份额<br>(亿份)<span class="ar">↕</span></th>
<th class="sortable" data-k="dShares">当日变动<br>(亿份)<span class="ar">↕</span></th>
<th class="sortable" data-k="flow1">当日净流入<br>(亿元)<span class="ar">↕</span></th>
<th class="sortable" data-k="flow1p">当日<br>分位<span class="ar">↕</span></th>
<th class="sortable" data-k="flow5">近5日<br>(亿元)<span class="ar">↕</span></th>
<th class="sortable" data-k="flow5p">近5日<br>分位<span class="ar">↕</span></th>
<th class="sortable" data-k="flow20">近20日<br>(亿元)<span class="ar">↕</span></th>
<th class="sortable" data-k="flow20p">近20日<br>分位<span class="ar">↕</span></th>
<th class="sortable" data-k="close">最新价<span class="ar">↕</span></th>
<th class="l">国家队持仓备注</th>
</tr></thead><tbody></tbody></table></div>

<div class="ranges" id="ranges">
<span class="lb">期限：</span><button data-r="1">1年（短线）</button><button data-r="0" class="active">全部（2024-01起）</button>
<span class="lb" id="rangeNote"></span>
</div>

<h2 style="margin-top:16px">二、总份额与净流入（按跟踪指数分组，可选合计或单只）</h2>
<div id="groups"></div>

<h2 style="margin-top:20px">三、股指期货席位（中信期货 · 中金所前20会员口径）</h2>
<div class="panel">
<h2 style="font-size:14px;font-weight:600">四品种净空头持仓（手）<small>净空头 = 持卖单 − 持买单；四品种合计</small></h2>
<div class="chart big" id="chart_fut"></div>
</div>
<div class="panel">
<h2 style="font-size:14px;font-weight:600" id="fut-latest-t">最新合约明细</h2>
<div style="overflow:auto;max-height:420px"><table id="futTbl" style="min-width:760px">
<thead><tr><th class="l">品种</th><th class="l">合约</th><th>多头持仓(手)</th><th>多头增减</th>
<th>空头持仓(手)</th><th>空头增减</th><th>净空头(手)</th><th>净空头变动</th></tr></thead>
<tbody></tbody></table></div>
<p class="note">「—」表示中信期货当日未进入该合约相应方向的前 20 名（中金所仅披露前 20 会员），并非 0 持仓。</p>
</div>

<h2 style="margin-top:20px">四、国家队官方持仓基准（2025年年报）</h2>
<p class="muted" id="base-asof"></p>
<div class="tblwrap"><table id="tbl-base" style="min-width:640px">
<thead><tr><th class="l">机构</th><th>持有ETF数</th><th>持仓份额(亿份)</th><th>持仓市值(亿元)</th><th>平均持仓占比</th></tr></thead>
<tbody></tbody></table></div>
<p class="note" id="base-note"></p>
<footer>数据来源：上交所/深交所公开披露（份额）、东方财富（行情）、中金所（席位持仓）。红=流入 绿=流出（国内惯例）。本页仅供研究，不构成投资建议。</footer>
<script src="echarts.min.js"></script>
<!--DATAJSON-->
<script>
var DATA=null, RANGE=0, SORTK=null, SORTD=-1, GROUPS={};
try{
  DATA=JSON.parse(document.getElementById('data-json').textContent);
  buildKPI(); buildTable(); buildGroups(); bindRange(); renderAll();
}catch(e){
  document.body.insertAdjacentHTML('afterbegin','<div class="notice">数据加载失败: '+e+'</div>');
}
function fmt(v,d){ if(v===null||v===undefined||isNaN(v))return '—'; return v.toFixed(d===undefined?2:d); }
function sgn(v,d){ if(v===null||v===undefined||isNaN(v))return '—'; return (v>0?'+':'')+v.toFixed(d===undefined?2:d); }
function cls(v){ return v>0?'up':(v<0?'down':''); }
function pctBadge(p){
  if(p===null||p===undefined) return '<span class="pct-badge pct-mid">—</span>';
  var c=p>=95?'pct-hi':(p<=5?'pct-lo':'pct-mid');
  return '<span class="pct-badge '+c+'">'+p.toFixed(0)+'%</span>';
}
/* ---------- 动态坐标范围：按可见数据 + 8% 留白 ---------- */
function bounds(arrays, includeZero){
  var lo=Infinity, hi=-Infinity;
  arrays.forEach(function(a){ a.forEach(function(v){
    if(v===null||v===undefined||isNaN(v)) return;
    if(v<lo) lo=v; if(v>hi) hi=v;
  });});
  if(lo===Infinity) return {};
  var rawLo=lo;
  if(includeZero){ if(lo>0) lo=0; if(hi<0) hi=0; }
  if(hi===lo){ hi=lo+1; lo=lo-1; }
  var pad=(hi-lo)*0.08, a=lo-pad, b=hi+pad;
  if(rawLo>=0 && a<0) a=0;
  return niceRange(a,b);
}
/* ---------- 长期图：底部范围条(slider) + 鼠标滚轮缩放(inside) ---------- */
function zoomOpt(dates){
  /* inside=滚轮缩放/拖动平移；slider=底部范围条，手柄上显示当前区间起止月 */
  return [{type:'inside'},{type:'slider',height:18,bottom:8,showDetail:true,
    labelFormatter:function(v){var d=dates[Math.round(v)];return d?d.slice(0,7):'';}}];
}
/* 取当前缩放后可见的索引区间 */
function zoomIdx(ch,n){
  var o=ch.getOption().dataZoom; if(!o||!o.length) return null;
  var st=o[0].start==null?0:o[0].start, en=o[0].end==null?100:o[0].end;
  var a=Math.max(0,Math.floor(st/100*(n-1))), b=Math.min(n-1,Math.ceil(en/100*(n-1)));
  return b>a ? [a,b] : null;
}
/* 把上下限收敛到整齐刻度，避免出现 4,818.080556 这类长坐标标签 */
function niceRange(lo,hi){
  if(!isFinite(lo)||!isFinite(hi)) return {};
  var span=hi-lo; if(span<=0) span=Math.abs(hi)||1;
  var raw=span/4, step=Math.pow(10,Math.floor(Math.log10(raw))), m=raw/step;
  if(m>=5) step*=5; else if(m>=2) step*=2;
  return {min:Math.floor(lo/step)*step, max:Math.ceil(hi/step)*step};
}
function sliceIdx(){
  if(!RANGE) return DATA.start_index;
  var last=DATA.dates[DATA.dates.length-1];
  var cut=(parseInt(last.slice(0,4))-RANGE)+last.slice(4);
  for(var i=0;i<DATA.dates.length;i++) if(DATA.dates[i]>=cut) return i;
  return DATA.start_index;
}
/* ---------- KPI ---------- */
function buildKPI(){
  var k=DATA.kpi, el=document.getElementById('kpis');
  var items=[
    {k:'当日估算净流入（亿元）', v:sgn(k.flow1), c:cls(k.flow1),
     s:(k.flow1_pct!==null?'2024 以来 '+k.flow1_pct.toFixed(0)+'% 分位':'—')+' · '+k.date},
    {k:'近5日累计（亿元）', v:sgn(k.flow5), c:cls(k.flow5),
     s:k.flow5_pct!==null?'2024 以来 '+k.flow5_pct.toFixed(0)+'% 分位':'—'},
    {k:'近20日累计（亿元）', v:sgn(k.flow20), c:cls(k.flow20), s:DATA.etfs.length+' 只合计'},
    {k:'监控组合规模（亿元）', v:fmt(k.value,0), c:'', s:'合计份额 × 最新价'},
    {k:'中信期货净空头变动（手）', v:sgn(k.fut_net_chg,0),
     c:cls(k.fut_net_chg),
     s:'净空头 '+fmt(k.fut_net,0)+' 手 · '+k.fut_date}
  ];
  el.innerHTML=items.map(function(it){
    return '<div class="kpi"><div class="k">'+it.k+'</div>'
      +'<div class="v '+it.c+'">'+it.v+'</div><div class="s">'+it.s+'</div></div>';
  }).join('');
}
/* ---------- 快照表 ---------- */
function buildTable(){
  var n=DATA.dates.length-1, rows=[];
  DATA.etfs.forEach(function(e){
    var s=DATA.series[e.code], st=DATA.flow_stats[e.code];
    var shd=(s.shares[n]!==null&&s.shares[n-1]!==null)?s.shares[n]-s.shares[n-1]:null;
    rows.push({code:e.code,name:e.name,index:e.index,note:e.gjd_note||'',
      shares:s.shares[n], dShares:shd,
      flow1:st.flow_last, flow1p:st.flow_pct,
      flow5:st.flow5_last, flow5p:st.flow5_pct,
      flow20:st.flow20_last, flow20p:st.flow20_pct,
      close:st.close_last});
  });
  if(SORTK){
    rows.sort(function(a,b){
      var x=a[SORTK], y=b[SORTK];
      if(typeof x==='string'||typeof y==='string'){
        x=x||''; y=y||''; return SORTD*(x<y?-1:x>y?1:0);
      }
      var xn=(x===null||x===undefined), yn=(y===null||y===undefined);
      if(xn&&yn) return 0; if(xn) return 1; if(yn) return -1;
      return SORTD*(x-y);
    });
  }
  var tb=document.querySelector('#tbl tbody');
  tb.innerHTML=rows.map(function(r){
    return '<tr>'
      +'<td class="l">'+r.code+'</td><td class="l">'+r.name+'</td><td class="l">'+r.index+'</td>'
      +'<td>'+fmt(r.shares)+'</td>'
      +'<td class="'+cls(r.dShares)+'">'+sgn(r.dShares)+'</td>'
      +'<td class="'+cls(r.flow1)+'">'+sgn(r.flow1)+'</td><td>'+pctBadge(r.flow1p)+'</td>'
      +'<td class="'+cls(r.flow5)+'">'+sgn(r.flow5)+'</td><td>'+pctBadge(r.flow5p)+'</td>'
      +'<td class="'+cls(r.flow20)+'">'+sgn(r.flow20)+'</td><td>'+pctBadge(r.flow20p)+'</td>'
      +'<td>'+fmt(r.close,3)+'</td>'
      +'<td class="l muted gw">'+r.note+'</td></tr>';
  }).join('');
  document.querySelectorAll('#tbl th.sortable').forEach(function(th){
    th.classList.toggle('sorted', th.dataset.k===SORTK);
    th.querySelector('.ar').textContent = th.dataset.k===SORTK ? (SORTD>0?'↑':'↓') : '↕';
    th.onclick=function(){
      if(SORTK===th.dataset.k) SORTD=-SORTD; else {SORTK=th.dataset.k; SORTD=-1;}
      buildTable();
    };
  });
}
/* ---------- 按指数分组：合计 / 单只 ---------- */
function buildGroups(){
  var groups={}, order=[];
  DATA.etfs.forEach(function(e){
    if(!groups[e.index]){groups[e.index]=[]; order.push(e.index);}
    groups[e.index].push(e);
  });
  var host=document.getElementById('groups'); host.innerHTML='';
  order.forEach(function(ix){
    var list=groups[ix];
    var opt='<option value="__all__">合计（'+list.length+' 只）</option>'
      +list.map(function(e){return '<option value="'+e.code+'">'+e.code+' '+e.name+'</option>';}).join('');
    var d=document.createElement('div'); d.className='panel';
    d.innerHTML='<div class="grp-head"><span class="t">'+ix+'</span>'
      +'<select data-ix="'+ix+'">'+opt+'</select>'
      +'<small class="muted">线：份额(亿份，左轴)　柱：当日估算净流入(亿元，右轴)</small></div>'
      +'<div class="chart tall" id="g_'+ix+'"></div>';
    host.appendChild(d);
    var sel=d.querySelector('select');
    sel.onchange=function(){ renderGroup(ix, sel.value); };
    GROUPS[ix]={list:list, el:null};
  });
}
function groupSeries(ix, code){
  var g=GROUPS[ix], i0=sliceIdx(), dates=DATA.dates.slice(i0);
  if(code==='__all__'){
    var sh=[], fl=[];
    for(var i=i0;i<DATA.dates.length;i++){
      var s=0, f=0, okS=false, okF=false;
      g.list.forEach(function(e){
        var v=DATA.series[e.code].shares[i]; if(v!==null&&v!==undefined){s+=v;okS=true;}
        var x=DATA.series[e.code].flow[i]; if(x!==null&&x!==undefined){f+=x;okF=true;}
      });
      sh.push(okS?Math.round(s*1000)/1000:null);
      fl.push(okF?Math.round(f*100)/100:null);
    }
    return {dates:dates, shares:sh, flow:fl, name:'合计'};
  }
  var s=DATA.series[code];
  return {dates:dates, shares:s.shares.slice(i0), flow:s.flow.slice(i0),
          name:(DATA.etfs.filter(function(e){return e.code===code;})[0]||{}).name||code};
}
function renderGroup(ix, code){
  var el=document.getElementById('g_'+ix); if(!el) return;
  var ch=GROUPS[ix].el;
  if(!ch){ ch=echarts.init(el); GROUPS[ix].el=ch; }
  var d=groupSeries(ix, code);
  var b1=bounds([d.shares], false), b2=bounds([d.flow], true);
  ch.setOption({animation:false,
    tooltip:{trigger:'axis',confine:true,
      formatter:function(ps){
        var h='<b>'+ps[0].axisValue+'</b><br>'+d.name+'<br>';
        ps.forEach(function(p){
          var u=p.seriesName.indexOf('份额')>=0?' 亿份':' 亿元';
          h+=p.marker+' '+p.seriesName+': <b>'+(p.value===null||p.value===undefined?'—':p.value)+u+'</b><br>';
        });
        return h;
      }},
    legend:{top:0,textStyle:{fontSize:11}},
    grid:{left:62,right:62,top:34,bottom:72},
    dataZoom:zoomOpt(d.dates),
    xAxis:{type:'category',data:d.dates,axisLabel:{fontSize:9,
      formatter:function(v){return d.dates.length>400?v.slice(0,7):v.slice(2);}}},
    yAxis:[{type:'value',name:'份额(亿份)',nameTextStyle:{fontSize:10},min:b1.min,max:b1.max,axisLabel:{fontSize:9}},
           {type:'value',name:'净流入(亿元)',nameTextStyle:{fontSize:10},min:b2.min,max:b2.max,axisLabel:{fontSize:9},splitLine:{show:false}}],
    series:[
      {name:'当日净流入',type:'bar',yAxisIndex:1,data:d.flow,barMaxWidth:14,
       itemStyle:{color:function(p){return p.value>=0?'rgba(207,34,46,.55)':'rgba(26,127,55,.55)';}}},
      {name:'份额',type:'line',yAxisIndex:0,data:d.shares,showSymbol:false,
       lineStyle:{width:1.8,color:'#1f6feb'},itemStyle:{color:'#1f6feb'},z:5}
    ]}, true);
  ch.off('dataZoom');
  ch.on('dataZoom',function(){
    if(ch.__zt) clearTimeout(ch.__zt);
    ch.__zt=setTimeout(function(){
      var r=zoomIdx(ch,d.dates.length); if(!r) return;
      var y1=bounds([d.shares.slice(r[0],r[1]+1)],false),
          y2=bounds([d.flow.slice(r[0],r[1]+1)],true);
      ch.setOption({yAxis:[{min:y1.min,max:y1.max},{min:y2.min,max:y2.max}]});
    },90);
  });
}
/* ---------- 合计净申赎 ---------- */
var C_TOTAL=null;
function renderTotal(){
  var i0=sliceIdx(), dates=DATA.dates.slice(i0);
  var fl=DATA.agg.flow.slice(i0);
  var cum=[], acc=0, ok=true;
  fl.forEach(function(v){ if(v===null||v===undefined){ok=false;cum.push(null);} else if(ok){acc+=v;cum.push(Math.round(acc*100)/100);} else {cum.push(null);} });
  var el=document.getElementById('chartTotal');
  if(!C_TOTAL) C_TOTAL=echarts.init(el);
  var b1=bounds([fl],true), b2=bounds([cum],true);
  C_TOTAL.setOption({animation:false,
    tooltip:{trigger:'axis',confine:true,formatter:function(ps){
      var h='<b>'+ps[0].axisValue+'</b><br>';
      ps.forEach(function(p){ h+=p.marker+' '+p.seriesName+': <b>'+(p.value===null?'—':p.value)+'</b> 亿元<br>'; });
      return h;}},
    legend:{top:0,textStyle:{fontSize:11}},
    grid:{left:64,right:64,top:34,bottom:72},
    dataZoom:zoomOpt(dates),
    xAxis:{type:'category',data:dates,axisLabel:{fontSize:9,
      formatter:function(v){return dates.length>400?v.slice(0,7):v.slice(2);}}},
    yAxis:[{type:'value',name:'单日(亿元)',nameTextStyle:{fontSize:10},min:b1.min,max:b1.max,axisLabel:{fontSize:9}},
           {type:'value',name:'累计(亿元)',nameTextStyle:{fontSize:10},min:b2.min,max:b2.max,axisLabel:{fontSize:9},splitLine:{show:false}}],
    series:[
      {name:'单日净流入',type:'bar',data:fl,barMaxWidth:12,
       itemStyle:{color:function(p){return p.value>=0?'rgba(207,34,46,.6)':'rgba(26,127,55,.6)';}}},
      {name:'区间累计',type:'line',yAxisIndex:1,data:cum,showSymbol:false,
       lineStyle:{width:2,color:'#1f6feb'},itemStyle:{color:'#1f6feb'},z:5}
    ]}, true);
  C_TOTAL.off('dataZoom');
  C_TOTAL.on('dataZoom',function(){
    if(C_TOTAL.__zt) clearTimeout(C_TOTAL.__zt);
    C_TOTAL.__zt=setTimeout(function(){
      var r=zoomIdx(C_TOTAL,dates.length); if(!r) return;
      var y1=bounds([fl.slice(r[0],r[1]+1)],true),
          y2=bounds([cum.slice(r[0],r[1]+1)],true);
      C_TOTAL.setOption({yAxis:[{min:y1.min,max:y1.max},{min:y2.min,max:y2.max}]});
    },90);
  });
}
/* ---------- 期货 ---------- */
var C_FUT=null;
function renderFut(){
  var F=DATA.futures, el=document.getElementById('chart_fut');
  if(!C_FUT) C_FUT=echarts.init(el);
  var arrays=[]; F.products.forEach(function(p){ arrays.push(F.series[p].net_short); });
  arrays.push(F.net_total);
  var b=bounds(arrays,true);
  C_FUT.setOption({animation:false,
    tooltip:{trigger:'axis',confine:true},
    legend:{top:0,textStyle:{fontSize:11}},
    grid:{left:70,right:24,top:34,bottom:72},
    dataZoom:zoomOpt(F.dates),
    xAxis:{type:'category',data:F.dates,axisLabel:{fontSize:9,
      formatter:function(v){return F.dates.length>400?v.slice(0,7):v.slice(2);}}},
    yAxis:{type:'value',name:'净空头(手)',nameTextStyle:{fontSize:10},min:b.min,max:b.max,axisLabel:{fontSize:9}},
    series:F.products.map(function(p){
      return {name:p+' '+F.names[p],type:'line',showSymbol:false,
        data:F.series[p].net_short,lineStyle:{width:1.5}};
    }).concat([{name:'四品种合计',type:'line',showSymbol:false,
      data:F.net_total,lineStyle:{width:2.4,color:'#111'},itemStyle:{color:'#111'}}])
  }, true);
  C_FUT.off('dataZoom');
  C_FUT.on('dataZoom',function(){
    if(C_FUT.__zt) clearTimeout(C_FUT.__zt);
    C_FUT.__zt=setTimeout(function(){
      var r=zoomIdx(C_FUT,F.dates.length); if(!r) return;
      var sub=arrays.map(function(a){return a.slice(r[0],r[1]+1);});
      var y=bounds(sub,true);
      C_FUT.setOption({yAxis:{min:y.min,max:y.max}});
    },90);
  });
  document.getElementById('fut-latest-t').innerHTML=
    '最新合约明细（'+F.latest.date+' · '+F.member+'）';
  var tb=document.querySelector('#futTbl tbody'), html='';
  F.products.forEach(function(p){
    (F.latest.contracts[p]||[]).forEach(function(c){
      var lo=c[1], loc=c[2], sho=c[3], shoc=c[4];
      var net=(lo!==null&&sho!==null)?sho-lo:null;
      var nchg=(loc!==null&&shoc!==null)?shoc-loc:null;
      html+='<tr><td class="l">'+p+' '+F.names[p]+'</td><td class="l">'+c[0]+'</td>'
        +'<td>'+fmt(lo,0)+'</td><td class="'+cls(loc)+'">'+sgn(loc,0)+'</td>'
        +'<td>'+fmt(sho,0)+'</td><td class="'+cls(shoc)+'">'+sgn(shoc,0)+'</td>'
        +'<td class="'+cls(net)+'">'+sgn(net,0)+'</td>'
        +'<td class="'+cls(nchg)+'">'+sgn(nchg,0)+'</td></tr>';
    });
  });
  tb.innerHTML=html;
}
/* ---------- 官方基准（只渲染一次） ---------- */
var BASE_DONE=false;
function renderBase(){
  if(BASE_DONE) return;
  BASE_DONE=true;
  var B=DATA.baseline;
  document.getElementById('base-asof').textContent=B.asof+'｜六家合计持仓市值约 '+B.total_value_yi+' 亿元';
  document.getElementById('base-note').textContent=B.note||'';
  var tb=document.querySelector('#tbl-base tbody');
  tb.innerHTML=B.holders.map(function(h){
    return '<tr><td class="l">'+h.name+'</td><td>'+h.etf_count+'</td><td>'+h.shares_yi+'</td>'
      +'<td>'+h.value_yi+'</td><td>'+(h.avg_pct===null?'—':h.avg_pct+'%')+'</td></tr>';
  }).join('');
}
/* ---------- 渲染总入口 ---------- */
function renderAll(){
  document.getElementById('sub').textContent =
    '数据更新至 '+DATA.updated+'（交易日）｜'+DATA.etfs.length+' 只宽基 ETF / '+Object.keys(GROUPS).length+' 个跟踪指数｜期限：'+(RANGE?'近1年':'全部（2024-01 起）');
  document.getElementById('rangeNote').textContent = RANGE?'':'（全部 = 2024-01-02 起，共 '+DATA.dates.length+' 个交易日）';
  renderTotal();
  Object.keys(GROUPS).forEach(function(ix){
    var sel=document.querySelector('select[data-ix="'+ix+'"]');
    renderGroup(ix, sel?sel.value:'__all__');
  });
  renderFut(); renderBase();
  if(C_TOTAL) C_TOTAL.resize();
  Object.keys(GROUPS).forEach(function(ix){ if(GROUPS[ix].el) GROUPS[ix].el.resize(); }); if(C_FUT) C_FUT.resize();
}
function bindRange(){
  document.querySelectorAll('#ranges button').forEach(function(btn){
    btn.onclick=function(){
      document.querySelectorAll('#ranges button').forEach(function(b){b.classList.toggle('active',b===btn);});
      RANGE=parseFloat(btn.dataset.r);
      renderAll();
    };
  });
  window.addEventListener('resize',function(){
    if(C_TOTAL) C_TOTAL.resize();
    Object.keys(GROUPS).forEach(function(ix){ if(GROUPS[ix].el) GROUPS[ix].el.resize(); }); if(C_FUT) C_FUT.resize();
  });
}
</script>
</body></html>"""


if __name__ == "__main__":
    main()
