#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — 网页版交易记录工具后端（仅标准库 http.server + sqlite3 + json）

所有 API 返回 JSON；/ 与 /index.html 提供前端单页。
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, "static", "index.html")


def _read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


class Handler(BaseHTTPRequestHandler):
    db_path = core.DEFAULT_DB
    server_version = "TradeTracker/1.0"

    # -- 基础工具 -----------------------------------------------------------

    def log_message(self, fmt, *args):  # 保持简洁的一行日志
        import sys
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _body_json(self):
        try:
            return json.loads(_read_body(self).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法的 JSON")

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        parts = urlparse(self.path)
        return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}

    # -- 路由 ----------------------------------------------------------------

    def do_GET(self):
        path, qs = self._query()
        try:
            if path in ("/", "/index.html"):
                with open(INDEX_HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/trades":
                with core.connect(self.db_path) as conn:
                    self._json({"trades": core.list_trades(conn)})
            elif path == "/api/daily":
                with core.connect(self.db_path) as conn:
                    self._json({"days": core.list_days(conn)})
            elif path == "/api/report":
                year = qs.get("year") or ""
                if not re.fullmatch(r"\d{4}", year):
                    return self._error(400, "参数 year 必须是 4 位年份，如 ?year=2026")
                with core.connect(self.db_path) as conn:
                    self._json(core.build_report(conn, year))
            elif path == "/api/export":
                year = qs.get("year") or ""
                fmt = qs.get("format", "csv")
                if not re.fullmatch(r"\d{4}", year):
                    return self._error(400, "参数 year 必须是 4 位年份，如 ?year=2026")
                with core.connect(self.db_path) as conn:
                    if fmt == "md":
                        self._send(200, core.export_markdown(conn, year),
                                   "text/markdown; charset=utf-8")
                    elif fmt == "csv":
                        # 加 BOM 方便 Excel 直接打开
                        self._send(200, "\ufeff" + core.export_csv(conn, year),  # BOM 便于 Excel
                                   "text/csv; charset=utf-8")
                    else:
                        self._error(400, "format 只支持 csv 或 md")
            else:
                self._error(404, "路径不存在: %s" % path)
        except ValueError as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001 - API 统一兜底
            self._error(500, "服务器内部错误: %s" % e)

    def do_POST(self):
        path, _ = self._query()
        try:
            if path == "/api/trades":
                d = self._body_json()
                code = str(d.get("code") or "").strip()
                if not code:
                    return self._error(400, "代码 code 不能为空")
                if d.get("buy_price") is None:
                    return self._error(400, "买入价 buy_price 不能为空")
                sell_date = (d.get("sell_date") or "").strip() or None
                sell_price = d.get("sell_price")
                sell_price = float(sell_price) if sell_price not in (None, "") else None
                shares = d.get("shares")
                shares = int(shares) if shares not in (None, "") else None
                with core.connect(self.db_path) as conn:
                    tid = core.add_trade(
                        conn, code, str(d.get("name") or "").strip(),
                        str(d.get("buy_date") or "").strip(), float(d["buy_price"]),
                        sell_date, sell_price, shares, str(d.get("note") or "").strip())
                self._json({"id": tid, "ok": True}, 201)
            elif path == "/api/daily":
                d = self._body_json()
                day = str(d.get("day") or "").strip()
                if d.get("pnl") is None:
                    return self._error(400, "盈亏金额 pnl 不能为空")
                kind = d.get("kind", "amount")
                if kind not in ("amount", "pct"):
                    return self._error(400, "kind 只支持 amount 或 pct")
                with core.connect(self.db_path) as conn:
                    core.record_day(conn, day, float(d["pnl"]), kind,
                                    str(d.get("note") or "").strip())
                self._json({"ok": True}, 201)
            elif path == "/api/import":
                raw = _read_body(self)
                text = raw.decode("utf-8-sig", errors="replace")
                with core.connect(self.db_path) as conn:
                    n = core.import_csv_text(conn, text)
                self._json({"imported": n, "ok": True})
            else:
                self._error(404, "路径不存在: %s" % path)
        except ValueError as e:
            self._error(400, str(e))
        except (KeyError, TypeError) as e:
            self._error(400, "参数错误: %s" % e)
        except Exception as e:  # noqa: BLE001
            self._error(500, "服务器内部错误: %s" % e)

    def do_PATCH(self):
        path, _ = self._query()
        m = re.fullmatch(r"/api/trades/(\d+)", path)
        try:
            if m:
                d = self._body_json()
                sell_date = str(d.get("sell_date") or "").strip()
                if d.get("sell_price") is None:
                    return self._error(400, "卖出价 sell_price 不能为空")
                with core.connect(self.db_path) as conn:
                    core.close_trade(conn, int(m.group(1)), sell_date, float(d["sell_price"]))
                    row = conn.execute("SELECT * FROM trades WHERE id=?",
                                       (int(m.group(1)),)).fetchone()
                self._json({"ok": True, "trade": core.trade_to_dict(row)})
            else:
                self._error(404, "路径不存在: %s" % path)
        except ValueError as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._error(500, "服务器内部错误: %s" % e)

    def do_DELETE(self):
        path, _ = self._query()
        try:
            m = re.fullmatch(r"/api/trades/(\d+)", path)
            if m:
                with core.connect(self.db_path) as conn:
                    core.delete_trade(conn, int(m.group(1)))
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/daily/(\d{4}-\d{2}-\d{2})", path)
            if m:
                with core.connect(self.db_path) as conn:
                    core.delete_day(conn, m.group(1))
                return self._json({"ok": True})
            self._error(404, "路径不存在: %s" % path)
        except ValueError as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._error(500, "服务器内部错误: %s" % e)


def make_server(host="127.0.0.1", port=8000, db_path=None):
    """创建 HTTP 服务（不启动）。db_path 默认项目目录下 trades.db。"""
    if db_path:
        Handler.db_path = db_path
    return ThreadingHTTPServer((host, port), Handler)


def run(host="127.0.0.1", port=8000, db_path=None, open_browser=True):
    import threading
    import webbrowser
    srv = make_server(host, port, db_path)
    url = "http://%s:%d/" % (host, srv.server_address[1])
    print("交易记录工具已启动: %s" % url)
    print("数据库: %s" % Handler.db_path)
    print("按 Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    run()
