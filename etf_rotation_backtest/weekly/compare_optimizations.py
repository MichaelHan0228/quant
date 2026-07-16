"""
对比测试：第一批优化效果
=======================
逐项开关，对比每项优化的独立贡献。
"""

import sys
import os
import pandas as pd
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (
    load_all_data, run_backtest, calc_metrics,
    DEFAULT_MA_SHORT, DEFAULT_MA_LONG, INITIAL_CAPITAL,
    BACKTEST_START, BACKTEST_END
)
import core.config as cfg


def run_with_flags(all_data, start_str, end_str, flags: dict, label: str):
    """用指定开关运行回测"""
    # 保存原始值
    originals = {k: getattr(cfg, k) for k in flags}
    # 设置新值
    for k, v in flags.items():
        setattr(cfg, k, v)
    # 运行
    nav_df, trade_log = run_backtest(all_data, start_str, end_str, silent=True)
    metrics = calc_metrics(nav_df)
    # 恢复原始值
    for k, v in originals.items():
        setattr(cfg, k, v)
    return metrics, nav_df, trade_log


def main():
    print("=" * 70)
    print("ETF Rotation Strategy - Batch 1 Optimization Comparison")
    print("=" * 70)

    all_data = load_all_data(BACKTEST_START)

    min_dates = [df["date"].iloc[0] for df in all_data.values()]
    actual_start = max(min_dates) + timedelta(days=cfg.DEFAULT_MA_LONG + 10)
    start_str = max(actual_start, pd.Timestamp(BACKTEST_START)).strftime("%Y-%m-%d")

    configs = [
        ("Baseline (all OFF)", {
            "USE_INVERSE_VOL_WEIGHT": False,
            "USE_STOP_PRICE_OPTIMIZATION": False,
            "USE_TIMEOUT_EXIT": False,
        }),
        ("+ Inverse-Vol Weight", {
            "USE_INVERSE_VOL_WEIGHT": True,
            "USE_STOP_PRICE_OPTIMIZATION": False,
            "USE_TIMEOUT_EXIT": False,
        }),
        ("+ Stop Price Optimization", {
            "USE_INVERSE_VOL_WEIGHT": False,
            "USE_STOP_PRICE_OPTIMIZATION": True,
            "USE_TIMEOUT_EXIT": False,
        }),
        ("+ Timeout Exit", {
            "USE_INVERSE_VOL_WEIGHT": False,
            "USE_STOP_PRICE_OPTIMIZATION": False,
            "USE_TIMEOUT_EXIT": True,
        }),
        ("All 3 ON", {
            "USE_INVERSE_VOL_WEIGHT": True,
            "USE_STOP_PRICE_OPTIMIZATION": True,
            "USE_TIMEOUT_EXIT": True,
        }),
    ]

    results = []
    for label, flags in configs:
        print(f"  Running: {label} ...")
        metrics, nav_df, trade_log = run_with_flags(all_data, start_str, BACKTEST_END, flags, label)
        metrics["label"] = label
        metrics["trades"] = len(trade_log)
        results.append(metrics)

    # 打印对比表
    print("\n" + "=" * 70)
    print(f"{'Config':<30} {'Return':>8} {'Annual':>8} {'MaxDD':>8} {'Sharpe':>8} {'WinRate':>8} {'Trades':>8}")
    print("-" * 70)
    for r in results:
        print(f"{r['label']:<30} {r['total_return']:>+7.1f}% {r['annual_return']:>+7.1f}% "
              f"{r['max_drawdown']:>7.1f}% {r['sharpe']:>8.2f} {r['win_rate']:>7.1f}% {r['trades']:>8d}")

    print("\n" + "=" * 70)
    print("Yearly Returns:")
    print("-" * 70)
    for r_idx, (label, flags) in enumerate(configs):
        metrics, nav_df, _ = run_with_flags(all_data, start_str, BACKTEST_END, flags, label)
        nav_copy = nav_df.copy()
        nav_copy["year"] = nav_copy["date"].dt.year
        yearly = {}
        for year, group in nav_copy.groupby("year"):
            if len(group) >= 2:
                yearly[year] = (group["nav"].iloc[-1] / group["nav"].iloc[0] - 1) * 100
        yr_str = " | ".join(f"{y}:{v:+.1f}%" for y, v in sorted(yearly.items()))
        print(f"  {label:<30} {yr_str}")


if __name__ == "__main__":
    main()
