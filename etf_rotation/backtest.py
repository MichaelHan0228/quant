"""回测引擎：T日收盘算分 → T+1开盘价调仓，阈值机制，复利净值。

支持两个降回撤扩展（在 Params 里配置）：
  - top_k > 1：持仓评分前 k 名，等权，剩余为现金（分散持仓）
  - cash_rule：'none' 不过滤（原版）/ 'score' 总分<=0 不买（绝对动量）/
               'ma' 收盘价低于 MA(ma_n) 不买（趋势过滤）
    未通过的标的会被剔除，空出的仓位持有现金（收益按 0 计）。

调仓阈值（文章原意："第二名超过第一名的1.5倍才换仓"）：
z-score 加权分可能为负，直接乘 1.5 对负数会反向，统一实现为
    challenger > current + (threshold-1) * |current|
k>1 时同理：挑战者须超过最弱持仓该边际才能挤入。
"""
import numpy as np
import pandas as pd
from pathlib import Path

from factors import Params


def _need_switch(best: str, best_score: float, holding: str, hold_score: float,
                 threshold: float) -> bool:
    if best == holding:
        return False
    margin = (threshold - 1.0) * abs(hold_score)
    return best_score > hold_score + margin


def _platform_series(close: pd.Series, volume: pd.Series, params: Params) -> pd.Series:
    """逐日计算平台位（只用 T 日及之前数据，无前视）。

    swing:      最近 anchor_n 天最低点=当前波段起点，起点前 box_n 天为箱体，
                箱体高度超 platform_max_h 视为无平台（NaN）；位 = 箱底+q×箱高。
    prevhigh:   同样锚定起点，位 = 起点前 ph_n 天最高收盘（前高），无高度上限。
    volprofile: 最近 vp_n 天按收盘价分桶、按成交量加权，取当前价下方
                成交量最大的桶（POC）中点为平台位；当前价下方无量则 NaN。
    """
    mode = params.platform_mode
    n = len(close)
    vals = np.full(n, np.nan)
    c = close.values
    v = volume.values if volume is not None else None
    A = params.platform_anchor_n
    for t in range(n):
        if t < A + 20:
            continue
        if mode in ("swing", "prevhigh"):
            win = c[t - A + 1:t + 1]
            if np.isnan(win).any():
                continue
            leg = t - A + 1 + int(np.argmin(win))  # 波段起点（窗口内最低价）
            lo = max(0, leg - (params.platform_box_n if mode == "swing"
                               else params.platform_ph_n))
            box = c[lo:leg]
            box = box[~np.isnan(box)]
            if len(box) < 20:
                continue
            hi, lo_ = float(box.max()), float(box.min())
            if mode == "swing":
                if hi / lo_ - 1 > params.platform_max_h:
                    continue  # 箱体太宽=不是平台
                vals[t] = lo_ + params.platform_q * (hi - lo_)
            else:
                vals[t] = hi
        elif mode == "volprofile":
            wc = c[t - params.platform_vp_n + 1:t + 1]
            wv = v[t - params.platform_vp_n + 1:t + 1]
            mask = ~np.isnan(wc) & ~np.isnan(wv)
            wc, wv = wc[mask], wv[mask]
            if len(wc) < 40 or wc.max() <= wc.min():
                continue
            bins = np.linspace(wc.min(), wc.max(), params.platform_vp_bins + 1)
            hist = np.zeros(params.platform_vp_bins)
            idx = np.clip(np.digitize(wc, bins) - 1, 0, len(hist) - 1)
            np.add.at(hist, idx, wv)
            below = bins[:-1] < c[t]          # 当前价下方的桶
            if not below.any() or hist[below].sum() <= 0:
                continue
            poc = int(np.argmax(np.where(below, hist, -1)))
            vals[t] = (bins[poc] + bins[poc + 1]) / 2
    return pd.Series(vals, index=close.index)


