"""
数据加载模块
==========
负责从腾讯财经接口拉取ETF日K线数据。

数据源说明：
  - 腾讯财经接口（web.ifzq.gtimg.cn）：免费、不封IP、支持前复权
  - 每次最多返回约800条日K线（约3年数据）
  - 数据字段：date, open, close, high, low, volume

注意事项：
  1. 腾讯接口返回GBK编码，需要正确解码
  2. 前复权数据(qfq)会自动调整历史价格，适合回测
  3. 非交易日不会返回数据
"""

import json
import urllib.request
import pandas as pd
from .config import ETF_POOL


def get_etf_klines(code: str, days: int = 800) -> pd.DataFrame:
    """
    从腾讯财经接口拉取ETF日K线数据（前复权）。
    
    参数:
        code: ETF代码，6位数字，如"518880"
        days: 拉取天数，默认800天（约3年）
    
    返回:
        DataFrame，列：date, open, high, low, close, volume
        如果拉取失败返回空DataFrame
    
    示例:
        >>> df = get_etf_klines("518880", days=800)
        >>> print(f"共{len(df)}条数据，从{df['date'].iloc[0]}到{df['date'].iloc[-1]}")
    """
    # 自动判断市场前缀：5/6/9开头=上海(sh)，其他=深圳(sz)
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    
    # 构造请求URL
    # qfq=前复权，day=日线
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={prefix}{code},day,,,{days},qfq")
    
    # 发送请求
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [WARN] 拉取 {code} 失败: {e}")
        return pd.DataFrame()
    
    # 解析返回数据
    key = f"{prefix}{code}"
    stock_data = data.get("data", {})
    if isinstance(stock_data, dict):
        stock_data = stock_data.get(key, {})
    else:
        return pd.DataFrame()
    
    # 提取K线数据（优先前复权，备选不复权）
    klines = stock_data.get("qfqday", []) or stock_data.get("day", [])
    if not klines:
        return pd.DataFrame()
    
    # 构造DataFrame
    df = pd.DataFrame(klines, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "close", "high", "low"]:
        df[col] = df[col].astype(float)
    
    # 按日期排序，去重
    df = df.sort_values("date").drop_duplicates(subset=["date"]).reset_index(drop=True)
    return df


def load_all_data(start_date: str = "2023-01-01") -> dict:
    """
    加载所有ETF池中的数据。
    
    参数:
        start_date: 数据起始日期，格式"YYYY-MM-DD"
    
    返回:
        dict，键=ETF代码，值=DataFrame
    
    示例:
        >>> all_data = load_all_data("2023-01-01")
        >>> print(f"加载了{len(all_data)}个ETF")
        >>> for code, df in all_data.items():
        ...     print(f"  {code}: {len(df)}条")
    """
    print("=" * 60)
    print("加载ETF数据...")
    print("=" * 60)
    
    all_data = {}
    for code, name in ETF_POOL.items():
        print(f"  拉取 {name}({code})...", end=" ")
        df = get_etf_klines(code, days=800)
        
        # 数据质量检查
        if df.empty or len(df) < 60:
            print("[FAIL] 数据不足")
            continue
        
        # 过滤到指定起始日期之后
        df = df[df["date"] >= start_date].reset_index(drop=True)
        if len(df) < 30:
            print(f"[FAIL] 过滤后不足({len(df)}条)")
            continue
        
        all_data[code] = df
        print(f"[OK] {len(df)}条 | {df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')}")
    
    print(f"\n成功加载 {len(all_data)} 个ETF\n")
    return all_data
