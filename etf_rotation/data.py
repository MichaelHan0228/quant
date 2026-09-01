"""数据层：腾讯财经前复权日K线 + 本地CSV缓存（增量更新）。

用前复权（qfq）是因为 mootdx bars 返回不复权价，ETF 分红（如红利低波每年一次）
会造成价格跳空、扭曲动量因子。腾讯接口不封 IP，可放心调用。
"""
import json
import time
import urllib.request
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

# 标的池：代码 -> (腾讯带前缀代码, 名称)。512890/513100/518880 沪市，159949 深市。
ETF_POOL = {
    "512890": ("sh512890", "红利低波ETF"),
    "159949": ("sz159949", "创业板50ETF"),
    "513100": ("sh513100", "纳指ETF"),
    "518880": ("sh518880", "黄金ETF"),
    "511010": ("sh511010", "国债ETF"),
    "159985": ("sz159985", "豆粕ETF"),
    "513520": ("sh513520", "日经ETF"),
    "513130": ("sh513130", "恒生科技ETF"),
    "512400": ("sh512400", "有色金属ETF"),
    "159992": ("sz159992", "创新药ETF"),
    "511260": ("sh511260", "十年国债ETF"),
    "501018": ("sh501018", "南方原油LOF"),
    # 宽基（扩池实验）
    "510050": ("sh510050", "上证50ETF"),
    "510300": ("sh510300", "沪深300ETF"),
    "510500": ("sh510500", "中证500ETF"),
    "512100": ("sh512100", "中证1000ETF"),
    "563300": ("sh563300", "中证2000ETF"),
    "588000": ("sh588000", "科创50ETF"),
    # 申万一级行业代表ETF（行业轮动实验池）
    "512800": ("sh512800", "银行ETF"),
    "512880": ("sh512880", "证券ETF"),
    "512690": ("sh512690", "酒ETF"),
    "512010": ("sh512010", "医药ETF"),
    "512480": ("sh512480", "半导体ETF"),
    "512720": ("sh512720", "计算机ETF"),
    "515790": ("sh515790", "光伏ETF"),
    "516110": ("sh516110", "汽车ETF"),
    "515220": ("sh515220", "煤炭ETF"),
    "515210": ("sh515210", "钢铁ETF"),
    "512200": ("sh512200", "房地产ETF"),
    "512660": ("sh512660", "军工ETF"),
    "512980": ("sh512980", "传媒ETF"),
    "515880": ("sh515880", "通信ETF"),
    "159996": ("sz159996", "家电ETF"),
    "159825": ("sz159825", "农业ETF"),
    "159611": ("sz159611", "电力ETF"),
    "159870": ("sz159870", "化工ETF"),
    "159928": ("sz159928", "消费ETF"),
}

# 默认回测池（文章原版4只），国债ETF需通过 --codes 显式加入
DEFAULT_CODES = ["512890", "159949", "513100", "518880"]

_START = "2012-01-01"  # 多拉数据支持长回测 + 因子 warmup；回测起点由 run_backtest(start=...) 控制


def _fetch_chunk(tencent_code: str, start: str, end: str, count: int = 640) -> list:
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"param={tencent_code},day,{start},{end},{count},qfq"
    )
    # 腾讯偶发 5xx/连接重置（短时限流），重试 3 次带退避
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=15).read().decode()
            node = json.loads(raw)["data"][tencent_code]
            return node.get("qfqday") or node.get("day") or []
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def _fetch_remote(code: str, start: str) -> pd.DataFrame:
    """分页拉取 start 至今的全部前复权日K。

    注意：腾讯接口在给了 end 且范围内条数超过 count 时，返回的是【最靠近 end 的
    count 根】，所以要向前翻页（每次把 end 前移），不能向后翻。
    """
    tencent_code = ETF_POOL[code][0]
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    rows = []
    while True:
        chunk = _fetch_chunk(tencent_code, start, end)
        if not chunk:
            break
        rows = chunk + rows
        if len(chunk) < 640 or chunk[0][0] <= start:
            break
        end = (pd.Timestamp(chunk[0][0]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = df[c].astype(float)
    return df


def load_kline(code: str, refresh: bool = False) -> pd.DataFrame:
    """带 CSV 缓存的日K加载，缓存仅当日有效。

    前复权价以最新价为锚，标的分红后历史价格会整体平移，隔天的缓存即失效，
    所以不做增量更新，跨天一律全量重拉（4只ETF约12次请求，腾讯不封IP）。
    """
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / f"{code}.csv"
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    if cache.exists() and not refresh:
        df = pd.read_csv(cache, dtype={"date": str})
        # 缓存锚定日 = 写入当天（用文件修改日期判断）
        mtime = pd.Timestamp(cache.stat().st_mtime, unit="s").strftime("%Y-%m-%d")
        if mtime == today:
            return df.reset_index(drop=True)
    df = _fetch_remote(code, _START)
    df.to_csv(cache, index=False)
    return df.reset_index(drop=True)


def load_panel(codes: list = None) -> dict:
    """加载标的池日K，返回 {code: DataFrame}，索引为 date。默认 DEFAULT_CODES。"""
    codes = codes or DEFAULT_CODES
    panel = {}
    for code in codes:
        df = load_kline(code)
        panel[code] = df.set_index("date")
        print(f"  {ETF_POOL[code][1]}({code}): {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}, {len(df)} 根")
    return panel