def _target_weights(row: pd.Series, holdings: dict, ma_ok: dict,
                    params: Params, exclude: set = None,
                    no_buy: set = None) -> dict:
    """根据当日评分和过滤规则，计算目标持仓 {code: weight}（等权 1/top_k，余为现金）。

    row 中 NaN（标的未上市/无数据）视为不在池内，直接剔除。
    no_buy 中的标的禁止作为新挑战者买入，但已有持仓保留（用于 QDII 溢价过滤）。
    """
    rule = params.cash_rule
    exclude = exclude or set()
    no_buy = no_buy or set()
    row = row.dropna()
    if rule == "none":
        passed = dict(row)
    elif rule == "score":
        passed = {c: s for c, s in row.items() if s > 0}
    elif rule == "ma":
        passed = {c: s for c, s in row.items() if ma_ok.get(c, False)}
    elif rule == "ma_strict":
        top = row.idxmax()
        passed = {top: row[top]} if ma_ok.get(top, False) else {}
    else:
        raise ValueError(f"未知 cash_rule: {rule}")
    for c in exclude:
        passed.pop(c, None)

    k = params.top_k
    survivors = [c for c in holdings if c in passed]
    cands = sorted((c for c in passed if c not in survivors and c not in no_buy),
                   key=lambda c: -passed[c])
    if params.threshold_exit is None:
        # 原版单一阈值：先填空位（无需阈值：没有挤压任何持仓）
        while len(survivors) < k and cands:
            survivors.append(cands.pop(0))
        # 位置已满：挑战者须超过最弱持仓 + 阈值边际才能挤入
        while cands and survivors:
            weakest = min(survivors, key=lambda c: row[c])
            if _need_switch(cands[0], row[cands[0]], weakest, row[weakest],
                            params.threshold):
                survivors.remove(weakest)
                survivors.append(cands.pop(0))
            else:
                break
    else:
        # 非对称阈值：换出松（最强挑战者超过在持者+exit边际即踢出），
        # 换入严（候选须超过最弱在持者+enter边际才补位，否则留现金）
        te, tn = params.threshold_exit, params.threshold
        if survivors and cands:
            best_c = cands[0]
            survivors = [h for h in survivors
                         if not _need_switch(best_c, row[best_c], h, row[h], te)]
        while len(survivors) < k and cands:
            if not survivors:  # 全部空仓时直接取最强候选
                survivors.append(cands.pop(0))
                continue
            weakest = min(survivors, key=lambda c: row[c])
            if _need_switch(cands[0], row[cands[0]], weakest, row[weakest], tn):
                survivors.append(cands.pop(0))
            else:
                break
    if params.weight_mode == "score":
        # 分数加权：负分截断为 0（该标的让位给现金），总仓位 = len/k 不变
        clipped = {c: max(row[c], 0.0) for c in survivors}
        tot = sum(clipped.values())
        scale = len(survivors) / k
        if tot > 0:
            return {c: clipped[c] / tot * scale for c in survivors}
    w = 1.0 / k
    return {c: w for c in survivors}


