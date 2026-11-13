#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
细分行业ETF动向追踪 —— 网页生成器 (v2)
输入: 数据库/etf.db + scripts/mapping_final.json + scripts/review_flags.json
输出: 网页/行业站/ (index.html + charts/*.js + echarts.min.js)

v2 改版:
  1. 顶部三区布局：各大类规模表居左，近一周增加/减少 TOP5 分居右上右下（仿原站）
  2. 图表视觉对齐原站：顶部颜色块图例 + 每线终点右侧贴名称标签(endLabel)
  3. 近一周份额变化率：以 100% 为中轴，默认上下限 ±2.5%（超出时自动放宽）
  4. 全图纵轴范围按可见数据动态计算（含 8% 留白），不再依赖自动 scale
  5. 分位数图下小字注明计算口径
  6. 归类审查区改为「妙想MCP 复核记录」（含跟踪标的指数）
"""
import json
import math
import os
import shutil
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "数据库", "etf.db")
OUT = os.path.join(ROOT, "网页", "行业站")
MAPPING = json.load(open(os.path.join(ROOT, "scripts", "mapping_final.json"),
                         encoding="utf-8"))
FLAGS = json.load(open(os.path.join(ROOT, "scripts", "review_flags.json"),
                       encoding="utf-8"))

NEW_SUB_COLOR = {"创业板宽基": "#C0392B", "海外宽基": "#8E44AD",
                 "黄金": "#D4A017", "有色期货": "#8B5E3C",
                 "能源化工": "#2E8B57", "农产品期货": "#B8860B"}
W_WINDOWS = [(6, "w6"), (60, "w60"), (250, "w250")]
D1 = 6  # 近一周交易日数


def percentile_rank(latest, hist):
    valid = [x for x in hist if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if latest is None or len(valid) < 30:
        return None
    return round(100.0 * sum(1 for x in valid if x < latest) / len(valid), 1)


def window_changes(values, w):
    out = [None] * len(values)
    for t in range(w, len(values)):
        base, cur = values[t - w], values[t]
        if base and base > 0 and cur is not None:
            out[t] = cur / base - 1.0
    return out


def r4(x):
    return round(x, 4) if x is not None else None


def main():
    conn = sqlite3.connect(DB)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM shares WHERE exch='SH' ORDER BY date")]
    didx = {d: i for i, d in enumerate(dates)}
    print(f"交易日 {len(dates)}: {dates[0]} ~ {dates[-1]}")

    sub_of_code = {}
    for cat, subs in MAPPING.items():
        for s in subs:
            for e in s["etfs"]:
                sub_of_code[e["code"]] = (cat, s["name"])
    n = len(dates)
    mat = {k: [0.0] * n for k in sub_of_code.values()}
    for date, code, sh in conn.execute(
            "SELECT date, code, shares_yi FROM shares"):
        k = sub_of_code.get(code)
        if k:
            i = didx.get(date)
            if i is not None:
                mat[k][i] += sh
    print(f"矩阵聚合完成: {len(mat)} 子类")

    stats = {}
    for key, values in mat.items():
        st = {"cur": r4(values[-1]), "etf_count": 0}
        for w, tag in W_WINDOWS:
            chg = window_changes(values, w)
            last = chg[-1]
            st[tag] = {"chg": r4(last) if last is not None else None,
                       "pct": percentile_rank(last, chg),
                       "n": sum(1 for x in chg if x is not None)}
        stats[key] = st
    for cat, subs in MAPPING.items():
        for s in subs:
            stats[(cat, s["name"])]["etf_count"] = len(s["etfs"])

    d1_dates = dates[-D1:]
    d1_idx = list(range(n - D1, n))

    # 大类汇总（近一周）
    cat_rows = []
    for cat, subs in MAPPING.items():
        total = sum(mat[(cat, s["name"])][-1] for s in subs)
        first = sum(mat[(cat, s["name"])][d1_idx[0]] for s in subs)
        chg = (total / first - 1) * 100 if first > 0 else None
        cat_rows.append({"cat": cat, "cur": round(total, 1),
                         "first": round(first, 1),
                         "chg": round(chg, 2) if chg is not None else None})
    cat_rows.sort(key=lambda r: -r["cur"])

    # TOP5
    def topn(sign):
        rows = []
        for key, st in stats.items():
            if st["w6"]["chg"] is None:
                continue
            delta = mat[key][n - 1] - mat[key][d1_idx[0]]
            rows.append({"cat": key[0], "sub": key[1],
                         "delta": round(delta, 2),
                         "chg": round(st["w6"]["chg"] * 100, 2),
                         "pct": st["w6"]["pct"],
                         "cur": round(mat[key][n - 1], 1)})
        rows.sort(key=lambda r: sign * r["delta"], reverse=True)
        return rows[:5]
    top_up, top_down = topn(1), topn(-1)

    last_date = dates[-1]
    # 货币 ETF 按口径剔除；商品期货/实物金已单独成类（12_商品期货），不再计入未归类池
    EXCL = ("货币", "快线", "快钱")
    unmapped, n_excl = [], 0
    for code, name, exch in conn.execute(
            "SELECT code, name, exch FROM shares WHERE date=?", (last_date,)):
        if code in sub_of_code:
            continue
        if code[:3] in ("501", "502") or any(p in name for p in EXCL):
            n_excl += 1
            continue
        unmapped.append(f"{code} {name}")

    os.makedirs(os.path.join(OUT, "charts"), exist_ok=True)
    src = os.path.join(ROOT, "网页", "echarts.min.js")
    if os.path.exists(src):
        shutil.copy(src, os.path.join(OUT, "echarts.min.js"))

    for cat, subs in MAPPING.items():
        cid = cat
        S0 = []
        for s in subs:
            vals = mat[(cat, s["name"])]
            S0.append({"name": s["name"],
                       "color": s["color"] or NEW_SUB_COLOR.get(s["name"]) or "#1f6feb",
                       "values": [r4(v) if v else 0 for v in vals]})
        S1 = []
        for s in subs:
            vals = [mat[(cat, s["name"])][i] for i in d1_idx]
            base = next((v for v in vals if v and v > 0), None)
            norm = [round(v / base * 100, 2) if (base and v and v > 0) else None
                    for v in vals]
            S1.append({"name": s["name"], "color": S0[len(S1)]["color"],
                       "values": norm})
        js = ("(function(){var D0=" + json.dumps(dates, separators=(",", ":")) +
              ";var S0=" + json.dumps(S0, separators=(",", ":")) +
              ";var D1=" + json.dumps(d1_dates, separators=(",", ":")) +
              ";var S1=" + json.dumps(S1, separators=(",", ":")) +
              ";var ST=" + json.dumps(
                  {s["name"]: stats[(cat, s["name"])] for s in subs},
                  separators=(",", ":")) +
              ";window.CHARTS_DATA=window.CHARTS_DATA||{};window.CHARTS_DATA["
              + json.dumps(cid) + "]={c1:{id:" + json.dumps(cid + "_c1") +
              ",dates:D0,percent:false,showSymbol:false,series:S0},c2:{id:" +
              json.dumps(cid + "_c2") + ",dates:D0,percent:true,showSymbol:false,series:S0},c3:{id:" +
              json.dumps(cid + "_c3") + ",dates:D1,percent:true,shortDates:true,showSymbol:true,series:S1},stats:ST};})();")
        with open(os.path.join(OUT, "charts", cid + ".js"), "w",
                  encoding="utf-8") as f:
            f.write(js)

    # 每只 ETF 的最新可得份额（个别标的末日停牌/退市时回退到自身最近一条）
    etf_cur = {code: sh for code, sh in conn.execute(
        "SELECT code, shares_yi FROM shares WHERE date=?", (last_date,))}
    stale = set()       # 最新交易日已无数据（退市/清盘/长期停牌）
    for (code,) in conn.execute(
            "SELECT code FROM shares GROUP BY code").fetchall():
        if code not in etf_cur:
            r = conn.execute(
                "SELECT shares_yi FROM shares WHERE code=? AND shares_yi IS NOT NULL "
                "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
            if r:
                etf_cur[code] = r[0]
                stale.add(code)

    # 图例/折线悬停：成分 ETF 明细，按最新份额从大到小排列（不展示规模数值）
    # x=1 标记"已无最新数据"，hover 里用 † 标出，避免把回退值误当成当前规模
    etfs_hover = {}
    for cat, subs in MAPPING.items():
        for s in subs:
            items = [{"code": e["code"], "name": e["name"],
                      "ex": "深" if e["exch"] == "SZ" else "沪",
                      "_s": etf_cur.get(e["code"]) or 0,
                      "x": 1 if e["code"] in stale else 0}
                     for e in s["etfs"]]
            items.sort(key=lambda x: (-x["_s"], x["code"]))
            for it in items:
                it.pop("_s")
                if not it["x"]:
                    it.pop("x")
            etfs_hover[s["name"]] = items
    if stale:
        print(f"[提示] {len(stale)} 只标的已无最新日数据（退市/清盘），hover 中以 † 标注: "
              f"{sorted(stale)[:8]}")

    html = render_html(dates, cat_rows, top_up, top_down, etfs_hover,
                       stats, unmapped, "--review" in sys.argv)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"生成完成 -> {OUT}")
    print(f"分片 {len(MAPPING)} 个; 未归类池 {len(unmapped)} 只")


# ---------------------------------------------------------------- HTML
def render_html(dates, cat_rows, top_up, top_down, etfs_hover, stats,
                unmapped, review):
    import html as H
    cats = list(MAPPING.keys())

    def cat_label(c):
        no, nm = c.split("_", 1)
        return f"{no}.{nm}"

    tabs = "".join(
        f'<button data-cid="{H.escape(c)}" class="{"active" if i == 0 else ""}">'
        f'{cat_label(c)}</button>' for i, c in enumerate(cats))

    def tbl_cat():
        return "".join(
            f'<tr><td>{cat_label(r["cat"])}</td><td>{r["cur"]:.1f}</td>'
            f'<td>{r["first"]:.1f}</td>'
            f'<td class="{"up" if (r["chg"] or 0)>=0 else "down"}">'
            f'{"+" if (r["chg"] or 0)>=0 else ""}{r["chg"]:.2f}%</td></tr>'
            for r in cat_rows)

    def tbl_top(rows, sign):
        return "".join(
            f'<tr><td>{H.escape(r["sub"])}</td>'
            f'<td class="{"up" if r["delta"]>=0 else "down"}">'
            f'{"+" if r["delta"]>=0 else ""}{r["delta"]:.1f}</td>'
            f'<td class="{"up" if r["chg"]>=0 else "down"}">'
            f'{"+" if r["chg"]>=0 else ""}{r["chg"]:.2f}%</td>'
            f'<td>{r["pct"] if r["pct"] is not None else "—"}</td></tr>'
            for r in rows)

    review_block = ""
    if review:
        frows = "".join(
            f'<tr><td>{c}</td><td>{H.escape(v["name"])}</td>'
            f'<td class="l">{H.escape(v["idx"])}</td>'
            f'<td class="l">{cat_label(v["cat"])} / {H.escape(v["sub"])}</td>'
            f'<td class="l">{v["action"]}</td></tr>'
            for c, v in sorted(FLAGS.items()))
        unm_rows = "".join(
            f'<tr><td>{H.escape(c)}</td><td>未归类（当日有份额数据）</td></tr>'
            for c in unmapped)
        review_block = f"""
<section class="review">
<h2>归类复核（妙想MCP 跟踪标的指数逐一核对，定稿后移除）</h2>
<table>
<thead><tr><th>代码</th><th class="l">名称</th><th class="l">跟踪指数</th>
<th class="l">归类位置</th><th class="l">动作</th></tr></thead>
<tbody>{frows}</tbody></table>
<p class="note">未归类池：{len(unmapped)} 只{('（' + '、'.join(unmapped) + '）') if unmapped else ''}——不计入任何子类。</p>
<p class="note"><b>剔除口径</b>：LOF（501/502，共 26 只）、货币 ETF（3 只）。
商品期货与实物金不再剔除，单独列为第 12 大类「商品期货」（黄金 14 只、有色期货、能源化工、农产品期货）。
大类共 12 个（原站「7.消费 / 7.医药」重号已修正；新增子类「创业板宽基」、新增大类「海外市场」「商品期货」）。</p>
<p class="note"><b>与原站对账（2026-11-09 沪市子集）</b>：金融地产 +0.1%、宽基 +2.9%、资源 +5.3% —— 差异全部来自原站未收录的 2024-2026 新上市 ETF，口径一致。
两处大差异均为原站问题：① 原站「科技」比我们少 74.6%，因其漏收录恒生互联 / 恒生科技 / 恒指科技等大盘港股科技 ETF（约 2980 亿份）；② 原站「债券」表显示 16388 亿份，但其成分 ETF 份额合计仅约 165 亿份 —— 原站把债券/LOF 接口的「规模(元)」当成「份额(份)」，数量级差约 100 倍；本站直接采用交易所份额接口，口径已纠正。</p>
</section>"""

    sections = ""
    for i, (cat, subs) in enumerate(MAPPING.items()):
        stat_rows = "".join(
            f'<tr><td>{H.escape(s["name"])}</td>'
            f'<td>{stats[(cat,s["name"])]["cur"]:.1f}</td>'
            f'<td>{stats[(cat,s["name"])]["etf_count"]}</td>'
            f'<td>{fmt_pct(stats[(cat,s["name"])]["w60"])}</td>'
            f'<td>{fmt_pct(stats[(cat,s["name"])]["w250"])}</td></tr>'
            for s in subs)
        n60 = max((stats[(cat, s["name"])]["w60"]["n"] for s in subs), default=0)
        n250 = max((stats[(cat, s["name"])]["w250"]["n"] for s in subs), default=0)
        sections += f"""
<section class="cat" id="sec_{H.escape(cat)}" style="display:{"block" if i==0 else "none"}">
<h2 style="border-left-color:{H.escape(subs[0]['color'] or '#58a6ff')}">{cat_label(cat)}</h2>
<h3>冷热度：份额变化率的历史分位</h3>
<table class="stattbl"><thead><tr><th>子类</th><th>当前规模<br>(亿份)</th><th>成分<br>只数</th>
<th>近3月变化率 · 分位</th><th>近1年变化率 · 分位</th></tr></thead>
<tbody>{stat_rows}</tbody></table>
<p class="note"><b>分位口径</b>：近3月 = 每 60 个交易日的份额变化率 r(t)=V(t)/V(t−60)−1，按日滚动计算；
分位 = 最新一期 r 在该子类<b>自身全部历史 r 序列</b>中的百分位（0%=历史最冷，100%=历史最热）。
本页样本量：近3月 {n60} 期、近1年 {n250} 期；样本 &lt;30 期显示「—」。</p>
<p class="note"><b>区间变化率</b>选取的是「近一周」：{D1} 个收盘日、{D1-1} 个交易日变动，r=V(最新)/V({D1-1}个交易日前)−1，与上表口径不同。</p>

<div class="card"><h3>ETF份额变化全景分析（亿份）</h3>
<div class="chart" id="{H.escape(cat)}_c1"></div>
<p class="note">口径：该子类全部成分 ETF 当日份额合计（交易所清算后数据），单位亿份。<b>鼠标悬停顶部图例色块</b>可查看该子类包含的全部 ETF（按最新规模从大到小排列）。纵轴按可见区间数据自动取值。</p></div>

<div class="card"><h3>长期份额变化率（各小类首个有效日 = 100%）</h3>
<div class="chart" id="{H.escape(cat)}_c2"></div>
<p class="note">口径：以所选区间内该子类的首个有效份额为 100 做归一化，比较不同规模子类的相对扩张速度（非收益率）。</p></div>

<div class="card"><h3>近一周份额变化率（各小类首个有效日 = 100%，纵轴以 100% 为中轴）</h3>
<div class="chart short" id="{H.escape(cat)}_c3"></div>
<p class="note">口径：近一周（{D1} 个收盘日、{D1-1} 个交易日变动），各子类首个有效份额归一为 100%；纵轴默认 ±2.5%（超出时自动放宽）以便看清差异。</p></div>
</section>"""

    etfs_js = "var ETFS=" + json.dumps(etfs_hover, ensure_ascii=False,
                                       separators=(",", ":")) + ";"
    n_all = sum(len(s["etfs"]) for v in MAPPING.values() for s in v)
    n_sh = sum(1 for v in MAPPING.values() for s in v for e in s["etfs"]
               if e["exch"] == "SH")
    n_sz = n_all - n_sh

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>细分行业ETF动向追踪（沪深）</title>
<style>
:root{{--bg:#ffffff;--card:#f6f8fa;--border:#d0d7de;--muted:#57606a;--text:#1f2328;--up:#cf222e;--down:#1a7f37;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{background:var(--bg);color:var(--text);font:14px/1.6 "Microsoft YaHei","PingFang SC",sans-serif;}}
.wrap{{max-width:1240px;margin:0 auto;padding:26px 20px 60px;}}
h1{{font-size:22px;font-weight:700;}}
h2{{font-size:17px;font-weight:700;margin:0 0 10px;padding-left:10px;border-left:3px solid #58a6ff;}}
h3{{font-size:14px;font-weight:600;margin:0 0 8px;color:#1f2328;}}
.sub{{color:var(--muted);font-size:13px;margin-top:5px;}}
.notice{{color:var(--up);font-size:12px;margin-top:4px;}}
.tabs{{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0 0;}}
.tabs button{{cursor:pointer;background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:8px;font:600 13px/1.6 "Microsoft YaHei",sans-serif;padding:5px 14px;}}
.tabs button:hover{{color:#1f2328;border-color:#8b949e;}}
.tabs button.active{{background:#1f6feb;border-color:#1f6feb;color:#fff;}}
.ranges{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 0;}}
.ranges button{{cursor:pointer;background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:8px;font:600 12px/1.6 "Microsoft YaHei",sans-serif;padding:3px 12px;}}
.ranges button:hover{{color:#1f2328;border-color:#8b949e;}}
.ranges button.active{{background:#238636;border-color:#238636;color:#fff;}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--border);border-radius:8px;overflow:hidden;font-size:13px;}}
th,td{{padding:7px 12px;text-align:right;white-space:nowrap;}}
th:first-child,td:first-child{{text-align:left;}}
th{{background:var(--card);color:var(--muted);font-weight:600;}}
tbody tr+tr{{border-top:1px solid #eaeef2;}}
.up{{color:var(--up);font-weight:600;}} .down{{color:var(--down);font-weight:600;}}
/* 顶部三区：左大类表，右上/右下 TOP5 */
.summary{{display:grid;grid-template-columns:1.1fr 1fr;gap:16px;margin-top:20px;align-items:start;}}
.summary .right{{display:flex;flex-direction:column;gap:14px;}}
.summary section{{margin:0;}}
@media(max-width:900px){{.summary{{grid-template-columns:1fr;}}}}
.card{{background:#fff;border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin:12px 0;}}
.chart{{width:100%;height:400px;}}
.chart.short{{height:300px;}}
.note{{color:var(--muted);font-size:12px;line-height:1.65;margin-top:6px;}}
.cat{{display:none;margin-top:12px;}}
.review{{background:#f0f7ff;border:2px dashed #1f6feb;border-radius:10px;padding:14px;margin:18px 0;}}
.review table{{font-size:12.5px;}}
.stattbl td:nth-child(n+2){{white-space:nowrap;}}
footer{{color:var(--muted);font-size:12px;margin-top:26px;border-top:1px solid var(--border);padding-top:12px;line-height:1.7;}}
</style></head><body><div class="wrap">
<h1>细分行业ETF动向追踪（沪深两市）</h1>
<div class="sub">数据区间 {dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）｜近一周区间 {dates[-D1]} ~ {dates[-1]}（{D1} 个收盘日）｜标的 {n_all} 只（沪 {n_sh} / 深 {n_sz}）｜{len(cats)} 大类 / {sum(len(v) for v in MAPPING.values())} 子类</div>
<div class="notice">注意：数据由交易所公开接口抓取，仅反映 ETF 份额与估算资金状况，不可作为唯一买入逻辑，需结合其他逻辑谨慎参考。份额变动 ≠ 资金净流入。</div>
{review_block}
<div class="summary">
<section>
<h2>各大类规模与近期区间变动（亿份）</h2>
<table><thead><tr><th>大类</th><th>当前总规模</th><th>区间首日规模</th><th>变动比例</th></tr></thead>
<tbody>{tbl_cat()}</tbody></table>
<p class="note">区间 = 近一周 {D1} 个收盘日（{dates[-D1]} → {dates[-1]}），即 {D1-1} 个交易日变动。</p>
</section>
<div class="right">
<section>
<h2>近一周份额增加 TOP 5（亿份）</h2>
<table><thead><tr><th>子类</th><th>增加量</th><th>变动%</th><th>分位</th></tr></thead>
<tbody>{tbl_top(top_up, 1)}</tbody></table>
</section>
<section>
<h2>近一周份额减少 TOP 5（亿份）</h2>
<table><thead><tr><th>子类</th><th>减少量</th><th>变动%</th><th>分位</th></tr></thead>
<tbody>{tbl_top(top_down, -1)}</tbody></table>
</section>
</div>
</div>
<p class="note">分位 = 该子类本周变化率（{D1} 个收盘日，r=V(最新)/V({D1-1}个交易日前)−1）在其自身全部历史同口径变化率中的百分位。</p>

<div class="tabs" id="tabs">{tabs}</div>
<div class="ranges" id="ranges">
<button data-r="1">1年</button><button data-r="3">3年</button><button data-r="5">5年</button><button data-r="0" class="active">全部</button>
</div>
{sections}
<footer>数据来源：上交所 / 深交所公开披露（ETF 份额，清算后数据），历史起始 2016-12-01。<br>
分位数说明：所有分位均为「该指标在自身历史分布中的百分位」，0% = 历史最低（最冷），100% = 历史最高（最热）。图表纵轴范围按当前可见区间的数据动态计算并留 8% 余量。</footer>
</div>
<script src="echarts.min.js"></script>
<script>{etfs_js}</script>
<script>
var curRange=0, _charts={{}};

/* ---------- 动态纵轴 ---------- */
function bounds(arrays, mustInclude){{
  var lo=Infinity, hi=-Infinity;
  arrays.forEach(function(a){{ a.forEach(function(v){{
    if(v===null||v===undefined||isNaN(v)) return;
    if(v<lo) lo=v; if(v>hi) hi=v;
  }});}});
  if(lo===Infinity) return {{min:null,max:null}};
  var rawLo=lo;
  if(mustInclude!==undefined&&mustInclude!==null){{
    if(lo>mustInclude) lo=mustInclude;
    if(hi<mustInclude) hi=mustInclude;
  }}
  if(hi===lo){{ hi=lo+Math.abs(lo||1)*0.02; lo=lo-Math.abs(lo||1)*0.02; }}
  var pad=(hi-lo)*0.08, a=lo-pad, b=hi+pad;
  if(rawLo>=0 && a<0) a=0;   /* 非负数据不画到 0 以下 */
  return niceRange(a,b);
}}
/* 把上下限收敛到「整齐刻度」，避免坐标标签出现 4,818.080556 这类长数字 */
function niceRange(lo,hi){{
  if(!isFinite(lo)||!isFinite(hi)) return {{min:null,max:null}};
  var span=hi-lo; if(span<=0) span=Math.abs(hi)||1;
  var raw=span/4, step=Math.pow(10,Math.floor(Math.log10(raw))), m=raw/step;
  if(m>=5) step*=5; else if(m>=2) step*=2;
  return {{min:+(Math.floor(lo/step)*step).toFixed(6), max:+(Math.ceil(hi/step)*step).toFixed(6)}};
}}
/* 100 为中轴：默认 ±2.5%，数据超出时自动放宽 */
function boundsCenter100(arrays){{
  var dev=0;
  arrays.forEach(function(a){{ a.forEach(function(v){{
    if(v===null||v===undefined||isNaN(v)) return;
    dev=Math.max(dev, Math.abs(v-100));
  }});}});
  var lim = dev<=2.5 ? 2.5 : Math.ceil(dev*10)/10*1.15;
  return {{min:100-lim, max:100+lim}};
}}
/* ---------- 期限 ---------- */
function sliceStartIndex(dates){{
  if(!curRange) return 0;
  var last=dates[dates.length-1];
  var cut=(parseInt(last.slice(0,4))-curRange)+last.slice(4);
  for(var i=0;i<dates.length;i++) if(dates[i]>=cut) return i;
  return 0;
}}
function ratesFromFirstPositive(vs){{
  var base=null; for(var i=0;i<vs.length;i++) if(vs[i]>0){{base=vs[i];break;}}
  if(!base) return vs.map(function(){{return null;}});
  return vs.map(function(v){{return v>0? Math.round(v/base*1000)/10 : null;}});
}}
/* ---------- 分片加载 ---------- */
var _chunkReq={{}};
function ensureCat(cid,cb){{
  if(window.CHARTS_DATA&&window.CHARTS_DATA[cid]){{if(cb)cb();return;}}
  if(_chunkReq[cid]){{_chunkReq[cid].push(cb);return;}}
  _chunkReq[cid]=[cb];
  var s=document.createElement('script');
  s.src='charts/'+encodeURIComponent(cid)+'.js';
  s.onload=function(){{(_chunkReq[cid]||[]).forEach(function(f){{f();}}); _chunkReq[cid]=null;}};
  s.onerror=function(){{
    var el=document.getElementById(cid+'_c1');
    if(el) el.innerHTML='<div class="note" style="padding:40px">数据分片加载失败: charts/'+cid+'.js</div>';
    _chunkReq[cid]=null;
  }};
  document.head.appendChild(s);
}}
/* ---------- 图表 ---------- */
function fmtDate(v){{ return v.slice(5); }}
/* 紧凑数字：>=1万显示「万」，避免线尾出现 695951% 这类长串 */
function fmtNum(v,percent){{
  if(v===null||v===undefined||isNaN(v)) return '—';
  var a=Math.abs(v), t;
  if(a>=10000) t=(v/10000).toFixed(1)+'万';
  else if(a>=100) t=v.toFixed(0);
  else t=v.toFixed(1);
  return t+(percent?'%':'');
}}
/* 成分 ETF 明细（按最新份额降序），图例色块与折线悬停共用 */
function etfHtml(name){{
  var arr=ETFS[name];
  if(!arr||!arr.length) return '<b>'+name+'</b>';
  var h='<div style="font-weight:700;margin-bottom:1px">'+name+' · '+arr.length+' 只</div>'
       +'<div style="color:#57606a;margin-bottom:5px;font-size:11px">按最新份额从大到小</div>';
  var hasStale=false;
  arr.forEach(function(e){{ if(e.x) hasStale=true; }});
  if(hasStale) h+='<div style="color:#cf222e;margin:-3px 0 4px;font-size:11px">† 已无最新日数据（退市/清盘），按最后可得日回退</div>';
  arr.forEach(function(e,i){{
    h+='<div style="line-height:1.55;white-space:nowrap">'+(i+1)+'. '+e.code+' '+e.name
      +'<span style="color:#57606a">（'+e.ex+'）</span>'
      +(e.x?'<span style="color:#cf222e"> †</span>':'')+'</div>';
  }});
  return h;
}}
/* 纵轴范围计算：线性/对数切换 + 1-2-5 整齐上限（初始与缩放后共用） */
function calcY(vis,percent,short){{
  if(short) return {{b:boundsCenter100(vis),log:false}};
  var yb=bounds(vis, percent?100:0), useLog=false;
  if(percent){{
    if(yb.min!==null&&yb.min>100) yb.min=100;
    var pos=[];
    vis.forEach(function(a){{a.forEach(function(v){{ if(v!==null&&v!==undefined&&v>0) pos.push(v); }});}});
    if(pos.length>10){{
      var mn=Math.min.apply(null,pos), mx=Math.max.apply(null,pos);
      if(mx/mn>50){{
        useLog=true;
        yb.min=Math.pow(10,Math.floor(Math.log10(mn)));
        var vv=mx*1.15, e10=Math.floor(Math.log10(vv)), mm=vv/Math.pow(10,e10);
        yb.max=(mm<=1?1:mm<=2?2:mm<=5?5:10)*Math.pow(10,e10);
      }}
    }}
  }}
  return {{b:yb,log:useLog}};
}}
/* 长期图：底部范围条(slider) + 鼠标滚轮缩放(inside) */
function zoomOpt(dates){{
  /* inside=滚轮缩放/拖动平移；slider=底部范围条，手柄上显示当前区间起止月 */
  return [{{type:'inside'}},{{type:'slider',height:18,bottom:8,showDetail:true,
    labelFormatter:function(v){{var d=dates[Math.round(v)];return d?d.slice(0,7):'';}}}}];
}}
function renderChart(el,cfg,cid){{
  if(!_charts[el.id]) _charts[el.id]=echarts.init(el);
  var ch=_charts[el.id];
  var percent=!!cfg.percent;
  var dates=cfg.dates, start=0;
  if(!cfg.shortDates){{ start=sliceStartIndex(cfg.dates); dates=cfg.dates.slice(start); }}
  var series=cfg.series.map(function(s){{
    var values=s.values.slice(start);
    if(percent) values=ratesFromFirstPositive(values);
    return {{name:s.name, color:s.color, values:values}};
  }});
  /* 纵轴：动态（缩放时按可见区间重算） */
  var vis=series.map(function(s){{return s.values;}});
  var _y=calcY(vis,percent,cfg.shortDates), yb=_y.b, useLog=_y.log;

  var sList=series.map(function(s){{
    var item={{
      name:s.name,type:'line',data:s.values,
      showSymbol:!!cfg.showSymbol,symbol:'circle',symbolSize:5,
      sampling:cfg.shortDates?undefined:'lttb',
      lineStyle:{{width:2.2,opacity:.9}},
      itemStyle:{{color:s.color}},color:s.color,
      emphasis:{{focus:'series'}},
      endLabel:{{show:true,precision:0,
        formatter:function(p){{
          return p.seriesName+' '+fmtNum(p.value,percent);
        }},
        color:s.color,fontWeight:'bold',fontSize:11,
        backgroundColor:'rgba(255,255,255,.88)',padding:[2,4],borderRadius:3,overflow:'break'}},
      labelLayout:{{moveOverlap:'shiftY',hideOverlap:false}}
    }};
    if(percent&&!useLog) item.areaStyle={{color:s.color,opacity:.12,origin:100}};
    return item;
  }});
  if(percent){{
    sList.push({{name:'基准线',type:'line',data:[],silent:true,
      markLine:{{silent:true,symbol:'none',
        lineStyle:{{color:'#57606a',type:'dashed',width:1.2,opacity:.65}},
        label:{{show:false}},data:[{{yAxis:100}}]}}}});
  }}
  ch.setOption({{
    backgroundColor:'transparent', animation:false,
    legend:{{type:'scroll',top:6,left:12,right:132,itemWidth:14,itemHeight:8,
      itemGap:12,icon:'roundRect',textStyle:{{color:'#57606a',fontSize:11}},pageIconSize:10,
      tooltip:{{show:true,confine:true,enterable:true,hideDelay:400,
        backgroundColor:'#fff',borderColor:'#d0d7de',borderWidth:1,
        padding:[8,10],
        textStyle:{{color:'#1f2328',fontSize:12}},
        extraCssText:'max-height:340px;max-width:400px;overflow:auto;overscroll-behavior:contain;'
          +'white-space:normal;box-shadow:0 4px 14px rgba(0,0,0,.12);border-radius:8px;line-height:1.5;',
        formatter:function(p){{ return etfHtml(p.name||p.value); }} }}}},
    grid:{{left:62,right:132,top:58,bottom:cfg.shortDates?42:76}},
    dataZoom:cfg.shortDates?undefined:zoomOpt(dates),
    tooltip:{{trigger:'axis',confine:true,enterable:true,hideDelay:400,backgroundColor:'#fff',borderColor:'#d0d7de',
      textStyle:{{color:'#1f2328',fontSize:12}},
      formatter:function(ps){{
        var h='<b>'+ps[0].axisValue+'</b><br>';
        ps.forEach(function(p){{
          if(p.seriesName==='基准线') return;
          var v=p.value;
          h+=p.marker+' '+p.seriesName+': <b>'+fmtNum(v,percent)+'</b><br>';
        }});
        return h;
      }}}},
    xAxis:{{type:'category',data:dates,boundaryGap:false,
      axisLine:{{lineStyle:{{color:'#d0d7de'}}}},axisTick:{{show:false}},
      splitLine:{{show:true,alignWithLabel:true,lineStyle:{{color:'#eaeef2',type:'dashed'}}}},
      axisLabel:{{color:'#57606a',fontSize:11,
        formatter:cfg.shortDates?fmtDate:function(v){{return v.slice(0,7);}},
        interval:cfg.shortDates?0:'auto'}},
      axisPointer:{{label:{{backgroundColor:'#d0d7de'}}}}}},
    yAxis:{{type:useLog?'log':'value',logBase:10,min:yb.min,max:yb.max,
      splitLine:{{lineStyle:{{color:'#eaeef2',type:'dashed'}}}},
      axisLabel:{{color:'#57606a',fontSize:11,formatter:function(v){{ return percent?fmtNum(v,true):v; }}}}}},
    series:sList
  }},true);
  ch.off('dataZoom');
  if(!cfg.shortDates){{
    ch.on('dataZoom',function(){{
      if(ch.__zt) clearTimeout(ch.__zt);
      ch.__zt=setTimeout(function(){{
        var o=ch.getOption().dataZoom; if(!o||!o.length) return;
        var n=dates.length;
        var st=o[0].start==null?0:o[0].start, en=o[0].end==null?100:o[0].end;
        var a=Math.max(0,Math.floor(st/100*(n-1))), b=Math.min(n-1,Math.ceil(en/100*(n-1)));
        if(b<=a) return;
        var sub=series.map(function(s){{return s.values.slice(a,b+1);}});
        var ny=calcY(sub,percent,false);
        ch.setOption({{yAxis:{{type:ny.log?'log':'value',min:ny.b.min,max:ny.b.max}}}});
      }},90);
    }});
  }}
}}
function renderCat(cid){{
  if(!cid||!window.CHARTS_DATA||!window.CHARTS_DATA[cid]) return;
  var cfg=window.CHARTS_DATA[cid];
  ['c1','c2','c3'].forEach(function(k){{
    var el=document.getElementById(cid+'_'+k);
    if(el) renderChart(el,cfg[k],cid);
  }});
}}
/* ---------- 交互 ---------- */
document.querySelectorAll('#tabs button').forEach(function(btn){{
  btn.onclick=function(){{
    document.querySelectorAll('#tabs button').forEach(function(b){{b.classList.toggle('active',b===btn);}});
    document.querySelectorAll('.cat').forEach(function(s){{s.style.display='none';}});
    var sec=document.getElementById('sec_'+btn.dataset.cid);
    sec.style.display='block';
    ensureCat(btn.dataset.cid,function(){{
      renderCat(btn.dataset.cid);
      ['c1','c2','c3'].forEach(function(k){{
        var e=document.getElementById(btn.dataset.cid+'_'+k);
        if(e&&_charts[e.id]) _charts[e.id].resize();
      }});
    }});
  }};
}});
document.querySelectorAll('#ranges button').forEach(function(btn){{
  btn.onclick=function(){{
    document.querySelectorAll('#ranges button').forEach(function(b){{b.classList.toggle('active',b===btn);}});
    curRange=parseFloat(btn.dataset.r);
    document.querySelectorAll('.cat').forEach(function(sec){{
      if(sec.style.display==='block') renderCat(sec.id.replace('sec_',''));
    }});
  }};
}});
window.addEventListener('resize',function(){{Object.keys(_charts).forEach(function(k){{_charts[k].resize();}});}});
var firstTab=document.querySelector('#tabs button');
ensureCat(firstTab.dataset.cid,function(){{renderCat(firstTab.dataset.cid);}});
</script>
</body></html>"""


def fmt_pct(w):
    if w is None or w.get("chg") is None:
        return "—"
    chg = w["chg"] * 100
    pct = w.get("pct")
    return (f'<span class="{"up" if chg>=0 else "down"}">'
            f'{"+" if chg>=0 else ""}{chg:.1f}%</span> · '
            f'{pct if pct is not None else "—"} 分位')


if __name__ == "__main__":
    main()
