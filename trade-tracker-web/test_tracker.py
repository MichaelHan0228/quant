#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_tracker.py — 统计口径单元测试 + API 冒烟测试（unittest，零依赖）

统计用例移植自命令行版 C:/Users/hanxudong/trade-tracker/test_tracker.py。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core
import server

# Windows 终端 UTF-8 输出（避免测试名中文乱码）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


class TrackerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.conn = core.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)


def seed_year(conn):
    """虚构全年数据（2025）：
    1月：+10%、+5%、-4%  → 成功 2/3，平均收益 7.50%，平均亏损 4.00%
    2月：-3%、-6%        → 无盈利月，成功百分比 0.00%
    1月日涨跌：2 上涨日、1 下跌日
    """
    core.add_trade(conn, "600001", "示例A", "2025-01-06", 10.0,
                   "2025-01-20", 11.0, 1000, "盈利10%")      # +10%
    core.add_trade(conn, "600002", "示例B", "2025-01-08", 20.0,
                   "2025-01-25", 21.0, 500, "盈利5%")        # +5%
    core.add_trade(conn, "600003", "示例C", "2025-01-10", 25.0,
                   "2025-01-28", 24.0, 400, "亏损4%")        # -4%
    core.add_trade(conn, "600004", "示例D", "2025-02-10", 30.0,
                   "2025-02-20", 29.1, 300, "亏损3%")        # -3%
    core.add_trade(conn, "600005", "示例E", "2025-02-12", 50.0,
                   "2025-02-25", 47.0, 200, "亏损6%")        # -6%
    core.record_day(conn, "2025-01-06", 1200.0)
    core.record_day(conn, "2025-01-07", 800.0)
    core.record_day(conn, "2025-01-08", -500.0)
    core.record_day(conn, "2025-01-09", 0.0)   # 平盘，不算上涨也不算下跌


class MonthlyStatsTest(TrackerTestBase):
    def setUp(self):
        super().setUp()
        seed_year(self.conn)

    def test_normal_month(self):
        months, _ = core.monthly_stats(self.conn, 2025)
        jan = months[0]
        self.assertEqual(jan["n_trades"], 3)
        self.assertAlmostEqual(jan["win_pct"], 2 / 3 * 100, places=2)   # 66.67%
        self.assertAlmostEqual(jan["avg_win"], 7.50, places=2)
        self.assertAlmostEqual(jan["avg_loss"], 4.00, places=2)
        self.assertAlmostEqual(jan["max_win"], 10.00, places=2)
        self.assertAlmostEqual(jan["max_loss"], 4.00, places=2)

    def test_no_win_month_shows_dash(self):
        months, _ = core.monthly_stats(self.conn, 2025)
        feb = months[1]
        self.assertEqual(feb["n_trades"], 2)
        self.assertAlmostEqual(feb["win_pct"], 0.00, places=2)
        self.assertIsNone(feb["avg_win"])    # 无盈利交易 -> 显示 "-"
        self.assertIsNone(feb["max_win"])
        self.assertAlmostEqual(feb["avg_loss"], 4.50, places=2)
        self.assertAlmostEqual(feb["max_loss"], 6.00, places=2)
        # 渲染层验证 "-"
        self.assertEqual(core.fmt_pct(feb["avg_win"]), "-")
        self.assertEqual(core.fmt_pct(feb["avg_loss"]), "4.50%")
        self.assertEqual(core.fmt_pct(feb["win_pct"]), "0.00%")

    def test_up_down_days(self):
        months, _ = core.monthly_stats(self.conn, 2025)
        jan = months[0]
        self.assertEqual(jan["up_days"], 2)
        self.assertEqual(jan["down_days"], 1)

    def test_avg_row(self):
        months, avg_row = core.monthly_stats(self.conn, 2025)
        # 平均收益：仅对有盈利的月份取平均 -> 只有 1 月 7.50%
        self.assertAlmostEqual(avg_row["avg_win"], 7.50, places=2)
        # 平均亏损：1月 4.00%、2月 4.50% -> 4.25%
        self.assertAlmostEqual(avg_row["avg_loss"], 4.25, places=2)
        # 成功百分比：有交易的月份 (66.67 + 0)/2
        self.assertAlmostEqual(avg_row["win_pct"], (2 / 3 * 100 + 0) / 2, places=2)


