"""入口（默认配置 = 5池 Top2等权 + 止损10% + 阈值1.5 + 窗口25）：
  python run.py backtest [选项]            # 回测（2019至今），输出报告
  python run.py sweep                      # 参数遍历（固定网格）
  python run.py signal [持仓代码] [选项]    # 今日信号：评分排名 + 是否应调仓

通用选项（backtest / signal 都支持）：
  --top-k N          持仓评分前 N 名等权（默认 2）
  --cash-rule RULE   none(默认) / score / ma / ma_strict
  --ma-n N           cash-rule 含 ma 时的均线窗口（默认 25）
  --stop PCT         固定移动止损比例（默认 0.13，0=关闭；只卖破位标的，不连坐）
  --atr MULT         ATR止损倍数：峰值-MULT×ATR(14)（默认 0 关闭；k=1 建议 3.0）
  --atr-n N          ATR 窗口（默认 14）
  --threshold T      调仓阈值（默认 1.5）
  --weights A,B,C    三因子权重（默认 0.3,0.3,0.4）
  --window N         三因子统一窗口（默认 25）
  --codes C1,C2,...  标的池（默认 512890,159949,513100,518880,159985）

示例：
  python run.py backtest                          # 默认最优配置
  python run.py backtest --top-k 1 --stop 0 --atr 3.0 --codes 512890,159949,513100,518880
                                                   # k=1 进攻型配置（原版4池+3xATR）
  python run.py signal 512890                     # 看今日信号
"""
import argparse
import sys

import pandas as pd

from backtest import _target_weights, run_backtest
from data import ETF_POOL, load_panel
from factors import Params, composite_scores, factor_matrices
from report import print_metrics, print_trades, save_report
from sweep import run_sweep


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="run.py")
    p.add_argument("cmd", nargs="?", default="backtest",
                   choices=["backtest", "sweep", "signal"])
    p.add_argument("holding", nargs="?", default=None, help="signal 用的当前持仓代码")
    p.add_argument("--top-k", type=int, default=2)
    p.add_argument("--cash-rule", default="none",
                   choices=["none", "score", "ma", "ma_strict"])
    p.add_argument("--ma-n", type=int, default=25)
    p.add_argument("--stop", type=float, default=0.13)
    p.add_argument("--atr", type=float, default=0.0)
    p.add_argument("--atr-n", type=int, default=14)
    p.add_argument("--threshold", type=float, default=1.5)
    p.add_argument("--threshold-exit", type=float, default=None,
                   help="换出阈值（宜低于换入；不传则与 --threshold 相同走原版逻辑）")
    p.add_argument("--vol-wt", type=int, default=0,
                   help="逆波动率加权窗口（如 25；0=关闭，等权）")
    p.add_argument("--stop-cd", type=int, default=5,
                   help="止损后冷却交易日数（默认 5）")
    p.add_argument("--weight-mode", default="equal", choices=["equal", "score"],
                   help="equal=等权(默认) / score=换仓时按分数定权重，之后漂移")
    p.add_argument("--vol-smooth", type=int, default=0,
                   help="波动率EMA平滑半衰期(交易日)，如 10；0=不平滑")
    p.add_argument("--reb-band", type=float, default=0.0,
                   help="再平衡带（如 0.05：权重偏离≤5pp不交易）；0=关闭")
    p.add_argument("--weights", default="0.3,0.3,0.4",
                   help="因子权重：3个值=乖离/斜率/效率；4个值=追加量能(如 0.25,0.25,0.3,0.2)")
    p.add_argument("--window", type=int, default=25)
    p.add_argument("--codes", default="512890,159949,513100,518880,159985",
                   help="逗号分隔标的池，默认4只原版+豆粕")
    p.add_argument("--start", default="2019-01-01",
                   help="回测起点（动态池：标的未上市则当年不参与轮动）")
    return p.parse_args(argv)


