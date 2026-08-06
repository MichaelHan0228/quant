# -*- coding: utf-8 -*-
"""双低可转债轮动 —— 数据抓取(增量更新)

用法:
    python fetch_data.py --sample     # 样本模式: 前30只(强制含113013/128013等退市券)
    python fetch_data.py              # 全量模式
"""
import os
import sys
import time
import socket
import argparse
from datetime import datetime

# 项目内 signal.py 会遮蔽标准库 signal(akshare 依赖的 py_mini_racer 需要),
# 把脚本目录/空串/cwd 移到 sys.path 末尾, 让标准库优先
_script_dir = os.path.dirname(os.path.abspath(__file__))
for _p in (_script_dir, "", os.getcwd()):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.append(_script_dir)

import requests
import pandas as pd

import config as C

sys.stdout.reconfigure(encoding="utf-8")

# 给所有 requests 调用(含 akshare 内部)加默认超时, 防止住宅 IP 下无限挂起
_orig_request = requests.sessions.Session.request


def _request_with_timeout(self, method, url, **kwargs):
    kwargs.setdefault("timeout", 15)
    return _orig_request(self, method, url, **kwargs)


requests.sessions.Session.request = _request_with_timeout

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------

def get_json(url, params, retries=C.RETRY_TIMES):
    """带重试的 GET JSON(东财接口在住宅IP下偶发被重置)"""
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(C.RETRY_INTERVAL)
    raise last_err


def code_to_symbol(code):
    """转债代码 -> akshare symbol (sh113050 / sz128013)"""
    return ("sh" if code.startswith("11") else "sz") + code


def code_to_secid(code):
    """转债代码 -> 东财 secid"""
    return ("1." if code.startswith("11") else "0.") + code


def save_csv(df, path):
    df.to_csv(path, index=False, encoding="utf-8-sig")


def read_csv(path, **kw):
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"code": str}, **kw)

# ----------------------------------------------------------------------
# 1. 转债宇宙(东财数据中心, 含已退市)
# ----------------------------------------------------------------------

