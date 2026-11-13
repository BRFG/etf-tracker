#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建最终映射表 mapping_final.json
  1. 从 DB 取当前全部代码 + 最新名称
  2. 名称不佳(以N开头)的用东财批量行情补全（f12代码/f14名称）
  3. 沪市：原站已映射(剔除LOF/商品/货币)保留归类；未映射的用规则引擎归类
  4. 深市：sz_classified.json（剔除 __EXCLUDE__）
  5. 大类重编号：1宽基 2风格 3科技 4制造 5能源 6资源 7消费 8医药 9金融地产
     10债券 11海外市场；宽基市场新增「创业板宽基」
"""
import json
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "数据库", "etf.db")

from classify_sz import RULES, MANUAL, FLAGS  # 复用规则引擎（深市版）

# 大类重编号 + 新增
CAT_ORDER = [
    "1_宽基市场", "2_风格策略", "3_科技", "4_制造", "5_能源", "6_资源",
    "7_消费", "8_医药", "9_金融地产", "10_债券", "11_海外市场", "12_商品期货",
]

# ---- 商品期货 / 实物金 ETF（v3 新增大类 12_商品期货）----
# 名称统一为「品种ETF+管理人」，管理人以妙想MCP「基金全称/管理人」核实。
COM_CAT = "12_商品期货"
COMMODITY = {
    # 黄金 / 上海金（实物金，跟踪 AU99.99 / 上海金基准价）
    "518600": ("黄金", "上海金ETF广发"),
    "518660": ("黄金", "黄金ETF工银"),
    "518680": ("黄金", "上海金ETF富国"),
    "518800": ("黄金", "黄金ETF国泰"),
    "518850": ("黄金", "黄金ETF华夏"),
    "518860": ("黄金", "上海金ETF建信"),
    "518880": ("黄金", "黄金ETF华安"),
    "518890": ("黄金", "上海金ETF中银"),
    "159812": ("黄金", "黄金ETF前海开源"),
    "159830": ("黄金", "金ETF天弘"),
    "159831": ("黄金", "金ETF嘉实"),
    "159834": ("黄金", "金ETF南方"),
    "159934": ("黄金", "黄金ETF易方达"),
    "159937": ("黄金", "黄金ETF博时"),
    # 商品期货（非贵金属）
    "159980": ("有色期货", "有色期货ETF大成"),
    "159981": ("能源化工", "能源化工ETF建信"),
    "159985": ("农产品期货", "豆粕ETF华夏"),
}
# 原站大类名 -> 新大类名
CAT_RENAME = {
    "7_消费": "7_消费", "7_医药": "8_医药", "8_金融地产": "9_金融地产",
    "9_债券": "10_债券",
}

# 沪市未收录 33 只的人工归类（名称为券商行情简称，靠代码判定）
SH_MANUAL = {
    "510910": "A股全指",   # 上证综指类
    "512090": "A股大盘", "512160": "A股大盘", "512180": "A股大盘",
    "512360": "A股大盘", "512380": "A股大盘", "512520": "A股大盘",
    "512990": "A股大盘", "515160": "A股大盘", "515770": "A股大盘",  # MSCI中国A股系列
    "512740": "汽车与零部件",
    "513000": "海外宽基",  # 日经225
    "513320": "科技综合",  # 香港新经济
    "513400": "海外宽基",  # 道琼斯
    "513800": "海外宽基",  # 东证
    "516040": "现金流策略", "561420": "现金流策略", "562170": "现金流策略",  # 300现金流
    "517090": "综合策略",  # 共赢
    "520580": "海外宽基",  # 新兴亚洲
    "520920": "科技综合",  # 天弘恒生科技
    "561250": "稀有金属", "561390": "稀有金属",
    "562110": "云计算与数据",  # 云50
    "563210": "A股小盘",   # 专精特新
    "588530": "人工智能",  # 科创板AI(中银)
    "589320": "科创板宽基", "589330": "科创板宽基", "589460": "科创板宽基",  # 科创200
    "589400": "半导体芯片", "589420": "半导体芯片", "589480": "半导体芯片",  # 科创芯设计
    "589410": "人工智能",  # 科创AI(嘉实)
    # ---- 行情简称需人工判定（35只）----
    "510900": "港股宽基",   # 恒生中国企业指数
    "513210": "港股宽基", "520950": "港股宽基", "520560": "港股宽基",
    "513660": "港股宽基",   # 恒生通（flag）
    "512420": "工程机械", "516440": "工程机械", "560920": "工程机械",
    "561030": "工程机械", "561070": "工程机械",
    "513280": "生物医药", "513930": "生物医药", "520760": "生物医药",
    "520930": "生物医药",
    "520690": "医药综合",   # 恒生港药
    "520500": "创新药",     # 恒生新药
    "513730": "海外宽基",   # 东南亚科技
    "520590": "科技综合", "520670": "科技综合", "526030": "科技综合",
    "520710": "科技综合", "520750": "科技综合", "526060": "科技综合",
    "515440": "A股全指",    # A股南方（flag）
    "561730": "A股全指", "561890": "A股全指",
    "515470": "人工智能", "561460": "人工智能", "589310": "人工智能",
    "562400": "金融科技",   # 金科WJ
    "589390": "半导体芯片",  # 芯设计
    "589490": "科创板宽基",  # 科创华夏（flag）
    "561970": "有色金属",   # 金属（flag）
    "511520": "中长期利率债",  # 政金债ETF（原站未收录）
}

# 人工判定中存疑的（审查HTML中标注）
SH_FLAGS = {
    "510140": "名称「上证指」未能判定具体指数，未归类，待确认",
    "526070": "名称「恒生博时」未能判定具体指数，未归类，待确认",
    "513660": "「恒生通」疑为恒生指数类，归港股宽基",
    "515440": "「A股南方」疑为南方中证A股，归A股全指",
    "589490": "「科创华夏」具体科创指数待确认，归科创板宽基",
    "561970": "「金属」疑为有色/细分金属，归有色金属",
}


def clean_name(name: str) -> str:
    """沪市全称清洗：截掉基金类型后缀，保留主题部分"""
    for cut in ("交易型开放式", "交易开放式", "开放式指数", "指数证券投资"):
        i = name.find(cut)
        if i > 2:
            return name[:i]
    return name


def classify_name(name: str):
    n = clean_name(name)
    for sub, kws in RULES:
        if sub == "__EXCLUDE__":
            for kw in kws:
                if kw in n:
                    return "__EXCLUDE__"
            continue
        for kw in kws:
            if kw in n:
                return sub
    return None


def enrich_names(codes_sh, codes_sz):
    """东财 ulist 批量补名称；返回 {code: name}。失败返回 {}"""
    import requests
    sess = requests.Session()
    sess.trust_env = False
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    out = {}
    all_secids = [("1." + c if c[0] == "5" else "0." + c) for c in codes_sh] + \
                 [("0." + c) for c in codes_sz]
    for i in range(0, len(all_secids), 80):
        batch = all_secids[i:i + 80]
        try:
            r = sess.get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                         params={"fltt": "2", "invt": "2", "fields": "f12,f14",
                                 "secids": ",".join(batch)}, timeout=30)
            diff = (r.json().get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            for d in diff:
                out[str(d.get("f12", ""))] = str(d.get("f14", ""))
        except Exception as ex:
            print(f"  ! 名称补全批次失败: {ex}", file=sys.stderr)
        time.sleep(0.3)
    return out


def main():
    conn = sqlite3.connect(DB)
    last_date = conn.execute(
        "SELECT MAX(date) FROM shares WHERE exch='SH'").fetchone()[0]
    print(f"最新交易日: {last_date}")
    # 当前代码+名称（取该日沪市全部；深市取最新非空名）
    cur_sh = {c: n for c, n in conn.execute(
        "SELECT code, name FROM shares WHERE date=? AND exch='SH'", (last_date,))}
    cur_sz = {}
    for c, n, d in conn.execute(
            "SELECT code, name, MAX(date) FROM shares WHERE exch='SZ'"
            " GROUP BY code"):
        cur_sz[c] = n

    # 名称补全（名称为空/N开头/过短）
    need = [c for c, n in list(cur_sh.items()) + list(cur_sz.items()) if not n]
    print(f"需补全名称: {len(need)} 只")
    extra = enrich_names([c for c in need if c in cur_sh],
                         [c for c in need if c in cur_sz])
    for c in need:
        if c in extra:
            (cur_sh if c in cur_sh else cur_sz)[c] = extra[c]
    # 去掉新上市占位 N 前缀
    for d in (cur_sh, cur_sz):
        for c, n in list(d.items()):
            if n.startswith("N") and len(n) > 2:
                d[c] = n[1:]
    print(f"东财兜底名称: {len(extra)} 只")

    # 原站映射
    origin = json.load(open(os.path.join(ROOT, "scripts", "mapping_full.json"),
                            encoding="utf-8"))
    sz_class = json.load(open(os.path.join(ROOT, "scripts", "sz_classified.json"),
                              encoding="utf-8"))

    mapping = {cat: [] for cat in CAT_ORDER}   # cat -> [ {name,color,etfs:[...]}, ... ]
    subs_index = {}                            # (cat, sub) -> etfs list
    unclassified, excluded = [], []

    def add(cat, sub, code, name, exch):
        key = (cat, sub)
        if key not in subs_index:
            entry = {"name": sub, "color": None, "etfs": []}
            mapping[cat].append(entry)
            subs_index[key] = entry["etfs"]
        subs_index[key].append({"code": code, "name": name, "exch": exch})

    # ---- 沪市：原站已映射（剔除501/502 LOF）----
    n_lof = 0
    for cat, subs in origin.items():
        new_cat = CAT_RENAME.get(cat, cat)
        for s in subs:
            for e in s["etfs"]:
                if e["code"][:3] in ("501", "502"):
                    n_lof += 1
                    continue
                if e["code"][:3] == "518":   # 商品金 -> 由末尾 COMMODITY 段统一加入 12_商品期货
                    continue
                # 名称：原站名(去截断省略号)优先
                dbn = cur_sh.get(e["code"])
                name = (e["name"].rstrip("…")) or dbn
                key = (new_cat, s["name"])
                if key not in subs_index:
                    entry = {"name": s["name"], "color": s["color"], "etfs": []}
                    mapping[new_cat].append(entry)
                    subs_index[key] = entry["etfs"]
                subs_index[key].append(
                    {"code": e["code"], "name": name, "exch": "SH"})
    print(f"原站沪市: 剔除 LOF {n_lof} 只, 商品金归入 {COM_CAT}")

    # ---- 沪市：未映射的（规则归类）----
    mapped_sh = {e["code"] for cat in mapping.values() for s in cat for e in s["etfs"]}
    for code, name in sorted(cur_sh.items()):
        if code in mapped_sh or code[:3] in ("501", "502"):
            continue
        if code[:3] == "518":                     # 商品金 -> 末尾统一归类
            continue
        if code[:3] == "511" and code not in SH_MANUAL and not classify_name(name):
            # 511 号段货币（银华日利等）
            excluded.append((code, "货币"))
            continue
        sub = SH_MANUAL.get(code) or classify_name(name)
        if sub == "__EXCLUDE__":
            excluded.append((code, "货币/商品"))
            continue
        if sub is None:
            unclassified.append((code, name, "SH"))
            continue
        cat = sub_to_cat(sub)
        if cat is None:
            unclassified.append((code, name, f"SH:{sub}"))
        else:
            add(cat, sub, code, name, "SH")

    # ---- 深市 ----
    for sub, etfs in sz_class.items():
        if sub == "__EXCLUDE__":
            for e in etfs:
                if e["code"] in COMMODITY:      # 商品 -> 12_商品期货
                    continue
                excluded.append((e["code"], "货币/商品"))
            continue
        cat = sub_to_cat(sub)
        if cat is None:
            for e in etfs:
                unclassified.append((e["code"], e["name"], f"SZ:{sub}"))
            continue
        for e in etfs:
            if e["code"] in COMMODITY:          # 商品从行业子类移到 12_商品期货
                continue
            add(cat, sub, e["code"], e["name"], "SZ")

    # ---- 商品期货 / 实物金（12_商品期货）----
    for code, (sub, disp) in sorted(COMMODITY.items()):
        if code in cur_sh:
            add(COM_CAT, sub, code, disp, "SH")
        elif code in cur_sz:
            add(COM_CAT, sub, code, disp, "SZ")
    print(f"商品期货: {sum(len(s['etfs']) for s in mapping[COM_CAT])} 只 / "
          f"{len(mapping[COM_CAT])} 子类")

    # ---- 输出 ----
    for cat in mapping:
        mapping[cat].sort(key=lambda s: -len(s["etfs"]))
    json.dump(mapping, open(os.path.join(ROOT, "scripts", "mapping_final.json"),
                            "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n_total = sum(len(s["etfs"]) for cat in mapping.values() for s in cat)
    print(f"\n最终映射: {n_total} 只 / {sum(len(v) for v in mapping.values())} 子类")
    for cat in CAT_ORDER:
        subs = mapping[cat]
        n = sum(len(s["etfs"]) for s in subs)
        sz_n = sum(1 for s in subs for e in s["etfs"] if e["exch"] == "SZ")
        print(f"  {cat}: {len(subs)} 子类 / {n} 只（深 {sz_n}）"
              f" -> {[s['name'] for s in subs]}")
    print(f"\n剔除: {len(excluded)} 只 {excluded[:10]}")
    print(f"未归类: {len(unclassified)} {unclassified[:20]}")
    if unclassified:
        json.dump(unclassified, open(os.path.join(ROOT, "scripts", "unclassified.json"),
                                     "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # 存疑清单（审查用）
    flags = {}
    for code, note in list(FLAGS.items()) + list(SH_FLAGS.items()):
        flags.setdefault(code, note)
    for code, name, src in unclassified:
        flags[code] = f"未归类（{name}）"
    json.dump(flags, open(os.path.join(ROOT, "scripts", "review_flags.json"),
                          "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"存疑/待确认: {len(flags)} 只 -> scripts/review_flags.json")


# 子类 -> 大类 反查表（模块级缓存）
_ORIGIN_CACHE = None

def sub_to_cat(sub):
    global _ORIGIN_CACHE
    if _ORIGIN_CACHE is None:
        _ORIGIN_CACHE = json.load(open(os.path.join(ROOT, "scripts", "mapping_full.json"),
                                       encoding="utf-8"))
    for cat, subs in _ORIGIN_CACHE.items():
        if any(s["name"] == sub for s in subs):
            return CAT_RENAME.get(cat, cat)
    if sub == "海外宽基":
        return "11_海外市场"
    if sub in ("创业板宽基", "科创板宽基", "A股全指", "A股大盘", "A股中盘", "A股小盘",
               "沪深港宽基", "港股宽基"):
        return "1_宽基市场"
    return None


if __name__ == "__main__":
    main()
