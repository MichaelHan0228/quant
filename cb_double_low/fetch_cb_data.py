"""
可转债双低回测 - 数据构建
==============================
阶段1: 转债全名单（东财 RPT_BOND_CB_LIST，含退市，1043只）→ data/cb_list.csv
阶段2: 转债日K（新浪 getKLineData，含退市债全历史）→ data/cb_daily/{code}.csv
阶段3: 正股日K（腾讯，复用 weekly_breakout 缓存）→ data/stock_daily/{code}.csv
阶段4: 正股分红送转（东财 RPT_SHAREBONUS_DET）→ data/dividends/{stock}.csv
阶段5: 下修日期（巨潮公告标题检索，仅转股价对不上的债）→ data/downward_revisions.csv

转股价重建口径（引擎用，此处仅采集）：
  起 = INITIAL_TRANSFER_PRICE；分红日 −每股税前派息；送转日 ÷(1+每10股送转/10)；
  若推算终值 ≠ 东财 TRANSFER_PRICE（下修发生），残差按巨潮下修公告日记入。
"""
import os
import re
import time
import random
import json

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
for d in ["cb_daily", "stock_daily", "dividends"]:
    os.makedirs(os.path.join(DATA, d), exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EM = requests.Session()
EM.headers.update({"User-Agent": UA})
_em_last = [0.0]


def em_get(url, params, timeout=20, retries=3):
    """东财请求：≥1s 节流 + 重试（防封）"""
    for i in range(retries):
        wait = 1.0 - (time.time() - _em_last[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.4))
        try:
            r = EM.get(url, params=params, timeout=timeout)
            _em_last[0] = time.time()
            return r
        except Exception:
            _em_last[0] = time.time()
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"em_get 失败: {url}")


# ── 阶段1: 名单 ─────────────────────────────────────────────

def fetch_cb_list():
    out = os.path.join(DATA, "cb_list.csv")
    if os.path.exists(out):
        df = pd.read_csv(out, dtype={"SECURITY_CODE": str, "CONVERT_STOCK_CODE": str})
        if len(df) > 900:
            return df
    rows = []
    for page in range(1, 8):
        r = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
            "reportName": "RPT_BOND_CB_LIST", "columns": "ALL",
            "pageNumber": str(page), "pageSize": "500",
            "sortColumns": "LISTING_DATE", "sortTypes": "1",
            "source": "WEB", "client": "WEB"})
        data = (r.json().get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
    df = pd.DataFrame(rows)
    keep = ["SECURITY_CODE", "SECUCODE", "SECURITY_NAME_ABBR", "CONVERT_STOCK_CODE",
            "LISTING_DATE", "DELIST_DATE", "EXPIRE_DATE", "INITIAL_TRANSFER_PRICE",
            "TRANSFER_PRICE", "RATING", "TRADE_MARKET"]
    df = df[keep].dropna(subset=["LISTING_DATE"])
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"名单: {len(df)} 只")
    return df


# ── 阶段2: 转债日K（腾讯proxy newfqkline，支持退市老债）──────────────

CB_CHUNKS = [("2006-01-01", "2010-12-31"), ("2011-01-01", "2013-12-31"),
             ("2014-01-01", "2016-12-31"), ("2017-01-01", "2019-12-31"),
             ("2020-01-01", "2022-12-31"), ("2023-01-01", "2026-12-31")]


def _tx_cb_fetch(code: str) -> pd.DataFrame:
    mkt = "sh" if code.startswith(("11", "13")) else "sz"
    parts = []
    for start, end in CB_CHUNKS:
        param = f"{mkt}{code},day,{start},{end},800,qfq"
        url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={param}"
        for _ in range(2):
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
                d = r.json()["data"].get(f"{mkt}{code}") or {}
                rows = d.get("qfqday") or d.get("day") or []
                if rows:
                    df = pd.DataFrame(rows).iloc[:, :6]
                    df.columns = ["date", "open", "close", "high", "low", "vol"]
                    parts.append(df)
                break
            except Exception:
                time.sleep(0.5)
        time.sleep(0.12)
    if not parts:
        return pd.DataFrame()
    full = pd.concat(parts).drop_duplicates("date").sort_values("date")
    full["date"] = pd.to_datetime(full["date"])
    for c in ["open", "close", "high", "low", "vol"]:
        full[c] = pd.to_numeric(full[c], errors="coerce")
    return full.dropna(subset=["close"])