def fetch_universe():
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    rows, page = [], 1
    while True:
        params = {
            "reportName": "RPT_BOND_CB_LIST", "columns": "ALL",
            "pageNumber": page, "pageSize": 500,
            "source": "WEB", "client": "WEB",
        }
        js = get_json(url, params)
        data = (js.get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        print(f"  universe page {page}: {len(data)} 行")
        if len(data) < 500:
            break
        page += 1
    df = pd.DataFrame(rows)
    save_csv(df, C.UNIVERSE_CSV)
    print(f"universe.csv 保存 {len(df)} 只(含退市)")
    return df

# ----------------------------------------------------------------------
# 2. 强赎信息(集思录, akshare)
# ----------------------------------------------------------------------

def fetch_redeem():
    import akshare as ak
    df = ak.bond_cb_redeem_jsl()
    df = df.rename(columns={"代码": "code"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    save_csv(df, C.REDEEM_CSV)
    print(f"redeem.csv 保存 {len(df)} 行, 状态分布: "
          f"{df['强赎状态'].value_counts().to_dict()}")
    return df

# ----------------------------------------------------------------------
# 3. 转债日线: 主源 akshare, 备用 东财 push2his
# ----------------------------------------------------------------------

def _cb_daily_em(code):
    """备用源: 东财 push2his 日K"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": code_to_secid(code), "klt": 101, "fqt": 0, "lmt": 8000,
        "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56",
    }
    js = get_json(url, params)
    klines = ((js.get("data") or {}).get("klines")) or []
    if not klines:
        raise ValueError(f"{code} 东财备用源无数据")
    rows = [k.split(",") for k in klines]
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df = df[["date", "open", "high", "low", "close", "volume"]]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _cb_daily_akshare(code):
    """主源: akshare 沪深转债日线(含退市券全历史)"""
    import akshare as ak
    df = ak.bond_zh_hs_cov_daily(symbol=code_to_symbol(code))
    if df is None or df.empty:
        raise ValueError(f"{code} akshare 无数据")
    df = df.rename(columns=str.lower)
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    return df[keep].copy()


def fetch_cb_daily(code, delist_date=None):
    """增量抓取单只转债日线, 缓存 data/cb_daily/{code}.csv"""
    path = os.path.join(C.CB_DAILY_DIR, f"{code}.csv")
    old = None
    if os.path.exists(path):
        old = read_csv(path)
        old["date"] = pd.to_datetime(old["date"])
        # 已退市且缓存已覆盖到最后交易日 -> 跳过网络请求
        if delist_date is not None and pd.notna(delist_date):
            if old["date"].max() >= pd.to_datetime(delist_date) - pd.Timedelta(days=7):
                return "skip"
    last_err = None
    for i in range(C.RETRY_TIMES):
        try:
            new = _cb_daily_akshare(code)
            break
        except Exception as e:
            last_err = e
            time.sleep(C.RETRY_INTERVAL)
    else:
        try:
            new = _cb_daily_em(code)  # 主源失败 -> 备用源
        except Exception as e2:
            raise RuntimeError(f"{code} 双源均失败: {last_err} / {e2}")
    new["date"] = pd.to_datetime(new["date"])
    if old is not None and not old.empty:
        new = new[new["date"] > old["date"].max()]
        if new.empty:
            return "uptodate"
        df = pd.concat([old, new], ignore_index=True)
    else:
        df = new
    df = df.drop_duplicates(subset="date").sort_values("date")
    save_csv(df, path)
    return f"{len(df)}行"


def fetch_all_cb_daily(universe, codes):
    """批量抓取转债日线"""
    delist_map = dict(zip(universe["SECURITY_CODE"].astype(str),
                          pd.to_datetime(universe["DELIST_DATE"], errors="coerce")))
    ok, fail = 0, []
    for i, code in enumerate(codes, 1):
        try:
            res = fetch_cb_daily(code, delist_map.get(code))
            ok += 1
            print(f"  [{i}/{len(codes)}] {code} -> {res}")
        except Exception as e:
            fail.append(code)
            print(f"  [{i}/{len(codes)}] {code} 失败: {e}")
        time.sleep(C.CB_FETCH_SLEEP)
    print(f"转债日线完成: 成功 {ok}, 失败 {len(fail)} {fail if fail else ''}")
    return fail

# ----------------------------------------------------------------------
# 4. 正股日线: mootdx (TCP, 不封IP)
# ----------------------------------------------------------------------

_TDX_SERVERS = [
    ('119.97.185.59', 7709), ('124.70.133.119', 7709), ('116.205.183.150', 7709),
    ('123.60.73.44', 7709),  ('116.205.163.254', 7709), ('121.36.225.169', 7709),
    ('123.60.70.228', 7709), ('124.71.9.153', 7709),    ('110.41.147.114', 7709),
    ('124.71.187.122', 7709),
]


def _probe(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def tdx_client(market='std'):
    """创建通达信客户端(规避 mootdx 0.11.x BESTIP bug)"""
    from mootdx.quotes import Quotes
    for ip, port in _TDX_SERVERS:
        if _probe(ip, port):
            return Quotes.factory(market=market, server=(ip, port))
    try:
        return Quotes.factory(market=market, bestip=True)
    except Exception:
        pass
    return Quotes.factory(market=market)


def _tdx_bars_all(client, method, code, last_date=None):
    """分批拉取 K 线全历史; last_date 非空时做增量(拉到覆盖缓存最后日期即停)"""
    frames, start = [], 0
    while True:
        df = method(symbol=code, frequency=9, offset=800, start=start)
        if df is None or df.empty:
            break
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"])
        frames.append(df)
        if len(df) < 800:
            break
        if last_date is not None and df["datetime"].min() <= last_date:
            break
        start += 800
        if start > 12000:  # 安全上限
            break
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="datetime")
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def _stock_daily_sina(code):
    """备用源: 新浪(akshare stock_zh_a_daily, 不复权), mootdx 失效时用"""
    import akshare as ak
    if code.startswith(("6", "9")):
        sym = "sh" + code
    elif code.startswith("8"):
        raise ValueError(f"{code} 北交所, 新浪源不支持")
    else:
        sym = "sz" + code
    df = ak.stock_zh_a_daily(symbol=sym, adjust="")
    if df is None or df.empty:
        raise ValueError(f"{code} 新浪源无数据")
    df = df.rename(columns=str.lower)
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df


def _stock_daily_tx(code):
    """第三备用源: 腾讯(akshare stock_zh_a_hist_tx), 退市正股用(新浪不支持退市股)"""
    import akshare as ak
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    df = ak.stock_zh_a_hist_tx(symbol=sym, start_date="19900101",
                               end_date=datetime.now().strftime("%Y%m%d"), adjust="")
    if df is None or df.empty:
        raise ValueError(f"{code} 腾讯源无数据")
    df = df.rename(columns=str.lower)
    if "volume" not in df.columns:
        df["volume"] = float("nan")  # 腾讯源无成交量字段, 回测只用收盘价
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_stock_daily(client, code):
    """增量抓取单只正股日线(不复权, 转股价值计算所需)

    源优先级: mootdx(通达信) -> 新浪 -> 腾讯(退市股兜底)
    实测 2026-08 本机 tdxpy 解析 K 线响应失败, mootdx 返回空时自动降级
    """
    path = os.path.join(C.STOCK_DAILY_DIR, f"{code}.csv")
    old = None
    last_date = None
    if os.path.exists(path):
        old = read_csv(path)
        old["date"] = pd.to_datetime(old["date"])
        last_date = old["date"].max()
        if last_date >= pd.Timestamp.now().normalize() - pd.Timedelta(days=3):
            return "uptodate"
    df, src = None, None
    if client is not None:
        try:
            df = _tdx_bars_all(client, client.bars, code, last_date)
            if df is not None and not df.empty:
                src = "mootdx"
        except Exception:
            pass
    for fetch, name in [(_stock_daily_sina, "sina"), (_stock_daily_tx, "tencent")]:
        if df is not None and not df.empty:
            break
        last_err = None
        for _ in range(C.RETRY_TIMES):
            try:
                df = fetch(code)
                src = name
                break
            except Exception as e:
                last_err = e
                df = None
                time.sleep(C.RETRY_INTERVAL)
    if df is None or df.empty:
        raise RuntimeError(f"{code} mootdx/新浪/腾讯三源均失败: {last_err}")
    df = df.rename(columns={"datetime": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep]
    if old is not None and not old.empty:
        df = pd.concat([old, df], ignore_index=True)
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    save_csv(df, path)
    return f"{len(df)}行({src})"


def fetch_all_stock_daily(codes):
    try:
        client = tdx_client()
    except Exception as e:
        print(f"  mootdx 客户端创建失败({e}), 全部走新浪源")
        client = None
    ok, fail = 0, []
    for i, code in enumerate(codes, 1):
        try:
            res = fetch_stock_daily(client, code)
            ok += 1
            print(f"  [{i}/{len(codes)}] 正股 {code} -> {res}")
        except Exception as e:
            fail.append(code)
            print(f"  [{i}/{len(codes)}] 正股 {code} 失败: {e}")
        time.sleep(0.3)
    print(f"正股日线完成: 成功 {ok}, 失败 {len(fail)} {fail if fail else ''}")
    return fail

# ----------------------------------------------------------------------
# 5. 基准: 中证转债指数 000832
# ----------------------------------------------------------------------

def fetch_benchmark():
    """基准 000832: mootdx -> 腾讯(akshare) -> 东财 push2his"""
    df, src = None, None
    try:
        client = tdx_client()
        df = _tdx_bars_all(client, client.index, C.BENCH_CODE)
        if df is not None and not df.empty:
            df = df.rename(columns={"datetime": "date", "vol": "volume"})
            src = "mootdx"
        else:
            df = None
    except Exception as e:
        print(f"  mootdx 指数源失败({e}), 转腾讯")
        df = None
    if df is None or df.empty:
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily_tx(symbol=f"sh{C.BENCH_CODE}")
            if df is not None and not df.empty:
                df = df.rename(columns=str.lower)
                src = "tencent"
            else:
                df = None
        except Exception as e:
            print(f"  腾讯指数源失败({e}), 转东财")
            df = None
    if df is None or df.empty:
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": f"1.{C.BENCH_CODE}", "klt": 101, "fqt": 0, "lmt": 8000,
            "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56",
        }
        js = get_json(url, params)
        rows = [k.split(",") for k in (js.get("data") or {}).get("klines", [])]
        df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
        src = "eastmoney"
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].drop_duplicates(subset="date").sort_values("date")
    save_csv(df, C.BENCH_CSV)
    print(f"bench_000832.csv 保存 {len(df)} 行 (来源 {src})")
    return df

# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def select_sample(universe):
    """样本: 强制退市券 + 其余按上市日期排序取满 SAMPLE_SIZE"""
    uni = universe.copy()
    uni["SECURITY_CODE"] = uni["SECURITY_CODE"].astype(str)
    forced = [c for c in C.SAMPLE_FORCE_CODES if c in set(uni["SECURITY_CODE"])]
    rest = uni[~uni["SECURITY_CODE"].isin(forced)].head(C.SAMPLE_SIZE - len(forced))
    codes = forced + rest["SECURITY_CODE"].tolist()
    print(f"样本 {len(codes)} 只: 强制退市券 {forced}")
    return uni[uni["SECURITY_CODE"].isin(codes)].reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="样本模式(30只, 含退市券)")
    args = ap.parse_args()

    os.makedirs(C.CB_DAILY_DIR, exist_ok=True)
    os.makedirs(C.STOCK_DAILY_DIR, exist_ok=True)

    print("== 1/5 抓取转债宇宙 ==")
    universe = fetch_universe()
    universe["SECURITY_CODE"] = universe["SECURITY_CODE"].astype(str)

    print("== 2/5 抓取强赎信息 ==")
    fetch_redeem()

    if args.sample:
        uni = select_sample(universe)
    else:
        uni = universe

    print(f"== 3/5 抓取转债日线 ({len(uni)} 只) ==")
    cb_fail = fetch_all_cb_daily(uni, uni["SECURITY_CODE"].tolist())

    stocks = (uni["CONVERT_STOCK_CODE"].dropna().astype(str)
              .str.replace(r"\..*$", "", regex=True).str.zfill(6).unique().tolist())
    print(f"== 4/5 抓取正股日线 ({len(stocks)} 只) ==")
    stk_fail = fetch_all_stock_daily(stocks)

    print("== 5/5 抓取基准 000832 ==")
    fetch_benchmark()

    print(f"\n全部完成. 转债失败 {len(cb_fail)}, 正股失败 {len(stk_fail)}")


if __name__ == "__main__":
    main()
