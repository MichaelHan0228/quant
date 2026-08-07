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
N_LIST = [10]              # 持仓只数(默认 10 只)
BUFFER_RANK = 5            # 调仓缓冲: 在持者排名超出 N+5 才被替换
INITIAL_CASH = 1_000_000   # 初始资金

# ---------- 过滤条件 ----------
MIN_LISTED_DAYS = 10       # 已上市交易日数下限
MIN_EXPIRE_YEARS = 1       # 距到期 > 1 年
MIN_ISSUE_SCALE = 3.0      # 发行规模(亿)下限
MAX_PRICE = 130.0          # 收盘价上限
MIN_AVG_AMOUNT = 0         # 近20日日均成交额下限(元), 0=关闭。
                           # 实测: 1000万门槛误杀43~61%的券(年化13.3%→2.2%), 300万门槛→7.8%;
                           # 双低alpha集中在流动性偏弱的中小盘债, 零售级组合(100万)0.1%滑点已够, 默认关闭;
                           # 组合 >500万 时建议开 3e6~5e6 防冲击成本

# ---------- 止盈线实验 ----------
# 买入仍卡 MAX_PRICE; 开启后持仓券放宽到 HOLD_MAX_PRICE,
# 仅当 价>HOLD_MAX_PRICE 或 (价>MAX_PRICE 且 溢价率>TP_PROFIT_PREMIUM) 时才强制跌出候选被卖出
# 即"让赢家多跑一段, 除非溢价泡沫化"。TAKE_PROFIT_ON=False 时与旧逻辑一致(价>130即轮出)
TAKE_PROFIT_ON = True
HOLD_MAX_PRICE = 140.0
TP_PROFIT_PREMIUM = 0.30
# 评级 >= A+ 的允许集合(排除 A/A-/BBB 及更低、无评级)
ALLOWED_RATINGS = {"AAA", "AA+", "AA", "AA-", "A+"}

# ---------- 交易成本 ----------
COMMISSION = 0.0001        # 佣金 万1, 双边
SLIPPAGE = 0.001           # 单边滑点 0.1%

# ---------- 强赎/退市窗口 ----------
REDEEM_BAN_DAYS = 30       # 距最后交易日 <= N 天: 禁止买入(持仓券随之跌出候选被卖出)

# ---------- 转股价时间轴 ----------
DIVIDENDS_DIR = os.path.join(DATA_DIR, "dividends")  # 正股分红送转(每股事件 → 逐次调整转股价)
REVISIONS_CSV = os.path.join(DATA_DIR, "downward_revisions.csv")  # 下修公告记录(stock,date)
# 下修新价估算开关: 默认关。实测两个失效模式——①同股票多只债时会错配(金田转债误用金铜转债记录);
# ②部分公司不修到底(平煤转债估4.39 vs 实际7.46), 低估TP会高估转股价值→错买, 方向危险。
# 保持关闭时仅误差方向为"下修券溢价率高估→少买", 偏保守。
APPLY_REVISION_EST = False

# ---------- 抓取参数 ----------
CB_FETCH_SLEEP = 0.4       # akshare 转债日线批量抓取间隔(防封IP)
RETRY_TIMES = 3            # 东财请求重试次数
RETRY_INTERVAL = 2.0       # 重试间隔(秒)
SAMPLE_SIZE = 30           # --sample 模式的样本只数
# 样本中必须包含的已退市券(验证退市券日线可拿)
SAMPLE_FORCE_CODES = ["113013", "128013"]

# ---------- 基准 ----------
BENCH_CODE = "000832"      # 中证转债指数
