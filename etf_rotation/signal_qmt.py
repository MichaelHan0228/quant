# -*- coding: utf-8 -*-
"""ETF 多因子动量轮动 —— 实盘信号工具 (QMT 版)

策略逻辑与 run.py / backtest.py 完全一致（三因子 Z-Score 加权 + 阈值调仓
+ Top-K 等权 + 移动止损/冷却），数据源换成本地 MiniQMT (xtquant) 前复权日线，
替代腾讯接口，规避网络依赖：
  - 日线: xtdata.download_history_data + get_market_data_ex(dividend_type="front")

与回测版的差异说明：
  - 因子用 numpy 手写 OLS（QMT 的 py3.13 环境无 sklearn），数学上等价：
    一元带截距回归的 slope 与 np.polyfit 一致，R² = 皮尔逊相关系数平方。
  - 移动止损需要持仓期峰值/冷却状态，回测在内存里跟踪，实盘持久化在
    state_qmt.json（每日收盘后运行本脚本即自动滚动更新）。
  - 本脚本只出信号、不下单；下单在 QMT 客户端手工执行。

⚠️ 运行环境: xtquant 二进制只支持 Python 3.6~3.13，且需先打开 MiniQMT 并登录:
    py -3.13 signal_qmt.py                          # 空仓状态看评分
    py -3.13 signal_qmt.py --holdings 512890,518880 # 传入当前持仓，出调仓指令
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state_qmt.json")

sys.stdout.reconfigure(encoding="utf-8")

# 标的池（与 data.py 默认池一致）：512890/513100/518880 沪市，159949/159985 深市
ETF_POOL = {
    "512890": "红利低波ETF",
    "159949": "创业板50ETF",
    "513100": "纳指ETF",
    "518880": "黄金ETF",
    "159985": "豆粕ETF",
}
DEFAULT_CODES = list(ETF_POOL)


# ---------------------------------------------------------------- 因子（numpy 版，与 factors.py 等价）

def _ols_slope_r2(y: np.ndarray, x: np.ndarray):
    """一元带截距 OLS：返回 (slope, R²)。R² = corr(x,y)²。"""
    slope = np.polyfit(x, y, 1)[0]
    r = np.corrcoef(x, y)[0, 1]
    return float(slope), float(r * r)


def bias_momentum(close: pd.Series, bias_n: int, momentum_day: int) -> float:
    bias = close / close.rolling(window=bias_n, min_periods=1).mean()
    recent = bias.iloc[-momentum_day:]
    y = (recent / recent.iloc[0]).values
    slope, _ = _ols_slope_r2(y, np.arange(len(y), dtype=float))
    return slope * 10000


def slope_momentum(close: pd.Series, slope_n: int) -> float:
    seg = close.iloc[-slope_n:]
    y = (seg / seg.iloc[0]).values
    slope, r2 = _ols_slope_r2(y, np.arange(1, len(y) + 1, dtype=float))
    return 10000 * slope * r2


def efficiency_momentum(df: pd.DataFrame, eff_n: int) -> float:
    seg = df.iloc[-eff_n:]
    pivot = (seg["open"] + seg["high"] + seg["low"] + seg["close"]) / 4.0
    logp = np.log(pivot.values)
    momentum = 100 * (logp[-1] - logp[0])
    direction = abs(logp[-1] - logp[0])
    volatility = np.abs(np.diff(logp)).sum()
    er = direction / volatility if volatility > 0 else 0.0
    return momentum * er


def latest_scores(panel: dict, window: int, weights: tuple) -> pd.Series:
    """最新一根K线的三因子横截面 Z-Score 加权总分（index=code）。"""
    raw = {}
    for code, df in panel.items():
        if df["close"].notna().sum() < window:
            continue  # 自身历史不足 warmup，当日不在池内
        raw[code] = {
            "bias": bias_momentum(df["close"], window, window),
            "slope": slope_momentum(df["close"], window),
            "eff": efficiency_momentum(df, window),
        }
    fdf = pd.DataFrame(raw)          # index=(bias,slope,eff), columns=code
    if fdf.shape[1] < 2:
        raise RuntimeError(f"有效标的不足 2 只（{list(fdf.columns)}），无法横截面标准化")
    std = fdf.std(axis=1).replace(0, np.nan)
    z = fdf.sub(fdf.mean(axis=1), axis=0).div(std, axis=0).fillna(0.0)
    return (z.T * np.asarray(weights)).sum(axis=1)


# ---------------------------------------------------------------- 调仓逻辑（与 backtest._target_weights 一致）

def _need_switch(best_score: float, hold_score: float, threshold: float) -> bool:
    margin = (threshold - 1.0) * abs(hold_score)
    return best_score > hold_score + margin


def target_weights(scores: pd.Series, holdings: dict, top_k: int,
                   threshold: float, exclude: set = None) -> dict:
    """阈值机制：先填空位，位满后挑战者须超过最弱持仓 + 0.5×|其分数| 才挤入。"""
    exclude = exclude or set()
    passed = {c: s for c, s in scores.items() if c not in exclude}
    survivors = [c for c in holdings if c in passed]
    cands = sorted((c for c in passed if c not in survivors),
                   key=lambda c: -passed[c])
    while len(survivors) < top_k and cands:
        survivors.append(cands.pop(0))
    while cands and survivors:
        weakest = min(survivors, key=lambda c: scores[c])
        if _need_switch(scores[cands[0]], scores[weakest], threshold):
            survivors.remove(weakest)
            survivors.append(cands.pop(0))
        else:
            break
    return {c: 1.0 / top_k for c in survivors}


# ---------------------------------------------------------------- QMT 数据

def _qmt_code(code: str) -> str:
    return f"{code}.SH" if code.startswith("5") else f"{code}.SZ"


def fetch_panel(codes: list, days: int = 200) -> dict:
    """MiniQMT 前复权日线。返回 {code: DataFrame(date, open, close, high, low)}。"""
    from xtquant import xtdata
    try:
        xtdata.enable_hello = False
    except Exception:
        pass
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    panel = {}
    for code in codes:
        qc = _qmt_code(code)
        xtdata.download_history_data(qc, period="1d", start_time=start, end_time=end)
        d = xtdata.get_market_data_ex(
            [], [qc], period="1d", start_time=start, end_time=end,
            dividend_type="front")
        df = d[qc]
        if df.empty:
            raise RuntimeError(f"{qc} 无数据，MiniQMT 是否已登录？")
        idx = df.index.astype(str)
        try:
            dates = pd.to_datetime(idx, format="%Y%m%d")
        except ValueError:  # 部分版本返回毫秒时间戳
            dates = pd.to_datetime(idx.astype(np.int64), unit="ms")
        out = pd.DataFrame({
            "date": dates,
            "open": df["open"].astype(float).values,
            "close": df["close"].astype(float).values,
            "high": df["high"].astype(float).values,
            "low": df["low"].astype(float).values,
        })
        out = out.drop_duplicates("date").sort_values("date").set_index("date")
        panel[code] = out
        print(f"  {ETF_POOL.get(code, code)}({qc}): "
              f"{out.index[0].date()} ~ {out.index[-1].date()}, {len(out)} 根")
    return panel


# ---------------------------------------------------------------- 止损状态持久化

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"peaks": {}, "banned": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="signal_qmt.py")
    ap.add_argument("--holdings", default="", help="当前持仓代码，逗号分隔，如 512890,518880")
    ap.add_argument("--top-k", type=int, default=2)
    ap.add_argument("--threshold", type=float, default=1.5)
    ap.add_argument("--stop", type=float, default=0.13, help="移动止损比例，0=关闭")
    ap.add_argument("--stop-cd", type=int, default=5, help="止损后冷却交易日数")
    ap.add_argument("--weights", default="0.3,0.3,0.4")
    ap.add_argument("--window", type=int, default=25)
    ap.add_argument("--codes", default=",".join(DEFAULT_CODES))
    args = ap.parse_args()

    weights = tuple(float(x) for x in args.weights.split(","))
    assert len(weights) == 3, "--weights 需要3个值"
    holdings = [c.strip() for c in args.holdings.split(",") if c.strip()]
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    print("加载 QMT 日线数据（前复权）...")
    panel = fetch_panel(codes)
    dates = panel[codes[0]].index
    last_date = dates[-1]
    closes = {c: float(panel[c]["close"].iloc[-1]) for c in codes}

    # ---- 移动止损检查（持仓期最高收盘回落 stop 则清仓 + 冷却）----
    state = load_state()
    peaks = state.get("peaks", {})
    banned = state.get("banned", {})
    stopped = []
    if args.stop > 0:
        for c in holdings:
            peak = max(float(peaks.get(c, 0) or 0), closes[c])
            peaks[c] = peak
            if closes[c] < peak * (1 - args.stop):
                stopped.append(c)
                # 冷却 cd 个交易日 ≈ cd*1.5 个自然日（含周末），保守近似
                banned[c] = (last_date + timedelta(days=int(args.stop_cd * 1.5))).strftime("%Y-%m-%d")
        if stopped:
            holdings = [c for c in holdings if c not in stopped]
            print("⚠️ 触发移动止损：")
            for c in stopped:
                print(f"   {c}({ETF_POOL.get(c, c)}) 收盘 {closes[c]} "
                      f"< 峰值 {peaks[c]:.3f}×{1 - args.stop:.0%} → 清仓，冷却至 {banned[c]}")

    # 仍处冷却期的标的剔除出候选
    exclude = {c for c, until in banned.items()
               if until >= last_date.strftime("%Y-%m-%d")}

    # ---- 评分 ----
    scores = latest_scores(panel, args.window, weights)
    table = pd.DataFrame({
        "代码": scores.index,
        "名称": [ETF_POOL.get(c, c) for c in scores.index],
        "总分": scores.values.round(3),
        "最新收盘": [closes[c] for c in scores.index],
        "状态": ["冷却中" if c in exclude else ("持仓" if c in holdings else "")
                for c in scores.index],
    }).sort_values("总分", ascending=False)
    print(f"\n===== {last_date.date()} 收盘评分 =====")
    print(table.to_string(index=False))

    # ---- 目标持仓 ----
    hold_dict = {c: 1.0 / args.top_k for c in holdings if c in scores.index}
    target = target_weights(scores, hold_dict, args.top_k, args.threshold, exclude)

    def _fmt(h):
        return ",".join(f"{c}({ETF_POOL.get(c, c)})x{w:.0%}" for c, w in h.items()) or "空仓"

    print(f"\n信号：当前持仓 [{_fmt(hold_dict)}] → 目标持仓 [{_fmt(target)}]")
    diff = {c for c, w in target.items() if hold_dict.get(c, 0) != w} | \
           {c for c, w in hold_dict.items() if target.get(c, 0) != w}
    if not diff:
        print("      无需调仓")
    else:
        for c in sorted(diff):
            old, new = hold_dict.get(c, 0), target.get(c, 0)
            if new > old:
                print(f"      次日开盘买入 {c}({ETF_POOL.get(c, c)}) 仓位 {old:.0%}→{new:.0%}")
            else:
                print(f"      次日开盘卖出 {c}({ETF_POOL.get(c, c)}) 仓位 {old:.0%}→{new:.0%}")

    # ---- 状态落盘：峰值跟踪目标持仓（新买入以最新收盘为初始峰值）----
    new_peaks = {}
    for c in target:
        new_peaks[c] = float(peaks.get(c, 0) or 0) if c in hold_dict else closes[c]
        if new_peaks[c] <= 0:
            new_peaks[c] = closes[c]
    state["peaks"] = new_peaks
    state["banned"] = {c: u for c, u in banned.items()
                       if u >= last_date.strftime("%Y-%m-%d")}
    save_state(state)
    print(f"\n状态已保存: {STATE_FILE}")
    if not holdings:
        print("提示：传入当前持仓可精确判断调仓，如: py -3.13 signal_qmt.py --holdings "
              + ",".join(list(target)[:args.top_k]))


if __name__ == "__main__":
    main()
