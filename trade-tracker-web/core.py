#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core.py — 统计与存储引擎（按《股票魔法师》Mark Minervini 统计口径）

逻辑直接移植自已验证的命令行版 C:/Users/hanxudong/trade-tracker/tracker.py，
仅去掉 CLI/终端渲染部分，import 改为接受 CSV 文本，另增 delete_trade。
仅使用 Python 标准库（sqlite3 / csv），兼容 Python 3.9+。
"""
import csv
import io
import os
import sqlite3
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    buy_date   TEXT    NOT NULL,          -- YYYY-MM-DD
    buy_price  REAL    NOT NULL,
    sell_date  TEXT,                      -- NULL 表示持仓中
    sell_price REAL,
    shares     INTEGER,
    note       TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS daily_pnl (
    day  TEXT PRIMARY KEY,                -- YYYY-MM-DD
    pnl  REAL NOT NULL,                   -- 金额或百分比，统计时只看正负号
    kind TEXT NOT NULL DEFAULT 'amount',  -- amount | pct
    note TEXT NOT NULL DEFAULT ''
);
"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def valid_date(s):
    datetime.strptime(s, "%Y-%m-%d")
    return s


def trade_return_pct(t):
    """单笔收益率：(卖出价-买入价)/买入价 * 100。未平仓返回 None。"""
    if t["sell_price"] is None or t["sell_date"] is None:
        return None
    return (t["sell_price"] - t["buy_price"]) / t["buy_price"] * 100.0


def trade_to_dict(t):
    """sqlite Row -> dict，附带收益率，供 API/前端使用。"""
    return {
        "id": t["id"],
        "code": t["code"],
        "name": t["name"],
        "buy_date": t["buy_date"],
        "buy_price": t["buy_price"],
        "sell_date": t["sell_date"],
        "sell_price": t["sell_price"],
        "shares": t["shares"],
        "note": t["note"],
        "open": t["sell_date"] is None,
        "return_pct": trade_return_pct(t),
    }


# ---------------------------------------------------------------------------
# 数据读写
# ---------------------------------------------------------------------------

def add_trade(conn, code, name, buy_date, buy_price,
              sell_date=None, sell_price=None, shares=None, note=""):
    valid_date(buy_date)
    if sell_date:
        valid_date(sell_date)
    if (sell_date is None) != (sell_price is None):
        raise ValueError("卖出日期与卖出价必须同时提供或同时留空")
    cur = conn.execute(
        "INSERT INTO trades (code, name, buy_date, buy_price, sell_date, sell_price, shares, note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (code, name or "", buy_date, buy_price, sell_date, sell_price, shares, note or ""),
    )
    conn.commit()
    return cur.lastrowid


def close_trade(conn, trade_id, sell_date, sell_price):
    valid_date(sell_date)
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        raise ValueError("找不到 ID 为 %s 的交易" % trade_id)
    if row["sell_date"] is not None:
        raise ValueError("ID %s 已平仓（卖出日期 %s），不能重复平仓" % (trade_id, row["sell_date"]))
    conn.execute("UPDATE trades SET sell_date=?, sell_price=? WHERE id=?",
                 (sell_date, sell_price, trade_id))
    conn.commit()


def delete_trade(conn, trade_id):
    cur = conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError("找不到 ID 为 %s 的交易" % trade_id)


def list_trades(conn):
    rows = conn.execute("SELECT * FROM trades ORDER BY buy_date, id").fetchall()
    return [trade_to_dict(t) for t in rows]


def record_day(conn, day, pnl, kind="amount", note=""):
    valid_date(day)
    conn.execute(
        "INSERT INTO daily_pnl (day, pnl, kind, note) VALUES (?,?,?,?)"
        " ON CONFLICT(day) DO UPDATE SET pnl=excluded.pnl, kind=excluded.kind, note=excluded.note",
        (day, pnl, kind, note or ""),
    )
    conn.commit()


def delete_day(conn, day):
    cur = conn.execute("DELETE FROM daily_pnl WHERE day=?", (day,))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError("找不到日期为 %s 的盈亏记录" % day)


def list_days(conn):
    rows = conn.execute("SELECT * FROM daily_pnl ORDER BY day").fetchall()
    return [dict(r) for r in rows]


def import_csv_text(conn, text):
    """从 CSV 文本导入。表头: code,name,buy_date,buy_price,sell_date,sell_price,shares,note"""
    n = 0
    # utf-8-sig 由调用方处理；这里兼容带 BOM 的文本
    if text.startswith("\ufeff"):
        text = text[1:]
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("code") or "").strip()
        if not code:
            continue
        sell_date = (row.get("sell_date") or "").strip() or None
        sell_price = (row.get("sell_price") or "").strip()
        sell_price = float(sell_price) if sell_price else None
        shares = (row.get("shares") or "").strip()
        shares = int(shares) if shares else None
        add_trade(
            conn,
            code=code,
            name=(row.get("name") or "").strip(),
            buy_date=(row.get("buy_date") or "").strip(),
            buy_price=float(row["buy_price"]),
            sell_date=sell_date,
            sell_price=sell_price,
            shares=shares,
            note=(row.get("note") or "").strip(),
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# 统计计算（书中口径）
# ---------------------------------------------------------------------------

def closed_trades(conn, year):
    rows = conn.execute(
        "SELECT * FROM trades WHERE sell_date IS NOT NULL AND substr(sell_date,1,4)=?",
        (str(year),),
    ).fetchall()
    return [(t, trade_return_pct(t)) for t in rows]


def _basic_stats(returns):
    """returns: 该期所有平仓交易收益率列表。返回按书中口径计算的各指标。"""
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    n = len(returns)
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    return {
        "n_trades": n,
        "n_wins": len(wins),
        "win_pct": (len(wins) / n * 100.0) if n else None,
        "avg_win": avg(wins),                       # 无盈利交易 -> None（显示 "-"）
        "avg_loss": avg([-r for r in losses]),      # 绝对值，正数显示
        "max_win": max(wins) if wins else None,
        "max_loss": max([-r for r in losses]) if losses else None,
    }


def monthly_stats(conn, year):
    """返回 12 个月的统计列表 + 平均值行。上涨/下跌日来自 daily_pnl。"""
    trades = closed_trades(conn, year)
    days = conn.execute(
        "SELECT day, pnl FROM daily_pnl WHERE substr(day,1,4)=?", (str(year),)
    ).fetchall()

    months = []
    for m in range(1, 13):
        key = "%04d-%02d" % (int(year), m)
        rets = [r for t, r in trades if t["sell_date"].startswith(key)]
        st = _basic_stats(rets)
        d = [x["pnl"] for x in days if x["day"].startswith(key)]
        st["month"] = key
        st["up_days"] = sum(1 for x in d if x > 0)
        st["down_days"] = sum(1 for x in d if x < 0)
        st["n_days"] = len(d)
        months.append(st)

    def col_avg(field, pred):
        vals = [st[field] for st in months if pred(st)]
        return sum(vals) / len(vals) if vals else None

    avg_row = {
        "month": "平均值",
        "n_trades": None,
        "n_wins": None,
        # 各列仅对有交易的月份取平均；"-"（无数据）的月份不参与
        "win_pct": col_avg("win_pct", lambda s: s["n_trades"] > 0),
        "avg_win": col_avg("avg_win", lambda s: s["avg_win"] is not None),
        "avg_loss": col_avg("avg_loss", lambda s: s["avg_loss"] is not None),
        "max_win": col_avg("max_win", lambda s: s["max_win"] is not None),
        "max_loss": col_avg("max_loss", lambda s: s["max_loss"] is not None),
        "up_days": col_avg("up_days", lambda s: s["n_days"] > 0),
        "down_days": col_avg("down_days", lambda s: s["n_days"] > 0),
    }
    return months, avg_row


def year_summary(conn, year):
    """交易总结（全年）。含 收益/风险比 与 调整后的收益/风险比。"""
    trades = closed_trades(conn, year)
    rets = [r for _, r in trades]
    st = _basic_stats(rets)

    wins = [r for r in rets if r > 0]
    losses = [-r for r in rets if r < 0]
    avg_win, avg_loss = st["avg_win"], st["avg_loss"]
    ratio = (avg_win / avg_loss) if (avg_win and avg_loss) else None

    # 调整后：剔除单笔最大收益和单笔最大亏损后重算
    adj_wins = wins[:]
    adj_losses = losses[:]
    if adj_wins:
        adj_wins.remove(max(adj_wins))
    if adj_losses:
        adj_losses.remove(max(adj_losses))
    adj_avg_win = sum(adj_wins) / len(adj_wins) if adj_wins else None
    adj_avg_loss = sum(adj_losses) / len(adj_losses) if adj_losses else None
    adj_ratio = (adj_avg_win / adj_avg_loss) if (adj_avg_win and adj_avg_loss) else None

    st.update({
        "ratio": ratio,
        "adj_avg_win": adj_avg_win,
        "adj_avg_loss": adj_avg_loss,
        "adj_ratio": adj_ratio,
    })
    return st


def build_report(conn, year):
    """供 /api/report 使用的完整报表结构。"""
    months, avg_row = monthly_stats(conn, year)
    return {
        "year": int(year),
        "months": months,
        "avg_row": avg_row,
        "summary": year_summary(conn, year),
    }


# ---------------------------------------------------------------------------
# 导出（CSV / Markdown 文本）
# ---------------------------------------------------------------------------

def fmt_pct(v):
    return "-" if v is None else "%.2f%%" % v


def fmt_num(v):
    return "-" if v is None else "%.2f" % v


def fmt_day(v):
    return "-" if v is None else ("%.1f" % v if isinstance(v, float) and v != int(v) else str(int(v)))


MONTH_HEADERS = ["月份", "平均收益", "平均亏损", "成功百分比", "总交",
                 "最大收益", "最大亏损", "上涨日", "下跌日"]


def _row_list(st, is_avg=False):
    return [
        st["month"], fmt_pct(st["avg_win"]), fmt_pct(st["avg_loss"]),
        fmt_pct(st["win_pct"]), "-" if is_avg else st["n_trades"],
        fmt_pct(st["max_win"]), fmt_pct(st["max_loss"]),
        st["up_days"] if not is_avg else fmt_day(st["up_days"]),
        st["down_days"] if not is_avg else fmt_day(st["down_days"]),
    ]


def export_csv(conn, year):
    """月度记录表 + 交易总结，单份 CSV 文本（utf-8-sig 由 server 层加 BOM）。"""
    months, avg_row = monthly_stats(conn, year)
    s = year_summary(conn, year)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["%s 年月度交易记录" % year])
    w.writerow(MONTH_HEADERS)
    for st in months:
        w.writerow(_row_list(st))
    w.writerow(_row_list(avg_row, is_avg=True))
    w.writerow([])
    w.writerow(["%s 年交易总结" % year])
    w.writerow(["指标", "值"])
    w.writerow(["成功百分比", fmt_pct(s["win_pct"])])
    w.writerow(["平均收益", fmt_pct(s["avg_win"])])
    w.writerow(["平均亏损", fmt_pct(s["avg_loss"])])
    w.writerow(["收益/风险比", fmt_num(s["ratio"])])
    w.writerow(["调整后的收益/风险比", fmt_num(s["adj_ratio"])])
    return buf.getvalue()


def export_markdown(conn, year):
    months, avg_row = monthly_stats(conn, year)
    s = year_summary(conn, year)
    md_rows = [_row_list(st) for st in months] + [_row_list(avg_row, is_avg=True)]
    md = ["# %s 年月度交易记录" % year, "",
          "| " + " | ".join(MONTH_HEADERS) + " |",
          "|" + "---|" * len(MONTH_HEADERS)]
    md += ["| " + " | ".join(str(c) for c in r) + " |" for r in md_rows]
    md += ["", "# %s 年交易总结" % year, "",
           "- 成功百分比: %s" % fmt_pct(s["win_pct"]),
           "- 平均收益: %s" % fmt_pct(s["avg_win"]),
           "- 平均亏损: %s" % fmt_pct(s["avg_loss"]),
           "- 收益/风险比: %s" % fmt_num(s["ratio"]),
           "- 调整后的收益/风险比: %s" % fmt_num(s["adj_ratio"]),
           ""]
    return "\n".join(md)
