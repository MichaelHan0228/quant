"""
分析工具模块
==========
提供回测结果分析、指标计算、参数敏感性分析等功能。

核心指标：
  - 总收益/年化收益
  - 最大回撤
  - 夏普比率（风险调整后收益）
  - Calmar比率（收益/回撤比）
  - 周胜率/盈亏比
"""

import pandas as pd
import numpy as np
from .config import INITIAL_CAPITAL, SENSITIVITY_MA_SHORT_RANGE, SENSITIVITY_MA_LONG_RANGE


def calc_metrics(nav_df: pd.DataFrame, freq: str = "weekly") -> dict:
    """
    计算回测核心指标。
    
    参数:
        nav_df: 净值曲线DataFrame，必须包含date和nav列
        freq: 数据频率，"daily"(日频)或"weekly"(周频)，影响夏普计算
    
    返回:
        dict，包含所有指标
    
    指标说明：
      - 总收益：(期末净值/期初净值 - 1) * 100
      - 年化收益：考虑复利的年化收益率
      - 最大回撤：净值从高点到低点的最大跌幅
      - 夏普比率：(收益-无风险利率)/波动率 * 年化因子
      - Calmar比率：年化收益/最大回撤（>1算优秀）
    """
    nav_df = nav_df.copy()
    nav_df["return"] = nav_df["nav"].pct_change()
    
    # 总收益
    total_return = (nav_df["nav"].iloc[-1] / nav_df["nav"].iloc[0] - 1) * 100
    
    # 年化收益
    years = (nav_df["date"].iloc[-1] - nav_df["date"].iloc[0]).days / 365.25
    annual_return = ((1 + total_return / 100) ** (1 / years) - 1) * 100 if years > 0 else 0
    
    # 最大回撤
    nav_df["cummax"] = nav_df["nav"].cummax()
    nav_df["drawdown"] = (nav_df["nav"] / nav_df["cummax"] - 1) * 100
    max_drawdown = nav_df["drawdown"].min()
    max_dd_date = nav_df.loc[nav_df["drawdown"].idxmin(), "date"]
    
    # 夏普比率（根据频率选择年化因子）
    returns = nav_df["return"].dropna()
    if freq == "weekly":
        annualize_factor = np.sqrt(52)
        rf_period = 0.02 / 52
    else:
        annualize_factor = np.sqrt(252)
        rf_period = 0.02 / 252
    
    sharpe = (returns.mean() - rf_period) / returns.std() * annualize_factor if returns.std() > 0 else 0
    
    # Calmar比率
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0
    
    # 胜率
    win_rate = (returns > 0).sum() / len(returns) * 100 if len(returns) > 0 else 0
    
    # 盈亏比
    avg_win = returns[returns > 0].mean() if (returns > 0).any() else 0
    avg_loss = abs(returns[returns < 0].mean()) if (returns < 0).any() else 0.001
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_drawdown,
        "max_dd_date": max_dd_date,
        "sharpe": sharpe,
        "calmar": calmar,
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
    }


def run_sensitivity_analysis(all_data: dict, start_date: str, end_date: str) -> pd.DataFrame:
    """
    参数敏感性分析。
    
    扫描短期均线和长期均线的参数组合，验证策略稳健性。
    
    设计思路：
      - 如果大部分参数组合都赚钱 → 策略稳健
      - 如果只有特定参数赚钱 → 有过拟合风险
    
    参数:
        all_data: 所有ETF数据
        start_date: 回测开始日期
        end_date: 回测结束日期
    
    返回:
        DataFrame，每个参数组合的回测结果
    """
    from .backtest import run_backtest
    
    print("\n" + "=" * 60)
    print("参数敏感性分析")
    print("=" * 60)
    
    ma_short_range = SENSITIVITY_MA_SHORT_RANGE
    ma_long_range = SENSITIVITY_MA_LONG_RANGE
    
    results = []
    total = sum(1 for ms in ma_short_range for ml in ma_long_range if ml > ms)
    count = 0
    
    print(f"扫描参数组合...")
    
    for ma_short in ma_short_range:
        for ma_long in ma_long_range:
            if ma_long <= ma_short:
                continue
            
            count += 1
            nav_df, _ = run_backtest(all_data, start_date, end_date,
                                     ma_short=ma_short, ma_long=ma_long, silent=True)
            
            if len(nav_df) > 1:
                metrics = calc_metrics(nav_df)
                results.append({
                    "ma_short": ma_short,
                    "ma_long": ma_long,
                    "total_return": round(metrics["total_return"], 1),
                    "annual_return": round(metrics["annual_return"], 1),
                    "max_drawdown": round(metrics["max_drawdown"], 1),
                    "sharpe": round(metrics["sharpe"], 2),
                    "calmar": round(metrics["calmar"], 2),
                })
            
            if count % 3 == 0:
                print(f"  进度: {count}/{total}")
    
    return pd.DataFrame(results)


def print_sensitivity_results(sens_df: pd.DataFrame):
    """
    打印敏感性分析结果。
    
    输出：
      1. 总收益热力图
      2. 夏普比率热力图
      3. 统计摘要
      4. 最优参数
    """
    print("\n" + "=" * 60)
    print("参数敏感性分析结果")
    print("=" * 60)
    
    if sens_df.empty:
        print("无有效结果")
        return
    
    # 总收益热力图
    print("\n【总收益%】(行=MA短期, 列=MA长期)")
    pivot = sens_df.pivot_table(values="total_return", index="ma_short", columns="ma_long")
    print(pivot.to_string(float_format=lambda x: f"{x:+.1f}%"))
    
    # 夏普比率热力图
    print("\n【夏普比率】(行=MA短期, 列=MA长期)")
    pivot_sharpe = sens_df.pivot_table(values="sharpe", index="ma_short", columns="ma_long")
    print(pivot_sharpe.to_string(float_format=lambda x: f"{x:.2f}"))
    
    # 统计
    profitable_pct = len(sens_df[sens_df["total_return"] > 0]) / len(sens_df) * 100
    sharpe_good = len(sens_df[sens_df["sharpe"] > 0.5]) / len(sens_df) * 100
    
    print(f"\n统计:")
    print(f"  总组合数: {len(sens_df)}")
    print(f"  盈利组合: {profitable_pct:.0f}%")
    print(f"  夏普>0.5: {sharpe_good:.0f}%")
    
    # 最优参数
    best = sens_df.loc[sens_df["sharpe"].idxmax()]
    print(f"\n最优参数（按夏普）: MA({int(best['ma_short'])},{int(best['ma_long'])})")
    print(f"  收益={best['total_return']:+.1f}% 夏普={best['sharpe']:.2f} 回撤={best['max_drawdown']:.1f}%")
    
    # 稳健性判断
    if profitable_pct > 70:
        print(f"\n[GOOD] 策略稳健性: 好（{profitable_pct:.0f}%参数组合盈利）")
    elif profitable_pct > 50:
        print(f"\n[WARN] 策略稳健性: 一般（{profitable_pct:.0f}%参数组合盈利）")
    else:
        print(f"\n[BAD] 策略稳健性: 差（仅{profitable_pct:.0f}%参数组合盈利）")