def fetch_cb_daily(codes: list[str]):
    ok, fail, skip = 0, [], 0
    for i, code in enumerate(codes):
        path = os.path.join(DATA, "cb_daily", f"{code}.csv")
        if os.path.exists(path):
            df0 = pd.read_csv(path, parse_dates=["date"])
            if not df0.empty and (pd.Timestamp.today() - df0["date"].max()).days <= 10:
                skip += 1
                continue
        df = _tx_cb_fetch(code)
        if df.empty:
            fail.append(code)
        else:
            df.to_csv(path, index=False)
            ok += 1
        if (i + 1) % 100 == 0:
            print(f"转债日K {i+1}/{len(codes)} 成功{ok} 跳过{skip} 失败{len(fail)}", flush=True)
    print(f"转债日K完成: 成功{ok} 跳过{skip} 失败{len(fail)}")
    if fail:
        pd.Series(fail).to_csv(os.path.join(DATA, "cb_daily_fail.csv"), index=False)


# ── 阶段3: 正股日K（腾讯，复用缓存）─────────────────────────

_TX_CACHE_DIRS = [
    r"D:\研究\quant\weekly_breakout\data\daily_tx",
    r"D:\研究\quant\weekly_breakout\data\csi1000_daily",
]
CHUNKS = [("2016-01-01", "2018-12-31"), ("2019-01-01", "2022-12-31"),
          ("2023-01-01", "2026-12-31")]


def _tx_fetch(code: str) -> pd.DataFrame:
    mkt = "sh" if code.startswith(("6", "9")) else "sz"
    parts = []
    for start, end in CHUNKS:
        param = f"{mkt}{code},day,{start},{end},800,qfq"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
        for _ in range(2):
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
                rows = (r.json()["data"].get(f"{mkt}{code}") or {})
                rows = rows.get("qfqday") or rows.get("day") or []
                if rows:
                    df = pd.DataFrame(rows).iloc[:, :6]
                    df.columns = ["date", "open", "close", "high", "low", "vol"]
                    parts.append(df)
                break
            except Exception:
                time.sleep(0.5)
        time.sleep(0.12)
    if not parts:
        return pd.DataFrame()
    full = pd.concat(parts).drop_duplicates("date").sort_values("date")
    full["date"] = pd.to_datetime(full["date"])
    for c in ["open", "close", "high", "low", "vol"]:
        full[c] = full[c].astype(float)
    return full


def fetch_stock_daily(stocks: list[str]):
    import shutil
    ok, fail, skip = 0, [], 0
    for i, code in enumerate(stocks):
        path = os.path.join(DATA, "stock_daily", f"{code}.csv")
        if os.path.exists(path):
            skip += 1
            continue
        copied = False
        for d in _TX_CACHE_DIRS:
            src = os.path.join(d, f"{code}.csv")
            if os.path.exists(src):
                shutil.copy(src, path)
                skip += 1
                copied = True
                break
        if copied:
            continue
        df = _tx_fetch(code)
        if df.empty:
            fail.append(code)
        else:
            df.to_csv(path, index=False)
            ok += 1
        if (i + 1) % 100 == 0:
            print(f"正股日K {i+1}/{len(stocks)} 新拉{ok} 复用{skip} 失败{len(fail)}", flush=True)
    print(f"正股日K完成: 新拉{ok} 复用{skip} 失败{len(fail)}")
    if fail:
        pd.Series(fail).to_csv(os.path.join(DATA, "stock_daily_fail.csv"), index=False)


# ── 阶段4: 分红送转 ─────────────────────────────────────────

def fetch_dividends(stocks: list[str]):
    ok, fail, skip = 0, [], 0
    for i, code in enumerate(stocks):
        path = os.path.join(DATA, "dividends", f"{code}.csv")
        if os.path.exists(path):
            skip += 1
            continue
        try:
            r = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
                "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1", "pageSize": "50",
                "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "1",
                "source": "WEB", "client": "WEB"})
            rows = (r.json().get("result") or {}).get("data") or []
            df = pd.DataFrame(rows)
            keep = ["SECURITY_CODE", "EX_DIVIDEND_DATE", "PRETAX_BONUS_RMB",
                    "TRANSFER_RATIO", "BONUS_RATIO"]
            if df.empty:
                df = pd.DataFrame(columns=keep)
            else:
                df = df[[c for c in keep if c in df.columns]]
            df.to_csv(path, index=False)
            ok += 1
        except Exception as e:
            fail.append(code)
            print(f"[FAIL] 分红 {code}: {str(e)[:60]}")
        if (i + 1) % 100 == 0:
            print(f"分红 {i+1}/{len(stocks)} 成功{ok} 跳过{skip} 失败{len(fail)}", flush=True)
    print(f"分红完成: 成功{ok} 跳过{skip} 失败{len(fail)}")


