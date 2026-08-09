# -*- coding: utf-8 -*-
"""累计转股比例时间轴采集（官方交易所口径）

沪市(11xxxx): 上交所 COMMON_SSE_SJ_ZQSJ_KZZZGTJ_L 分页接口
深市(12xxxx): 深交所 convertible_bond_conversion (ShowReport) 分页接口
输出: data/conversion/{code}.csv  列: date,conv_rate_pct (float, 累计%)
增量: 文件已存在则只补 max(date) 之后的记录

用法:
    python fetch_conversion.py                 # 全量(universe 中 11/12 开头的券)
    python fetch_conversion.py --codes 110085,123207
"""
import argparse
import os
import sys
import time

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "data", "conversion")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SLEEP = 0.35
RETRY = 3


def _get(url, params, headers):
    last = None
    for i in range(RETRY):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败: {last}")


def fetch_sse(code):
    """上交所: 返回 [(date_str, conv_rate_pct_float), ...] 按日期升序"""
    url = "https://query.sse.com.cn/commonQuery.do"
    headers = {"User-Agent": UA,
               "Referer": "https://www.sse.com.cn/market/bond/convertible/"}
    rows = []
    page = 1
    while True:
        params = {
            "jsonCallBack": "cb", "isPagination": "true",
            "pageHelp.pageSize": "100", "pageHelp.pageNo": str(page),
            "pageHelp.beginPage": str(page), "pageHelp.cacheSize": "1",
            "pageHelp.endPage": str(page), "pagecache": "false",
            "sqlId": "COMMON_SSE_SJ_ZQSJ_KZZZGTJ_L",
            "SEARCH_YEAR": "", "BOND_CODE": code,
        }
        r = _get(url, params, headers)
        text = r.text.strip()
        if text.startswith("cb("):
            text = text[3:]
        if text.endswith(")"):
            text = text[:-1]
        import json
        js = json.loads(text)
        data = (js.get("pageHelp") or {}).get("data") or []
        if not data:
            break
        for d in data:
            ds = str(d.get("TRADE_DATE_CVS", "")).strip()[:10]
            rate = str(d.get("TOT_CONV_RATE", "")).replace(",", "").strip()
            if not ds or not rate:
                continue
            try:
                rows.append((ds, float(rate)))
            except ValueError:
                continue
        page += 1
        time.sleep(SLEEP)
    rows.sort(key=lambda x: x[0])
    return rows


def fetch_szse(code, cutoff=None):
    """深交所: 返回 [(date_str, conv_rate_pct_float), ...] 按日期升序。
    cutoff(date str) 非空时做增量: 遇到整页日期都 <= cutoff 即停止翻页。"""
    url = "https://www.szse.cn/api/report/ShowReport/data"
    headers = {"User-Agent": UA,
               "Referer": "https://www.szse.cn/market/bond/convertible/index.html"}
    rows = []
    page = 1
    pagecount = None
    while True:
        params = {
            "SHOWTYPE": "JSON", "CATALOGID": "convertible_bond_conversion",
            "TABKEY": "tab1", "txtkzdm": code, "PAGENO": str(page),
            "random": "0.5",
        }
        r = _get(url, params, headers)
        js = r.json()
        tab = js[0] if isinstance(js, list) and js else {}
        if pagecount is None:
            try:
                pagecount = int((tab.get("metadata") or {}).get("pagecount") or 1)
            except ValueError:
                pagecount = 1
        data = tab.get("data") or []
        if not data:
            break
        for d in data:
            ds = str(d.get("conversion_date", "")).strip()[:10]
            rate = str(d.get("accumulated_conversion_ratio", "")).replace(",", "").strip()
            if not ds or not rate:
                continue
            try:
                v = float(rate)
            except ValueError:
                continue
            if cutoff is None or ds > cutoff:
                rows.append((ds, v))
        # 页内/跨页日期均无序(实测), 不能按页早停, 只能翻满 pagecount
        page += 1
        if page > pagecount:
            break
        time.sleep(SLEEP)
    rows.sort(key=lambda x: x[0])
    return rows


def fetch_code(code):
    """增量抓取一只券, 落盘 data/conversion/{code}.csv"""
    path = os.path.join(OUT_DIR, f"{code}.csv")
    existing = None
    if os.path.exists(path):
        try:
            existing = pd.read_csv(path, encoding="utf-8-sig", dtype={"date": str})
        except Exception:
            existing = None
    cutoff = None
    if existing is not None and not existing.empty:
        cutoff = str(existing["date"].max())[:10]
    if code.startswith("11"):
        rows = fetch_sse(code)  # 券少记录少, 直接全量重取
        if cutoff is not None:
            rows = [x for x in rows if x[0] > cutoff]
    else:
        rows = fetch_szse(code, cutoff=cutoff)
    if not rows:
        return 0
    new = pd.DataFrame(rows, columns=["date", "conv_rate_pct"])
    if existing is not None and not existing.empty:
        df = pd.concat([existing, new], ignore_index=True)
    else:
        df = new
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return len(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="", help="只抓指定券, 逗号分隔")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        uni = pd.read_csv(os.path.join(BASE, "data", "universe.csv"),
                          encoding="utf-8-sig", dtype={"SECURITY_CODE": str})
        codes = sorted(c for c in uni["SECURITY_CODE"].str.zfill(6).unique()
                       if c.startswith(("11", "12")))
    print(f"待抓 {len(codes)} 只券")
    ok, empty, fail = 0, 0, 0
    for i, code in enumerate(codes):
        try:
            n = fetch_code(code)
            if n > 0:
                ok += 1
            else:
                empty += 1
        except Exception as e:
            fail += 1
            print(f"[WARN] {code}: {str(e)[:80]}", flush=True)
        if (i + 1) % 50 == 0:
            print(f"{i+1}/{len(codes)} 有数据{ok} 无新增{empty} 失败{fail}", flush=True)
        time.sleep(SLEEP)
    print(f"完成: 有数据 {ok}, 无新增/无记录 {empty}, 失败 {fail}")


if __name__ == "__main__":
    main()
