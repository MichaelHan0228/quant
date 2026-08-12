#!/bin/bash
# 双低策略参数网格实验 runner
# 用法: bash run_grid.sh A|B|C
set -x
cd "$(dirname "$0")"
case "$1" in
  A)  # 双低值天花板档位 + 止盈网格一部分
    python backtest.py --tag _mdl150 --max-dl 150
    python backtest.py --tag _mdl160 --max-dl 160
    python backtest.py --tag _mdl170 --max-dl 170
    python backtest.py --tag _hm140_tp20 --hold-max 140 --tp-prem 0.2
    ;;
  B)  # 止盈网格: hold-max=130 三档 + 140/0.4
    python backtest.py --tag _hm130_tp20 --hold-max 130 --tp-prem 0.2
    python backtest.py --tag _hm130_tp30 --hold-max 130 --tp-prem 0.3
    python backtest.py --tag _hm130_tp40 --hold-max 130 --tp-prem 0.4
    python backtest.py --tag _hm140_tp40 --hold-max 140 --tp-prem 0.4
    ;;
  C)  # 止盈网格: hold-max=150 三档
    python backtest.py --tag _hm150_tp20 --hold-max 150 --tp-prem 0.2
    python backtest.py --tag _hm150_tp30 --hold-max 150 --tp-prem 0.3
    python backtest.py --tag _hm150_tp40 --hold-max 150 --tp-prem 0.4
    ;;
esac
echo "GRID $1 DONE"
