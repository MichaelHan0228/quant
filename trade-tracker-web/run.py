#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run.py — 启动交易记录工具（默认 127.0.0.1:8000，自动打开浏览器）"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import server

if __name__ == "__main__":
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("用法: python run.py [端口]   默认 8000")
            sys.exit(1)
    server.run(port=port)