# ── 阶段5: 下修日期（巨潮，仅残差债）────────────────────────

def fetch_downward_revisions(cb_list: pd.DataFrame):
    """对 INITIAL_TRANSFER_PRICE ≠ TRANSFER_PRICE 的债，用正股+公告检索下修日期"""
    out = os.path.join(DATA, "downward_revisions.csv")
    if os.path.exists(out):
        return pd.read_csv(out)
    cand = cb_list.copy()
    cand["init"] = pd.to_numeric(cand["INITIAL_TRANSFER_PRICE"], errors="coerce")
    cand["cur"] = pd.to_numeric(cand["TRANSFER_PRICE"], errors="coerce")
    cand = cand[(cand["init"].notna()) & (cand["cur"].notna())]
    # 先做分红调整推算（粗略：不算分红时的债，init≈cur 即无下修）
    need = cand[(cand["cur"] / cand["init"] - 1).abs() > 0.01]
    print(f"转股价有变动的债: {len(need)} 只，逐只检索下修公告 ...")
    records = []
    for i, (_, bond) in enumerate(need.iterrows()):
        stock = bond["CONVERT_STOCK_CODE"]
        try:
            # 巨潮公告检索（searchkey=向下修正转股价格）
            org = _cninfo_orgid(stock)
            r = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                data={"stock": f"{stock},{org}", "tabName": "fulltext", "pageSize": "30",
                      "pageNum": "1", "column": "", "category": "", "plate": "",
                      "seDate": "", "searchkey": "向下修正转股价格", "secid": "",
                      "sortName": "", "sortType": "", "isHLtitle": "true"},
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": "https://www.cninfo.com.cn/new/disclosure",
                         "Origin": "https://www.cninfo.com.cn"}, timeout=15)
            anns = (r.json().get("announcements") or [])
            for a in anns:
                title = a.get("announcementTitle", "")
                if "向下修正" in title and "转股价格" in title:
                    ts = a.get("announcementTime")
                    dt = pd.to_datetime(int(ts), unit="ms") if ts else None
                    records.append({"bond": bond["SECURITY_CODE"], "stock": stock,
                                    "date": dt.strftime("%Y-%m-%d") if dt is not None else "",
                                    "title": title})
        except Exception as e:
            print(f"[WARN] 下修检索 {stock}: {str(e)[:50]}")
        if (i + 1) % 50 == 0:
            print(f"下修检索 {i+1}/{len(need)} 已记录{len(records)}", flush=True)
        time.sleep(0.25)
    df = pd.DataFrame(records)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"下修记录: {len(df)} 条")
    return df


_ORGID_CACHE = {}


def _cninfo_orgid(code: str) -> str:
    global _ORGID_CACHE
    if not _ORGID_CACHE:
        try:
            r = requests.get("http://www.cninfo.com.cn/new/data/szse_stock.json",
                             headers={"User-Agent": UA}, timeout=15)
            _ORGID_CACHE = {s["code"]: s["orgId"] for s in r.json().get("stockList", [])}
        except Exception:
            pass
    if code in _ORGID_CACHE:
        return _ORGID_CACHE[code]
    if code.startswith("6"):
        return f"gssh0{code}"
    return f"gssz0{code}"


def main():
    print("阶段1: 转债名单")
    cb = fetch_cb_list()
    codes = cb["SECURITY_CODE"].astype(str).tolist()
    stocks = sorted(cb["CONVERT_STOCK_CODE"].dropna().astype(str).unique().tolist())
    print(f"涉及正股 {len(stocks)} 只")
    print("阶段2: 转债日K")
    fetch_cb_daily(codes)
    print("阶段3: 正股日K")
    fetch_stock_daily(stocks)
    print("阶段4: 分红送转")
    fetch_dividends(stocks)
    print("阶段5: 下修日期")
    fetch_downward_revisions(cb)
    print("全部完成")


if __name__ == "__main__":
    main()