def _params_from(args) -> Params:
    weights = tuple(float(x) for x in args.weights.split(","))
    assert len(weights) in (3, 4), "--weights 需要3或4个值，如 0.3,0.3,0.4 或 0.25,0.25,0.3,0.2"
    return Params.with_window(
        args.window, weights=weights, threshold=args.threshold,
        top_k=args.top_k, cash_rule=args.cash_rule, ma_n=args.ma_n,
        stop_pct=args.stop, atr_mult=args.atr, atr_n=args.atr_n,
        stop_sell_all=False, threshold_exit=args.threshold_exit,
        vol_n=args.vol_wt, stop_cd=args.stop_cd,
        weight_mode=args.weight_mode, vol_smooth=args.vol_smooth,
        reb_band=args.reb_band)


def _panel_and_scores(params: Params, codes: list = None):
    panel = load_panel(codes)
    union = None
    for df in panel.values():
        union = df.index if union is None else union.union(df.index)
    dates = union.sort_values()
    fm = factor_matrices(panel, dates, params)
    return panel, dates, composite_scores(fm, params.weights)


def cmd_backtest(args):
    params = _params_from(args)
    codes = args.codes.split(",") if args.codes else None
    print("加载数据...")
    panel, dates, scores = _panel_and_scores(params, codes)
    print("因子计算完成，开始回测...")
    result = run_backtest(scores, panel, params, start=args.start)
    print_metrics(result["metrics"])
    print_trades(result["trades"])
    save_report(result)


def cmd_signal(args):
    params = _params_from(args)
    holding = args.holding
    codes = args.codes.split(",") if args.codes else None
    print("加载数据...")
    panel, dates, scores = _panel_and_scores(params, codes)
    valid_rows = scores[scores.notna().sum(axis=1) >= 2]
    last = valid_rows.iloc[-1].dropna()
    date = valid_rows.index[-1]
    table = pd.DataFrame({
        "代码": last.index,
        "名称": [ETF_POOL[c][1] for c in last.index],
        "总分": last.values.round(3),
        "最新收盘": [panel[c]["close"].loc[date] for c in last.index],
    }).sort_values("总分", ascending=False)
    print(f"\n===== {date} 收盘评分 =====")
    print(table.to_string(index=False))

    # 与回测引擎同一套逻辑：_target_weights（含过滤/阈值/top_k）
    holdings = {}
    if holding:
        if holding not in last.index:
            print(f"\n持仓代码 {holding} 不在标的池 {list(last.index)} 中")
            return
        holdings = {holding: 1.0 / params.top_k}
    ma_row = {}
    if params.cash_rule in ("ma", "ma_strict"):
        ma = panel_close = pd.DataFrame(
            {c: df["close"] for c, df in panel.items()}).reindex(scores.index)
        ma_ok = panel_close > panel_close.rolling(params.ma_n, min_periods=1).mean()
        ma_row = {c: bool(ma_ok.at[date, c]) for c in last.index}
    target = _target_weights(last, holdings, ma_row, params)

    def _fmt(h):
        return ",".join(f"{c}({ETF_POOL[c][1]})x{w:.0%}" for c, w in h.items()) or "空仓"

    print(f"\n信号：当前持仓 [{_fmt(holdings)}] → 目标持仓 [{_fmt(target)}]")
    diff = {c for c, w in target.items() if holdings.get(c, 0) != w} | \
           {c for c, w in holdings.items() if target.get(c, 0) != w}
    if not diff:
        print("      无需调仓")
    else:
        for c in sorted(diff):
            old, new = holdings.get(c, 0), target.get(c, 0)
            if new > old:
                print(f"      买入 {c}({ETF_POOL[c][1]}) 仓位 {old:.0%}→{new:.0%}")
            else:
                print(f"      卖出 {c}({ETF_POOL[c][1]}) 仓位 {old:.0%}→{new:.0%}")
    if not holding:
        print("\n提示：传入当前持仓代码可精确判断调仓，如: python run.py signal 512890")


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    if args.cmd == "backtest":
        cmd_backtest(args)
    elif args.cmd == "sweep":
        run_sweep()
    elif args.cmd == "signal":
        cmd_signal(args)
