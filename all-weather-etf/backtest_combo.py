# -*- coding: utf-8 -*-
"""全天候组合层回测：真实股息率择时 + 宽基极端低估分档接管
==================================================================
规则（combo-plan.md）：
  常态层：真实股息率全史分位 → 红利低波季度调档（±5pp 步进，国债反向），同 backtest_real_dy。
  宽基层：每月最后一个交易日评估，五档阶梯（建仓 t1 / 满仓 cap / 持有 / 缩仓 t1 / 清仓 0），
    买入凑钱先货币ETF后十年国债ETF（都可清零）；缩仓/清仓回来的钱恢复择时层应有比例。

变体（信号口径 × 标的 两个维度）：
      combo20         分位信号：A500真实PE(2024-09后)/沪深300代理(之前)  标的=A500ETF拼接
      combo15         同上，t1=5pp / cap=15pp
      combo20_s300sig 分位信号：沪深300五年滚动分位(全程)              标的=A500ETF拼接
      combo20_hs300   分位信号：沪深300五年滚动分位                    标的=沪深300ETF全程
      combo20_dual    双确认：A500绝对PE锚(≤13.1进/≤12满/≥14.8缩/≥15清)
                      且沪深300五年分位确认(≤20%进/≤10%满)            标的=A500ETF拼接
      combo20_erp     绝对ERP锚：A500折算ERP(≥5.2%进/≥5.6%满/≤4.7%缩/≤4.3%清)
                      A500ERP=100/(300PE×1.111)−10Y，2024-09后用真实A500PE
      注：dual/erp 用绝对锚，不依赖分位窗口长度（解决A500历史太短问题）。

对照：进取版基线（固定权重）、进取版真实DY择时（单层）。
样本：2020-01 起（真实行情段）、2015-01 起（长样本，拼接段口径见 panel_long.py）。
输出: output/combo_*.csv
"""
import os

import pandas as pd

import backtest
import backtest_timing
from backtest import (LEGS, VARIANTS, INITIAL_CAPITAL, REBAL_BAND, CASH_LEG,
                      load_panel, buy_shares, sell_shares, metrics, yearly,
                      run_backtest)
from backtest_timing import run_backtest_timed
from backtest_a500 import load_a500_series, load_a500_signal
from timing_signal import band_adjust, ROLL_WINDOW, MIN_OBS
from real_dy import build_real_dy
from panel_long import build_panel_long

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DATA_LONG = os.path.join(BASE, "data_long")
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

MAX_STEP = 0.05          # 择时层单次步进
A500_LEG = "a500"        # backtest_a500 模块已把该腿注册进 LEGS（2tick 价差）
HS300_LEG = "hs300"
LEGS[HS300_LEG] = {"name": "沪深300", "spread": 0.002}   # 宽基层直接用 510300 的变体

A500_300_RATIO = 1.111   # 重叠期 A500PE / 300PE 平均比值（2024-09 ~ 2026-07）

# 变体配置：kind = pct_low（分位，低=便宜）/ abs_low（绝对PE，低=便宜）/ abs_high（绝对ERP，高=便宜）
# lines = (满仓线, 建仓线, 缩仓线, 清仓线)；confirm = 进场需沪深300五年分位确认（满仓线, 建仓线）
VARIANTS_A500 = {
    "combo20":         {"t1": 0.10, "cap": 0.20, "kind": "pct_low",  "sig": "a500",
                        "lines": (0.10, 0.20, 0.50, 0.60), "leg": A500_LEG},
    "combo15":         {"t1": 0.05, "cap": 0.15, "kind": "pct_low",  "sig": "a500",
                        "lines": (0.10, 0.20, 0.50, 0.60), "leg": A500_LEG},
    "combo20_s300sig": {"t1": 0.10, "cap": 0.20, "kind": "pct_low",  "sig": "s300",
                        "lines": (0.10, 0.20, 0.50, 0.60), "leg": A500_LEG},
    "combo20_hs300":   {"t1": 0.10, "cap": 0.20, "kind": "pct_low",  "sig": "s300",
                        "lines": (0.10, 0.20, 0.50, 0.60), "leg": HS300_LEG},
    "combo20_dual":    {"t1": 0.10, "cap": 0.20, "kind": "abs_low",  "sig": "pe_a500",
                        "lines": (12.0, 13.1, 14.8, 15.0), "leg": A500_LEG,
                        "confirm": ("s300", 0.10, 0.20)},
    "combo20_erp":     {"t1": 0.10, "cap": 0.20, "kind": "abs_high", "sig": "erp_a500",
                        "lines": (5.6, 5.2, 4.7, 4.3), "leg": A500_LEG},
}