def run_backtest(scores: pd.DataFrame, panel: dict, params: Params,
                 start: str = "2019-01-01") -> dict:
    dates = scores.index
    # 并集日历下标的未上市/停牌日为 NaN：上市后 ffill 补齐（停牌按价格不变处理），
    # 上市前保持 NaN（该标的不可能持仓，不会参与计算）
    opens = pd.DataFrame({c: df["open"] for c, df in panel.items()}).reindex(dates).ffill()
    closes = pd.DataFrame({c: df["close"] for c, df in panel.items()}).reindex(dates).ffill()
    ma_ok = None
    if params.cash_rule in ("ma", "ma_strict"):
        ma = closes.rolling(params.ma_n, min_periods=1).mean()
        ma_ok = closes > ma
    atr = None
    if params.atr_mult > 0 or params.atr_mult_map:
        # Wilder 平滑 ATR
        highs = pd.DataFrame({c: df["high"] for c, df in panel.items()}).reindex(dates).ffill()
        lows = pd.DataFrame({c: df["low"] for c, df in panel.items()}).reindex(dates).ffill()
        tr = pd.concat([highs - lows, (highs - closes.shift()).abs(),
                        (lows - closes.shift()).abs()]).groupby(level=0).max()
        atr = tr.ewm(alpha=1 / params.atr_n, min_periods=params.atr_n).mean()
    vols = None
    if params.vol_n > 0:
        # 逆波动率加权：T日及之前 vol_n 个交易日的日收益率标准差（无前视）
        vols = closes.pct_change().rolling(params.vol_n).std()
        if params.vol_smooth > 0:
            # EMA 平滑（半衰期 vol_smooth 个交易日），降低目标权重的日度跳动
            vols = vols.ewm(halflife=params.vol_smooth).mean()

    platforms = None
    if params.platform_mode != "none":
        platforms = {c: _platform_series(closes[c], df["volume"].reindex(dates),
                                         params)
                     for c, df in panel.items()}
    premium = None
    if params.premium_limit > 0:
        # QDII 溢价率序列（data/{code}_premium.csv，由 fetch_premium.py 生成）
        pcols = {}
        for c in panel:
            f = Path(__file__).parent / "data" / f"{c}_premium.csv"
            if f.exists():
                pcols[c] = pd.read_csv(f, dtype={"date": str},
                                       index_col="date")["premium"]
        if pcols:
            premium = pd.DataFrame(pcols).reindex(dates).ffill()

    # 当日可选标的 ≥2 才构成有效轮动池（z-score 至少需要2个样本）
    valid = scores.index[scores.notna().sum(axis=1) >= 2]
    start_pos = max(dates.searchsorted(start), dates.searchsorted(valid[0]) + 1)

    holdings: dict = {}   # code -> weight
    nav = 1.0
    nav_idx, nav_val, cash_idx = [], [], []
    trades = []
    entry_nav = {}        # code -> 建仓时净值
    round_trips = []
    pending = None
    peaks = {}            # code -> 持仓期最高收盘（移动止损用）
    entry_px = {}         # code -> 建仓开盘价（浮盈收紧止损用）
    banned = {}           # code -> 冷却截止的日期下标

    for i in range(start_pos, len(dates)):
        d = dates[i]
        # ---- 昨收 → 今开（沿用旧持仓）----
        if i > start_pos and holdings:
            inv = sum(w * opens.at[d, c] / closes.at[dates[i - 1], c]
                      for c, w in holdings.items())
            nav *= (1 - sum(holdings.values())) + inv
        # ---- 开盘：执行昨日信号 ----
        if pending is not None:
            turnover = sum(abs(pending.get(c, 0) - holdings.get(c, 0))
                           for c in set(pending) | set(holdings))
            if turnover > 1e-9:
                nav *= 1 - params.commission * turnover
                for c in sorted(set(pending) | set(holdings)):
                    old, new = holdings.get(c, 0), pending.get(c, 0)
                    if new > old:
                        trades.append((d, "买入", c, opens.at[d, c], new, nav))
                        entry_nav[c] = nav
                        peaks[c] = opens.at[d, c]
                        entry_px[c] = opens.at[d, c]
                    elif old > new:
                        trades.append((d, "卖出", c, opens.at[d, c], old, nav))
                        if new == 0:
                            peaks.pop(c, None)
                            entry_px.pop(c, None)
                            if c in entry_nav:
                                round_trips.append(nav / entry_nav.pop(c) - 1)
                holdings = pending
            pending = None
        # ---- 今开 → 今收 ----
        if holdings:
            inv = sum(w * closes.at[d, c] / opens.at[d, c]
                      for c, w in holdings.items())
            nav *= (1 - sum(holdings.values())) + inv
        cash_idx.append(1 - sum(holdings.values()))
        # ---- 收盘：移动止损检查（触发则次日开盘清仓并进入冷却）----
        stopped = set()
        if (params.stop_pct > 0 or params.atr_mult > 0
                or params.atr_mult_map or platforms is not None):
            for c in holdings:
                peaks[c] = max(peaks.get(c, closes.at[d, c]), closes.at[d, c])
                hit = False
                in_profit = params.profit_trigger > 0 and c in entry_px and \
                    peaks[c] / entry_px[c] - 1 >= params.profit_trigger
                # 浮盈收紧止损：峰值收益 ≥ profit_trigger 后改用 profit_stop 止损线
                stop_c = params.profit_stop if in_profit else params.stop_pct
                lines = []
                if stop_c > 0:
                    lines.append(peaks[c] * (1 - stop_c))
                if platforms is not None:
                    lv = platforms[c].iat[i]
                    if not pd.isna(lv):
                        lines.append(lv)
                if lines:
                    # 浮盈头寸：较低线生效（两线皆破才止损，平台不破可容忍洗盘）；
                    # 亏损头寸：较高线生效（平台破位立即止损，不等百分比线）
                    thr = min(lines) if in_profit else max(lines)
                    if closes.at[d, c] < thr:
                        hit = True
                mult = (params.atr_mult_map or {}).get(c, params.atr_mult)
                if mult > 0:
                    a = atr.at[d, c]
                    if not pd.isna(a) and \
                            closes.at[d, c] < peaks[c] - mult * a:
                        hit = True
                if hit:
                    stopped.add(c)
                    banned[c] = i + params.stop_cd
            if stopped:
                if params.stop_sell_all:
                    pending = {}
                else:
                    # 只卖破位标的：未破位持仓保留权重，空出的仓位留现金，
                    # 次日收盘的信号环节会按评分递补（破位标的在冷却期内被排除）
                    pending = {c: w for c, w in holdings.items() if c not in stopped}
        # ---- 收盘：计算次日信号 ----
        if pending is None:
            row = scores.loc[d].dropna()
            if len(row) >= 2:  # 动态池：当日可选标的 ≥2 才出信号
                ma_row = ({c: bool(ma_ok.at[d, c]) for c in row.index}
                          if ma_ok is not None else {})
                exclude = {c for c, until in banned.items() if i < until}
                no_buy = set()
                if premium is not None:
                    # 溢价超限的 QDII 禁止新买入（持仓不强卖，等溢价回落）
                    no_buy = {c for c in row.index
                              if c in premium.columns
                              and not pd.isna(premium[c].iat[i])
                              and premium[c].iat[i] > params.premium_limit}
                pending = _target_weights(row, holdings, ma_row, params,
                                          exclude, no_buy)
                if params.vol_target > 0 and pending:
                    # 组合波动率目标：策略自身近 N 日日收益年化波动超目标则按比例降仓，
                    # 0.25 步进量化防每日抖动；只用到 T-1 日及之前的净值，无前视
                    ex = 1.0
                    if len(nav_val) > params.vol_target_n:
                        r = (pd.Series(nav_val).pct_change().dropna()
                             .iloc[-params.vol_target_n:])
                        realized = float(r.std()) * np.sqrt(252)
                        if realized > 0:
                            ex = min(1.0, params.vol_target / realized)
                    ex = round(ex * 4) / 4.0
                    pending = {c: w * ex for c, w in pending.items()}
                if (params.weight_mode != "equal"
                        and set(pending) == set(holdings)):
                    # 成分不变：权重随价格漂移，不做每日再平衡（防换手爆炸）
                    pending = holdings
                if vols is not None and len(pending) >= 2:
                    # 逆波动率加权：权重 ∝ 1/vol，总仓位不变，余下仍为现金；
                    # 任一标的波动率缺失/为 0 时退回等权
                    v = {c: vols.at[d, c] for c in pending}
                    if all(pd.notna(x) and x > 0 for x in v.values()):
                        inv = {c: 1.0 / x for c, x in v.items()}
                        tot_inv = sum(inv.values())
                        scale = sum(pending.values())
                        pending = {c: inv[c] / tot_inv * scale for c in pending}
                        if (params.reb_band > 0
                                and set(pending) == set(holdings)
                                and all(abs(pending[c] - holdings[c])
                                        <= params.reb_band for c in pending)):
                            # 再平衡带：成分不变且偏离 ≤ band，维持现状不交易
                            pending = holdings
        nav_idx.append(d)
        nav_val.append(nav)

    nav_s = pd.Series(nav_val, index=pd.to_datetime(nav_idx), name="nav")
    cash_s = pd.Series(cash_idx, index=pd.to_datetime(nav_idx), name="cash_w")
    trades_df = pd.DataFrame(
        trades, columns=["date", "action", "code", "price", "weight", "nav"])
    return {"nav": nav_s, "trades": trades_df, "cash": cash_s,
            "round_trips": round_trips,
            "metrics": _metrics(nav_s, round_trips, trades_df, cash_s)}


