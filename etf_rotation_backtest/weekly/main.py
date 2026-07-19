"""
ETF轮动策略 - 主运行脚本
=====================
运行回测、敏感性分析、生成报告。

使用方法：
  python main.py              # 运行完整回测
  python main.py --sensitivity # 运行敏感性分析
  python main.py --help       # 查看帮助
"""

import sys
import os
import argparse
import pandas as pd
from datetime import timedelta

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (
    load_all_data, run_backtest, calc_metrics,
    run_sensitivity_analysis, print_sensitivity_results,
    DEFAULT_MA_SHORT, DEFAULT_MA_LONG, INITIAL_CAPITAL,
    BACKTEST_START, BACKTEST_END
)


def print_results(metrics: dict, nav_df: pd.DataFrame, 
                  trade_log: pd.DataFrame, benchmark_df: pd.DataFrame = None):
    """打印回测结果"""
    print("\n" + "=" * 60)
    print("回测结果")
    print("=" * 60)
    print(f"  总收益:       {metrics['total_return']:+.1f}%")
    print(f"  年化收益:     {metrics['annual_return']:+.1f}%")
    print(f"  最大回撤:     {metrics['max_drawdown']:.1f}%")
    print(f"  夏普比率:     {metrics['sharpe']:.2f}")
    print(f"  Calmar比率:   {metrics['calmar']:.2f}")
    print(f"  周胜率:       {metrics['win_rate']:.1f}%")
    
    # 基准对比
    if benchmark_df is not None and not benchmark_df.empty:
        bench_return = (benchmark_df["nav"].iloc[-1] / benchmark_df["nav"].iloc[0] - 1) * 100
        print(f"\n  基准(沪深300): {bench_return:+.1f}%")
        print(f"  超额收益:      {metrics['total_return'] - bench_return:+.1f}%")
    
    # 年度收益
    print("\n年度收益:")
    nav_df_copy = nav_df.copy()
    nav_df_copy["year"] = nav_df_copy["date"].dt.year
    for year, group in nav_df_copy.groupby("year"):
        if len(group) < 2:
            continue
        yr = (group["nav"].iloc[-1] / group["nav"].iloc[0] - 1) * 100
        print(f"  {year}: {yr:+.1f}%")
    
    # 交易统计
    if not trade_log.empty:
        print(f"\n交易统计:")
        print(f"  交易次数: {len(trade_log)}")
        total_comm = trade_log["commission"].sum()
        print(f"  总佣金: {total_comm:,.0f}元")


def calc_benchmark(all_data: dict, start_date: str, end_date: str) -> pd.DataFrame:
    """计算沪深300 Buy&Hold基准"""
    from core.utils import get_price_on_date, calc_buy_price, calc_commission
    
    code = "510300"
    if code not in all_data:
        return pd.DataFrame()
    
    df = all_data[code]
    df = df[(df["date"] >= start_date) & (df["date"] <= end_date)].copy()
    if df.empty:
        return pd.DataFrame()
    
    first_close = df.iloc[0]["close"]
    buy_price = calc_buy_price(first_close, code)
    shares = int(INITIAL_CAPITAL / buy_price / 100) * 100
    cost = shares * buy_price + calc_commission(shares * buy_price)
    
    df["nav"] = shares * df["close"] + (INITIAL_CAPITAL - cost)
    return df[["date", "nav"]]


def main():
    parser = argparse.ArgumentParser(description="ETF轮动策略回测")
    parser.add_argument("--sensitivity", action="store_true", help="运行参数敏感性分析")
    parser.add_argument("--start", type=str, default=BACKTEST_START, help="回测开始日期")
    parser.add_argument("--end", type=str, default=BACKTEST_END, help="回测结束日期")
    parser.add_argument("--ma-short", type=int, default=DEFAULT_MA_SHORT, help="短期均线周期")
    parser.add_argument("--ma-long", type=int, default=DEFAULT_MA_LONG, help="长期均线周期")
    parser.add_argument("--refresh", action="store_true", help="强制刷新数据缓存")
    args = parser.parse_args()
    
    # 输出目录
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)
    
    # 加载数据
    print("=" * 60)
    print("ETF轮动策略")
    print("=" * 60)
    all_data = load_all_data(BACKTEST_START, refresh=args.refresh)
    
    if len(all_data) < 3:
        print("ERROR: 可用ETF不足3个")
        return
    
    # 确定回测起始日期
    # 从用户指定的起始日期开始，数据不足的ETF会自动跳过
    start_str = pd.Timestamp(args.start).strftime("%Y-%m-%d")
    
    print(f"\n参数: MA({args.ma_short},{args.ma_long})")
    print(f"区间: {start_str} ~ {args.end}")
    
    # 运行回测
    print("\n" + "=" * 60)
    print("运行回测...")
    print("=" * 60)
    
    nav_df, trade_log = run_backtest(
        all_data, start_str, args.end,
        ma_short=args.ma_short, ma_long=args.ma_long
    )
    
    # 计算基准
    benchmark_df = calc_benchmark(all_data, start_str, args.end)
    
    # 计算指标
    metrics = calc_metrics(nav_df)
    
    # 打印结果
    print_results(metrics, nav_df, trade_log, benchmark_df)
    
    # 保存结果
    nav_df.to_csv(os.path.join(output_dir, "nav_curve.csv"), index=False, encoding="utf-8-sig")
    if not trade_log.empty:
        trade_log.to_csv(os.path.join(output_dir, "trades.csv"), index=False, encoding="utf-8-sig")
    
    print(f"\n结果已保存到: {output_dir}")
    
    # 敏感性分析
    if args.sensitivity:
        sens_df = run_sensitivity_analysis(all_data, start_str, args.end)
        print_sensitivity_results(sens_df)
        sens_df.to_csv(os.path.join(output_dir, "sensitivity.csv"), index=False, encoding="utf-8-sig")
    
    print("\nDone!")


if __name__ == "__main__":
    main()