# v3 正式底仓（2026-07-26 起）：红利低波25 / 标普500 20 / 十年国债25 / 黄金20 / 豆粕5 / 货币5
# 择时层浮动范围同步平移：红利低波 15%~35%，国债反向。
V3_WEIGHTS = {"hlb": 0.25, "sp500": 0.20, "bond10": 0.25,
              "gold": 0.20, "soybean": 0.05, "cash": 0.05}

# 国债腿下限：A500 凑钱先货币后国债，但国债不卖穿此线（保对冲引擎）。
# 0 = 关闭（v3 原版行为）；0.10 = 下限10%（2026-07-30 增强版实验）
BOND_FLOOR = 0.0


def build_dy_signal() -> pd.DataFrame:
    """真实股息率全史扩展分位 → 择时层档位（同 backtest_real_dy 口径）"""
    dy = build_real_dy()["dy"].dropna()
    pct = dy.expanding(min_periods=250).apply(lambda x: (x <= x[-1]).mean(), raw=True)
    return pd.DataFrame({"pct": pct, "adjust": pct.map(band_adjust)}).dropna()


def build_hs300_signal() -> pd.Series:
    """沪深300 PE 五年滚动分位（全程用这一条，不切换到 A500 真实PE）"""
    pe = pd.read_csv(os.path.join(DATA, "csi300_pe.csv"),
                     parse_dates=["date"]).set_index("date")["pe"].sort_index()
    pe = pe[~pe.index.duplicated(keep="last")]
    pct = pe.rolling(ROLL_WINDOW, min_periods=MIN_OBS).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)
    pct.name = "pct"
    return pct.dropna()


def build_a500_pe_series() -> pd.Series:
    """A500 折算市盈率：2024-09-03 前 = 沪深300PE × 1.111，之后用真实 A500 PE"""
    pe300 = pd.read_csv(os.path.join(DATA, "csi300_pe.csv"),
                        parse_dates=["date"]).set_index("date")["pe"].sort_index()
    pe500 = pd.read_csv(os.path.join(DATA, "a500_pe.csv"),
                        parse_dates=["date"]).set_index("date")["pe"].sort_index()
    pe300 = pe300[~pe300.index.duplicated(keep="last")]
    pe500 = pe500[~pe500.index.duplicated(keep="last")]
    proxy = (pe300 * A500_300_RATIO)[pe300.index < pe500.index[0]]
    s = pd.concat([proxy, pe500]).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s.name = "pe_a500"
    return s


def build_a500_erp_series() -> pd.Series:
    """A500 折算 ERP = 盈利收益率(100/PE) − 10年国债收益率"""
    pe = build_a500_pe_series()
    y10 = pd.read_csv(os.path.join(DATA, "cgb10y.csv"),
                      parse_dates=["date"]).set_index("date")["y10"].sort_index()
    y10r = y10.reindex(pe.index.union(y10.index)).ffill().reindex(pe.index)
    erp = (100 / pe - y10r).dropna()
    erp.name = "erp_a500"
    return erp


def eval_ladder(p: float, cur: float, cfg: dict, cp: float | None) -> float:
    """宽基仓位阶梯：返回新的目标仓位。
    kind=pct_low/abs_low：值低=便宜；kind=abs_high：值高=便宜。
    confirm 只限制进场/加仓，不限制缩仓/清仓。"""
    t1, cap = cfg["t1"], cfg["cap"]
    full, enter, halve, exit_ = cfg["lines"]
    kind = cfg["kind"]
    if kind in ("pct_low", "abs_low"):
        if p >= exit_:
            return 0.0
        if p >= halve:
            return min(cur, t1)
        if p >= enter:
            return cur
        # 便宜区：confirm 把关进场
        ok_t1 = cp is None or cp <= cfg["confirm"][2]
        ok_full = cp is None or cp <= cfg["confirm"][1]
        if p < full:
            if ok_full:
                return cap
            return max(cur, t1) if ok_t1 else cur
        return max(cur, t1) if ok_t1 else cur
    else:  # abs_high（ERP，高=便宜）
        if p <= exit_:
            return 0.0
        if p <= halve:
            return min(cur, t1)
        if p < enter:
            return cur
        if p >= full:
            return cap
        return max(cur, t1)


