"""候选ETF与现有标的池的日收益相关性扫描。

复用 data.py 的腾讯前复权K线（带CSV缓存），把候选代码临时注入 ETF_POOL，
计算各候选与池内标的的日收益 Pearson 相关性（窗口：2022-01-01 起，或候选上市后）。

用法: python scan_corr.py
结果写入 output/corr_scan.txt 并打印到终端。
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import data  # noqa: E402

POOL = ["512890", "159949", "513100", "518880", "159985"]

# 候选：代码 -> (腾讯带前缀代码, 名称)
CANDIDATES = {
    "511090": ("sh511090", "30年国债ETF"),
    "511260": ("sh511260", "十年国债ETF"),
    "511360": ("sh511360", "短融ETF"),
    "511380": ("sh511380", "可转债ETF"),
    "513500": ("sh513500", "标普500ETF"),
    "513880": ("sh513880", "日经225ETF"),
    "513030": ("sh513030", "德国ETF"),
    "513730": ("sh513730", "东南亚科技ETF"),
    "159329": ("sz159329", "沙特ETF"),
    "164824": ("sz164824", "印度基金LOF"),
    "501018": ("sh501018", "南方原油LOF"),
    "162411": ("sz162411", "华宝油气LOF"),
    "161226": ("sz161226", "国投白银LOF"),
    "515220": ("sh515220", "煤炭ETF"),
    "159865": ("sz159865", "养殖ETF"),
    "159611": ("sz159611", "电力ETF"),
}

WIN_START = "2022-01-01"  # 统一窗口起点（候选晚于该日上市的，用其上市日起）


def main():
    data.ETF_POOL.update(CANDIDATES)

    closes = {}
    for code in POOL + list(CANDIDATES):
        df = data.load_kline(code).set_index("date")
        closes[code] = df["close"]
    rets = pd.DataFrame(closes).pct_change()

    lines = []
    header = f"{'代码':<7}{'名称':<12}{'窗口起点':<12}{'样本数':>6}  " + \
             "  ".join(f"{data.ETF_POOL[c][1]:>6}" for c in POOL) + "    最大|r|  平均|r|"
    lines.append(header)
    rows = []
    for code in CANDIDATES:
        r = rets[code].dropna()
        start = max(str(r.index.min()), WIN_START)
        r = r[r.index >= start]
        corrs = {}
        for p in POOL:
            pair = pd.concat([r, rets[p]], axis=1).dropna()
            corrs[p] = pair.iloc[:, 0].corr(pair.iloc[:, 1]) if len(pair) > 30 else float("nan")
        max_abs = max(abs(v) for v in corrs.values())
        avg_abs = sum(abs(v) for v in corrs.values()) / len(corrs)
        rows.append((code, start, len(r), corrs, max_abs, avg_abs))

    rows.sort(key=lambda x: x[4])
    for code, start, n, corrs, max_abs, avg_abs in rows:
        name = CANDIDATES[code][1]
        line = f"{code:<7}{name:<12}{start:<12}{n:>6}  " + \
               "  ".join(f"{corrs[p]:>8.2f}" for p in POOL) + \
               f"    {max_abs:>6.2f}  {avg_abs:>6.2f}"
        lines.append(line)

    lines.append("")
    lines.append(f"窗口: {WIN_START} 起（上市晚于该日的用上市日）；日收益 Pearson 相关；")
    lines.append("列 = 池内标的（红利低波/创业板50/纳指/黄金/豆粕）。")
    lines.append("经验阈值: 最大|r| < 0.3 可视为低相关候选；0.3~0.5 边际；> 0.5 同质化。")
    out = "\n".join(lines)

    (Path(__file__).parent / "output").mkdir(exist_ok=True)
    (Path(__file__).parent / "output" / "corr_scan.txt").write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