class YearSummaryTest(TrackerTestBase):
    def setUp(self):
        super().setUp()
        seed_year(self.conn)

    def test_summary(self):
        s = core.year_summary(self.conn, 2025)
        self.assertEqual(s["n_trades"], 5)
        self.assertAlmostEqual(s["win_pct"], 40.00, places=2)          # 2/5
        self.assertAlmostEqual(s["avg_win"], 7.50, places=2)           # (10+5)/2
        self.assertAlmostEqual(s["avg_loss"], 13 / 3, places=2)        # (4+3+6)/3
        self.assertAlmostEqual(s["ratio"], 7.5 / (13 / 3), places=2)   # 1.73

    def test_adjusted_ratio(self):
        """剔除最大收益(+10%)和最大亏损(-6%)：盈利 [5]，亏损 [4,3] -> 5/3.5 = 1.43"""
        s = core.year_summary(self.conn, 2025)
        self.assertAlmostEqual(s["adj_avg_win"], 5.00, places=2)
        self.assertAlmostEqual(s["adj_avg_loss"], 3.50, places=2)
        self.assertAlmostEqual(s["adj_ratio"], 5.0 / 3.5, places=2)


class ZeroReturnTest(TrackerTestBase):
    def test_zero_return_counts_but_neither_win_nor_loss(self):
        """收益率恰为 0：计入总交，不算盈利也不算亏损"""
        core.add_trade(self.conn, "600010", "示例G", "2025-05-06", 10.0,
                       "2025-05-20", 10.0)      # 0%
        core.add_trade(self.conn, "600011", "示例H", "2025-05-07", 10.0,
                       "2025-05-21", 11.0)      # +10%
        months, _ = core.monthly_stats(self.conn, 2025)
        may = months[4]
        self.assertEqual(may["n_trades"], 2)
        self.assertAlmostEqual(may["win_pct"], 50.0, places=2)
        self.assertAlmostEqual(may["avg_win"], 10.0, places=2)
        self.assertIsNone(may["avg_loss"])      # 无亏损交易


class ImportTest(TrackerTestBase):
    def test_import_csv_text(self):
        text = ("code,name,buy_date,buy_price,sell_date,sell_price,shares,note\n"
                "000001,平安银行,2025-03-03,10.00,2025-03-20,11.00,1000,导入盈利\n"
                "000002,万科A,2025-03-05,8.00,,,500,导入持仓中\n")
        n = core.import_csv_text(self.conn, text)
        self.assertEqual(n, 2)
        rows = self.conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1]["sell_date"])          # 持仓中
        self.assertAlmostEqual(core.trade_return_pct(rows[0]), 10.0, places=2)


class CloseTradeTest(TrackerTestBase):
    def test_close_open_trade(self):
        tid = core.add_trade(self.conn, "600009", "示例F", "2025-04-01", 10.0)
        self.assertIsNone(self.conn.execute(
            "SELECT sell_date FROM trades WHERE id=?", (tid,)).fetchone()["sell_date"])
        core.close_trade(self.conn, tid, "2025-04-15", 10.5)
        row = self.conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertAlmostEqual(core.trade_return_pct(row), 5.0, places=2)
        # 重复平仓应报错
        with self.assertRaises(ValueError):
            core.close_trade(self.conn, tid, "2025-04-16", 10.6)


class DeleteTradeTest(TrackerTestBase):
    def test_delete(self):
        tid = core.add_trade(self.conn, "600012", "示例I", "2025-06-01", 10.0)
        core.delete_trade(self.conn, tid)
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM trades WHERE id=?", (tid,)).fetchone())
        with self.assertRaises(ValueError):
            core.delete_trade(self.conn, tid)


# ---------------------------------------------------------------------------
# API 冒烟测试：threading 起服务 + urllib 请求
# ---------------------------------------------------------------------------

class ApiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        cls.db_path = cls.tmp.name
        cls.srv = server.make_server("127.0.0.1", 0, db_path=cls.db_path)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        os.unlink(cls.db_path)

    def call(self, method, path, body=None, raw=None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
            headers["Content-Type"] = "text/csv"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_full_flow(self):
        # 首页
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type"))
            self.assertIn("交易记录", resp.read().decode("utf-8"))

        # 录入 3 笔（2 盈 1 亏 + 1 持仓）
        st, r = self.call("POST", "/api/trades", {
            "code": "600001", "name": "示例A", "buy_date": "2026-01-06", "buy_price": 10.0})
        self.assertEqual(st, 201)
        tid1 = r["id"]
        st, r = self.call("POST", "/api/trades", {
            "code": "600002", "name": "示例B", "buy_date": "2026-01-08", "buy_price": 20.0,
            "sell_date": "2026-01-25", "sell_price": 21.0})     # +5%
        self.assertEqual(st, 201)
        st, r = self.call("POST", "/api/trades", {
            "code": "600003", "name": "示例C", "buy_date": "2026-01-10", "buy_price": 25.0,
            "sell_date": "2026-01-28", "sell_price": 24.0})     # -4%
        self.assertEqual(st, 201)
        tid3 = r["id"]

        # 平仓第 1 笔：+10%
        st, r = self.call("PATCH", "/api/trades/%d" % tid1, {
            "sell_date": "2026-01-20", "sell_price": 11.0})
        self.assertEqual(st, 200)
        self.assertAlmostEqual(r["trade"]["return_pct"], 10.0, places=2)
        # 重复平仓 -> 400
        st, r = self.call("PATCH", "/api/trades/%d" % tid1, {
            "sell_date": "2026-01-21", "sell_price": 11.5})
        self.assertEqual(st, 400)
        self.assertIn("error", r)

        # 列表
        st, r = self.call("GET", "/api/trades")
        self.assertEqual(st, 200)
        self.assertEqual(len(r["trades"]), 3)
        open_flags = [t["open"] for t in r["trades"]]
        self.assertEqual(open_flags.count(True), 0)  # 全部已平仓？不——第 4 笔还没录
        # 录入一笔持仓中
        st, r = self.call("POST", "/api/trades", {
            "code": "600004", "buy_date": "2026-02-02", "buy_price": 30.0})
        self.assertEqual(st, 201)
        tid4 = r["id"]
        st, r = self.call("GET", "/api/trades")
        self.assertTrue(any(t["id"] == tid4 and t["open"] for t in r["trades"]))

        # 日盈亏：2 上涨、1 下跌、1 平盘
        for day, pnl in [("2026-01-06", 1200.0), ("2026-01-07", 800.0),
                         ("2026-01-08", -500.0), ("2026-01-09", 0.0)]:
            st, r = self.call("POST", "/api/daily", {"day": day, "pnl": pnl})
            self.assertEqual(st, 201)
        st, r = self.call("GET", "/api/daily")
        self.assertEqual(len(r["days"]), 4)

        # 报表：2026 年 1 月 +10%、+5%、-4% → 同移植用例的口径
        st, r = self.call("GET", "/api/report?year=2026")
        self.assertEqual(st, 200)
        self.assertEqual(r["year"], 2026)
        self.assertEqual(len(r["months"]), 12)
        jan = r["months"][0]
        self.assertEqual(jan["month"], "2026-01")
        self.assertEqual(jan["n_trades"], 3)
        self.assertAlmostEqual(jan["win_pct"], 2 / 3 * 100, places=2)
        self.assertAlmostEqual(jan["avg_win"], 7.50, places=2)
        self.assertAlmostEqual(jan["avg_loss"], 4.00, places=2)
        self.assertAlmostEqual(jan["max_win"], 10.00, places=2)
        self.assertAlmostEqual(jan["max_loss"], 4.00, places=2)
        self.assertEqual(jan["up_days"], 2)
        self.assertEqual(jan["down_days"], 1)
        s = r["summary"]
        self.assertEqual(s["n_trades"], 3)
        self.assertAlmostEqual(s["ratio"], 7.5 / 4.0, places=2)
        # 调整后：剔除 +10% 与 -4% 后，盈利 [5]，亏损空 -> None
        self.assertAlmostEqual(s["adj_avg_win"], 5.0, places=2)
        self.assertIsNone(s["adj_ratio"])

        # CSV 导入
        st, r = self.call("POST", "/api/import",
                          raw="code,name,buy_date,buy_price,sell_date,sell_price,shares,note\n"
                              "000001,平安银行,2026-03-03,10.00,2026-03-20,11.00,1000,导入\n")
        self.assertEqual(st, 200)
        self.assertEqual(r["imported"], 1)

        # 删除
        st, r = self.call("DELETE", "/api/trades/%d" % tid4)
        self.assertEqual(st, 200)
        st, r = self.call("GET", "/api/trades")
        self.assertFalse(any(t["id"] == tid4 for t in r["trades"]))

        # 错误处理：缺买入价 -> 400；非法年份 -> 400
        st, r = self.call("POST", "/api/trades", {"code": "X"})
        self.assertEqual(st, 400)
        st, r = self.call("GET", "/api/report?year=20x6")
        self.assertEqual(st, 400)

        # 导出
        with urllib.request.urlopen(self.base + "/api/export?year=2026&format=md",
                                    timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            md = resp.read().decode("utf-8")
            self.assertIn("月度交易记录", md)
            self.assertIn("交易总结", md)
        with urllib.request.urlopen(self.base + "/api/export?year=2026&format=csv",
                                    timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("2026-01", resp.read().decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