def run_backtest_combo(panel: pd.DataFrame, base_weights: dict,
                       dy_sig: pd.DataFrame, signals: dict, cfg: dict,
                       start_date: str, label: str,
                       sp500_cfg: dict | None = None):
    """组合层回测。返回(净值, 再平衡日志, 宽基决策, 择时决策, 交易明细, 总费用, 标普决策)
    sp500_cfg: 可选，标普500 ERP 防御层 {"sig": 序列key, "cut": 减仓线, "restore": 回补线,
    "floor": 低配仓位}——ERP ≤ cut → sp500 降到 floor（钱停泊货币ETF），
    ERP ≥ restore → 回到基准仓位。"""
    wide_sig = signals[cfg["sig"]]
    confirm_sig = signals[cfg["confirm"][0]] if "confirm" in cfg else None
    wide_leg = cfg["leg"]
    dates = panel.index[panel.index >= pd.Timestamp(start_date)]
    weights = dict(base_weights)
    weights[wide_leg] = 0.0
    holdings = {leg: 0 for leg in weights}
    cash = INITIAL_CAPITAL
    total_fees = 0.0
    rebal_log, a500_log, dy_log, trade_log, sp500_log = [], [], [], [], []

    hlb_t = base_weights["hlb"]     # 择时层当前红利低波档位（季度步进）
    wide_t = 0.0                    # 宽基层当前仓位档（月度阶梯）
    sp500_t = base_weights["sp500"]  # 标普500 ERP 防御层当前仓位档（月度滞回）

    def apply_targets():
        """由 hlb_t / wide_t / sp500_t 推导全部目标权重。
        宽基出资先货币后国债；宽基减仓时两者自动恢复择时层应有水平。
        标普500 减仓的钱停泊货币ETF（可被宽基层优先征用）。"""
        bond_base = base_weights["bond10"] - (hlb_t - base_weights["hlb"])
        cash_base = base_weights[CASH_LEG] + (base_weights["sp500"] - sp500_t)
        from_cash = min(cash_base, wide_t)                    # 先卖货币
        from_bond = min(max(bond_base - BOND_FLOOR, 0.0), wide_t - from_cash)   # 再卖国债(限下限)
        shortfall = wide_t - from_cash - from_bond            # 货币+国债都不够的部分
        weights["hlb"] = round(hlb_t - shortfall, 4)          # 差额从红利低波让出（不会发生，兜底）
        weights["bond10"] = round(bond_base - from_bond, 4)
        weights[CASH_LEG] = round(cash_base - from_cash, 4)
        weights["sp500"] = sp500_t
        weights[wide_leg] = wide_t

    def assets_on(date):
        return cash + sum(holdings[leg] * panel.loc[date, leg] for leg in holdings)

    def rebalance(date, reason):
        nonlocal cash, total_fees
        total = assets_on(date)
        prices = panel.loc[date]
        fees = 0.0

        def _log(leg, side, shares, px):
            trade_log.append({"date": date, "leg": leg, "name": LEGS[leg]["name"],
                              "side": side, "shares": shares, "price": round(px, 4),
                              "amount": round(shares * px, 2)})

        for leg, w in weights.items():
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * w
            if cur_val > tgt_val:
                sh = int((cur_val - tgt_val) / prices[leg] / 100) * 100
                if 0 < sh <= holdings[leg]:
                    proceeds, fee = sell_shares(leg, prices[leg], sh)
                    holdings[leg] -= sh
                    cash += proceeds
                    fees += fee
                    _log(leg, "卖", sh, prices[leg])
        if CASH_LEG in weights and holdings[CASH_LEG] > 0:
            tgt_cash = total * weights[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val > tgt_cash:
                sh = min(holdings[CASH_LEG],
                         int((etf_val - tgt_cash) / prices[CASH_LEG] / 100) * 100)
                if sh > 0:
                    proceeds, _ = sell_shares(CASH_LEG, prices[CASH_LEG], sh)
                    holdings[CASH_LEG] -= sh
                    cash += proceeds
                    _log(CASH_LEG, "卖", sh, prices[CASH_LEG])
        for leg, w in weights.items():
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * w
            if tgt_val > cur_val:
                sh, spent = buy_shares(leg, prices[leg], tgt_val - cur_val, cash)
                if sh:
                    holdings[leg] += sh
                    cash -= spent
                    fees += spent - sh * (prices[leg] + LEGS[leg]["spread"])
                    _log(leg, "买", sh, prices[leg])
        if CASH_LEG in weights:
            tgt_cash = total * weights[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val < tgt_cash and cash > 0:
                sh, spent = buy_shares(CASH_LEG, prices[CASH_LEG], tgt_cash - etf_val, cash)
                if sh:
                    holdings[CASH_LEG] += sh
                    cash -= spent
                    _log(CASH_LEG, "买", sh, prices[CASH_LEG])
        total_fees += fees
        rebal_log.append({"date": date, "reason": reason, "fees": round(fees, 2)})

    apply_targets()
    rebalance(dates[0], "期初建仓")

    # 检查日：每月最后一个交易日；其中 3/6/9/12 月为季度检查日（择时层）
    monthly_checks, quarterly_checks = [], []
    for y in range(dates[0].year, dates[-1].year + 1):
        for m in range(1, 13):
            md = dates[(dates.year == y) & (dates.month == m)]
            if len(md):
                monthly_checks.append(md[-1])
                if m in (3, 6, 9, 12):
                    quarterly_checks.append(md[-1])

    rows = []
    for date in dates:
        if date in monthly_checks and date != dates[0]:
            note = []
            # 1) 择时层（仅季度检查日）：真实股息率档位 ±5pp 步进
            if date in quarterly_checks:
                sig = dy_sig[dy_sig.index <= date]
                if not sig.empty:
                    pct_dy = sig["pct"].iloc[-1]
                    adj = sig["adjust"].iloc[-1]
                    desired = base_weights["hlb"] + adj
                    new_hlb = round(hlb_t + max(-MAX_STEP, min(MAX_STEP, desired - hlb_t)), 4)
                    if new_hlb != hlb_t:
                        dy_log.append({"date": date, "dy_pct": round(pct_dy, 4),
                                       "band_adj": adj, "hlb_target": new_hlb})
                        hlb_t = new_hlb
                        note.append(f"DY分位{pct_dy:.0%}→红利{hlb_t:.0%}")
            # 2) 宽基层（每月检查日）：五档阶梯（信号口径由 cfg 决定）
            s = wide_sig[wide_sig.index <= date]
            if not s.empty:
                p = s.iloc[-1]
                cp = None
                if confirm_sig is not None:
                    cs = confirm_sig[confirm_sig.index <= date]
                    cp = cs.iloc[-1] if not cs.empty else None
                new_a = eval_ladder(p, wide_t, cfg, cp)
                if new_a != wide_t:
                    action = {0.0: "清仓", cfg["cap"]: "满仓"}.get(new_a)
                    if action is None:
                        action = "建仓" if wide_t == 0 else ("加仓" if new_a > wide_t else "缩仓")
                    a500_log.append({"date": date, "signal": round(p, 4),
                                     "action": action, "target": new_a})
                    wide_t = new_a
                    note.append(f"宽基信号{p:.2f}→{action}{new_a:.0%}")
            # 3) 标普500 ERP 防御层（每月检查日，滞回两档）
            if sp500_cfg is not None:
                es = sp500_cfg["sig"][sp500_cfg["sig"].index <= date]
                if not es.empty:
                    e = es.iloc[-1]
                    new_s = sp500_t
                    if e <= sp500_cfg["cut"]:
                        new_s = sp500_cfg["floor"]
                    elif e >= sp500_cfg["restore"]:
                        new_s = base_weights["sp500"]
                    if new_s != sp500_t:
                        sp500_log.append({"date": date, "erp": round(e, 4),
                                          "action": "减仓" if new_s < sp500_t else "回补",
                                          "target": new_s})
                        sp500_t = new_s
                        note.append(f"标普ERP{e:.2f}→{'减仓' if new_s < sp500_t else '回补'}{new_s:.0%}")
            apply_targets()
            # 4) 阈值纪律：偏离 ≥5pp 才调仓
            total = assets_on(date)
            prices = panel.loc[date]

            def _leg_val(leg):
                if leg == CASH_LEG:
                    return cash + holdings[leg] * prices[leg]
                return holdings[leg] * prices[leg]

            dev = max(abs(_leg_val(leg) / total - w) for leg, w in weights.items())
            if dev >= REBAL_BAND:
                rebalance(date, f"偏离{dev*100:.1f}pp({';'.join(note)})" if note
                          else f"偏离{dev*100:.1f}pp")
        rows.append({"date": date, "assets": assets_on(date)})
    eq = pd.DataFrame(rows)
    eq["nav"] = eq["assets"] / INITIAL_CAPITAL
    return (eq, pd.DataFrame(rebal_log), pd.DataFrame(a500_log),
            pd.DataFrame(dy_log), pd.DataFrame(trade_log), total_fees,
            pd.DataFrame(sp500_log))


def run_sample(panel: pd.DataFrame, dy_sig: pd.DataFrame, signals: dict,
               start_date: str, tag: str):
    """单样本对比：基线 / 真实DY择时 / 各组合变体（进取版）"""
    backtest.START_DATE = start_date
    backtest_timing.START_DATE = start_date
    w = V3_WEIGHTS

    eq_b, log_b, fees_b = run_backtest(panel, w, "aggressive")
    eq_t, log_t, dec_t, tr_t, fees_t = run_backtest_timed(panel, w, dy_sig, "aggressive")

    results = {"基线": (eq_b, log_b, fees_b),
               "真实DY择时": (eq_t, log_t, fees_t)}
    a500_logs = {}
    for name, cfg in VARIANTS_A500.items():
        eq, log, alog, dlog, trades, fees, _splog = run_backtest_combo(
            panel, w, dy_sig, signals, cfg, start_date, name)
        results[name] = (eq, log, fees)
        a500_logs[name] = alog
        eq.to_csv(os.path.join(OUT, f"combo_nav_{name}_{tag}.csv"), index=False,
                  encoding="utf-8-sig")
        log.to_csv(os.path.join(OUT, f"combo_rebalance_{name}_{tag}.csv"), index=False,
                   encoding="utf-8-sig")
        trades.to_csv(os.path.join(OUT, f"combo_trades_{name}_{tag}.csv"), index=False,
                      encoding="utf-8-sig")
        if not alog.empty:
            alog.to_csv(os.path.join(OUT, f"combo_a500_decisions_{name}_{tag}.csv"),
                        index=False, encoding="utf-8-sig")
        if not dlog.empty:
            dlog.to_csv(os.path.join(OUT, f"combo_dy_decisions_{name}_{tag}.csv"),
                        index=False, encoding="utf-8-sig")

    print("\n" + "=" * 100)
    print(f"组合层对比（进取版，{start_date[:4]}-01 ~ {panel.index[-1].date()}，100万，{tag}）")
    print("=" * 100)
    print(f"{'版本':<18}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>7}")
    for name, (eq, log, fees) in results.items():
        m = metrics(eq)
        print(f"{name:<18}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%"
              f"{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{len(log):>5}{fees:>7,.0f}")

    # 年度收益明细
    years = sorted({y for eq, _, _ in results.values() for y in yearly(eq)})
    ymap = {name: yearly(eq) for name, (eq, _, _) in results.items()}
    yrows = []
    print(f"\n年度收益（{tag}）:")
    hdr = f"{'年份':<6}" + "".join(f"{c:>16}" for c in results)
    print(hdr)
    for y in years:
        row = {"年份": y}
        line = f"{y:<6}"
        for name in results:
            v = ymap[name].get(y)
            row[name] = round(v, 1) if v is not None else None
            line += f"{(f'{v:+.1f}%' if v is not None else '-'):>16}"
        yrows.append(row)
        print(line)
    pd.DataFrame(yrows).to_csv(os.path.join(OUT, f"combo_yearly_{tag}.csv"),
                               index=False, encoding="utf-8-sig")

    # 宽基决策记录
    for name, alog in a500_logs.items():
        if not alog.empty:
            print(f"\n宽基决策记录（{name}，{tag}）:")
            print(alog.to_string(index=False))
    return results


def main():
    print("构建信号：真实股息率择时 + 宽基信号（A500拼接分位 / 沪深300分位 / 绝对PE锚 / 绝对ERP锚）...")
    dy_sig = build_dy_signal()
    signals = {
        "a500": load_a500_signal(),
        "s300": build_hs300_signal(),
        "pe_a500": build_a500_pe_series(),
        "erp_a500": build_a500_erp_series(),
    }
    for k, s in signals.items():
        print(f"  {k:<10} {s.index[0].date()} ~ {s.index[-1].date()}，最新 {s.iloc[-1]:.2f}")

    print("\n加载短样本面板（2020 起，真实行情段）...")
    panel_s = load_panel()
    panel_s[A500_LEG] = load_a500_series(panel_s.index)
    panel_s = panel_s[panel_s.index >= pd.Timestamp("2020-01-01")]

    print("加载长样本面板（2015 起，拼接重建段）...")
    panel_l = build_panel_long()
    panel_l[A500_LEG] = load_a500_series(panel_l.index, hs300=panel_l["hs300"])
    panel_l = panel_l[panel_l.index >= pd.Timestamp("2015-01-01")]

    run_sample(panel_s, dy_sig, signals, "2020-01-01", "短样本")
    run_sample(panel_l, dy_sig, signals, "2015-01-01", "长样本")

    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