def _metrics(nav: pd.Series, round_trips: list, trades: pd.DataFrame,
             cash: pd.Series = None) -> dict:
    ret = nav.pct_change().dropna()
    n = len(nav)
    total = nav.iloc[-1] / nav.iloc[0] - 1
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (252 / n) - 1 if n > 1 else 0
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
    dd = (nav / nav.cummax() - 1).min()
    calmar = ann / abs(dd) if dd < 0 else np.inf
    yearly = nav.groupby(nav.index.year).last()
    yearly_ret = (yearly / yearly.shift(1) - 1).dropna()
    first_year = nav.index.year.min()
    m = {
        "区间": f"{nav.index[0].date()} ~ {nav.index[-1].date()}",
        "累计收益": f"{total:.2%}",
        "年化收益": f"{ann:.2%}",
        "夏普": round(sharpe, 2),
        "最大回撤": f"{dd:.2%}",
        "卡玛": round(calmar, 2),
        "调仓次数": len(trades[trades.action == "买入"]) if len(trades) else 0,
        "持仓胜率": f"{np.mean([r > 0 for r in round_trips]):.1%}" if round_trips else "-",
        "首年收益": f"{nav[nav.index.year == first_year].iloc[-1] / nav.iloc[0] - 1:.2%}",
        "逐年收益": {int(y): f"{r:.2%}" for y, r in yearly_ret.items()},
    }
    if cash is not None:
        m["平均现金仓位"] = f"{cash.mean():.1%}"
    return m
