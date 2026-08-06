# -*- coding: utf-8 -*-
"""双低可转债轮动策略 —— 全局参数配置"""
import os

# ---------- 路径 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CB_DAILY_DIR = os.path.join(DATA_DIR, "cb_daily")
STOCK_DAILY_DIR = os.path.join(DATA_DIR, "stock_daily")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UNIVERSE_CSV = os.path.join(DATA_DIR, "universe.csv")
REDEEM_CSV = os.path.join(DATA_DIR, "redeem.csv")
BENCH_CSV = os.path.join(DATA_DIR, "bench_000832.csv")

# ---------- 回测区间 ----------
START_DATE = "2020-01-01"
END_DATE = None  # None = 数据最新日期

# ---------- 组合参数 ----------
N_LIST = [10, 15, 20]      # 持仓只数变体
BUFFER_RANK = 5            # 调仓缓冲: 在持者排名超出 N+5 才被替换
INITIAL_CASH = 1_000_000   # 初始资金

# ---------- 过滤条件 ----------
MIN_LISTED_DAYS = 10       # 已上市交易日数下限
MIN_EXPIRE_YEARS = 1       # 距到期 > 1 年
MIN_ISSUE_SCALE = 3.0      # 发行规模(亿)下限
MAX_PRICE = 130.0          # 收盘价上限
# 评级 >= A+ 的允许集合(排除 A/A-/BBB 及更低、无评级)
ALLOWED_RATINGS = {"AAA", "AA+", "AA", "AA-", "A+"}

# ---------- 交易成本 ----------
COMMISSION = 0.0001        # 佣金 万1, 双边
SLIPPAGE = 0.001           # 单边滑点 0.1%

# ---------- 转股价近似 ----------
# 数据最新日期往前 TP_RECENT_DAYS 个日历日内用当前 TRANSFER_PRICE, 更早用 INITIAL_TRANSFER_PRICE
TP_RECENT_DAYS = 60

# ---------- 抓取参数 ----------
CB_FETCH_SLEEP = 0.4       # akshare 转债日线批量抓取间隔(防封IP)
RETRY_TIMES = 3            # 东财请求重试次数
RETRY_INTERVAL = 2.0       # 重试间隔(秒)
SAMPLE_SIZE = 30           # --sample 模式的样本只数
# 样本中必须包含的已退市券(验证退市券日线可拿)
SAMPLE_FORCE_CODES = ["113013", "128013"]

# ---------- 基准 ----------
BENCH_CODE = "000832"      # 中证转债指数
