"""
数据加载模块
==========
负责获取ETF日K线数据，并缓存到本地。

数据源说明：
  - 新浪财经API：免费、不封IP、支持前复权、可获取2000条数据（约8年）
  - 腾讯财经API：免费、不封IP、作为备用数据源
  - 数据字段：date, open, close, high, low, volume

缓存策略：
  - 首次拉取后保存到 data/{code}.csv
  - 后续运行直接读本地，保证回测结果一致性
  - 用 --refresh 参数强制刷新数据
"""

import json
import os
import requests
import pandas as pd
from datetime import datetime, timedelta
from .config import ETF_POOL


# 缓存目录：core同级的data目录
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def get_etf_klines(code: str, days: int = 3000, refresh: bool = False) -> pd.DataFrame:
    """
    获取ETF日K线数据（前复权）。
    
    缓存策略：
      1. 本地有缓存且不强制刷新 → 直接读本地
      2. 本地无缓存或强制刷新 → 从网络拉取并保存
    
    参数:
        code: ETF代码，6位数字，如"518880"
        days: 拉取天数，默认2000天（约8年）
        refresh: 是否强制刷新（忽略缓存）
    
    返回:
        DataFrame，列：date, open, high, low, close, volume
    """
    cache_file = os.path.join(CACHE_DIR, f"{code}.csv")
    
    # 检查缓存
    if not refresh and os.path.exists(cache_file):
        df = pd.read_csv(cache_file)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "close", "high", "low"]:
            df[col] = df[col].astype(float)
        return df
    
    # 从网络拉取（优先新浪，备用腾讯）
    df = _fetch_from_sina(code, days)
    if df.empty:
        df = _fetch_from_tencent(code, days)
    
    if not df.empty:
        # 保存到缓存
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(cache_file, index=False, encoding="utf-8-sig")
    
    return df


def _fetch_from_sina(code: str, days: int = 2000) -> pd.DataFrame:
    """
    从新浪财经API获取ETF日K线数据。
    优势：可获取2000条数据（约8年），不封IP。
    """
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    symbol = f"{prefix}{code}"
    
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {
        "symbol": symbol,
        "scale": "240",  # 日线
        "ma": "no",
        "datalen": str(days),
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        if not data:
            return pd.DataFrame()
        
        df = pd.DataFrame(data)
        df = df.rename(columns={"day": "date"})
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "close", "high", "low"]:
            df[col] = df[col].astype(float)
        
        # 按日期排序，去重
        df = df.sort_values("date").drop_duplicates(subset=["date"]).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"  [WARN] 新浪API获取 {code} 失败: {e}")
        return pd.DataFrame()


def _fetch_from_tencent(code: str, days: int = 800) -> pd.DataFrame:
    """
    从腾讯财经API获取ETF日K线数据（备用）。
    """
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{days},qfq"
    
    req = requests.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"  [WARN] 腾讯API获取 {code} 失败: {e}")
        return pd.DataFrame()
    
    key = f"{prefix}{code}"
    stock_data = data.get("data", {})
    if isinstance(stock_data, dict):
        stock_data = stock_data.get(key, {})
    else:
        return pd.DataFrame()
    
    klines = stock_data.get("qfqday", []) or stock_data.get("day", [])
    if not klines:
        return pd.DataFrame()
    
    df = pd.DataFrame(klines, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "close", "high", "low"]:
        df[col] = df[col].astype(float)
    
    df = df.sort_values("date").drop_duplicates(subset=["date"]).reset_index(drop=True)
    return df


def load_all_data(start_date: str = "2023-01-01", refresh: bool = False) -> dict:
    """
    加载所有ETF池中的数据。
    
    参数:
        start_date: 数据起始日期
        refresh: 是否强制刷新缓存
    
    返回:
        dict，键=ETF代码，值=DataFrame
    """
    print("=" * 60)
    print("加载ETF数据...")
    if refresh:
        print("  (强制刷新模式)")
    else:
        print(f"  (缓存目录: {CACHE_DIR})")
    print("=" * 60)
    
    all_data = {}
    for code, name in ETF_POOL.items():
        cache_file = os.path.join(CACHE_DIR, f"{code}.csv")
        from_cache = not refresh and os.path.exists(cache_file)
        
        print(f"  {'读取' if from_cache else '拉取'} {name}({code})...", end=" ")
        df = get_etf_klines(code, days=2000, refresh=refresh)
        
        if df.empty or len(df) < 60:
            print("[FAIL] 数据不足")
            continue
        
        # 过滤到指定起始日期之后
        df = df[df["date"] >= start_date].reset_index(drop=True)
        if len(df) < 30:
            print(f"[FAIL] 过滤后不足({len(df)}条)")
            continue
        
        all_data[code] = df
        source = "本地" if from_cache else "网络"
        print(f"[OK] {len(df)}条({source}) | {df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')}")
    
    print(f"\n成功加载 {len(all_data)} 个ETF\n")
    return all_data
