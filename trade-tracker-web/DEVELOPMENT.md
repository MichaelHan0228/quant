# 开发逻辑文档 — 网页版股票交易记录工具（trade-tracker-web）

## 1. 项目背景与目标

本项目是一个**零依赖、单机自用**的网页版股票交易记录与统计工具。统计口径逐条还原《股票魔法师》（Mark Minervini）中的两张表：

- **月度交易记录表**：按月份统计平均收益、平均亏损、成功百分比、总交（总交易笔数）、最大收益、最大亏损、上涨日、下跌日，并附一行"平均值"。
- **交易总结**：全年的成功百分比、平均收益、平均亏损、收益/风险比、调整后的收益/风险比。

目标是替代手工维护 Excel：在浏览器里录入交易和每日盈亏，随时查看与书上一致口径的报表，并支持 CSV / Markdown 导出。用户自用，不考虑多用户、权限、联网部署。

## 2. 统计口径逐条说明

### 2.1 单笔交易收益率

```
收益率 = (卖出价 - 买入价) / 买入价 × 100%
```

- 未平仓（`sell_date` / `sell_price` 为 NULL）的交易收益率为 `None`，不参与任何统计。
- 实现：`core.trade_return_pct()`（core.py:55-59）。

### 2.2 按卖出日归月

月度统计以**卖出日期**所在月份归属：某笔 1 月买入、2 月卖出的交易计入 2 月。持仓中的交易不计入任何月份。实现上先按 `substr(sell_date,1,4)` 过滤年份（`core.closed_trades()`，core.py:179-184），再按 `sell_date` 的前缀 `YYYY-MM` 分到 12 个月。

### 2.3 月表各列定义

对应 `MONTH_HEADERS`（core.py:301-302）：

| 列 | 定义 |
|---|---|
| 月份 | `YYYY-MM`，固定输出 1–12 月 |
| 平均收益 | 该月所有**盈利**平仓交易收益率的算术平均；无盈利交易显示 `-` |
| 平均亏损 | 该月所有**亏损**平仓交易收益率绝对值的算术平均（正数显示）；无亏损交易显示 `-` |
| 成功百分比 | 盈利笔数 / 总平仓笔数 × 100% |
| 总交 | 该月平仓总笔数（含收益率为 0 的交易） |
| 最大收益 | 该月盈利交易中的最大单笔收益率；无则 `-` |
| 最大亏损 | 该月亏损交易中的最大单笔亏损（绝对值）；无则 `-` |
| 上涨日 | 该月每日盈亏记录中 `pnl > 0` 的天数 |
| 下跌日 | 该月每日盈亏记录中 `pnl < 0` 的天数 |

计算集中在 `core._basic_stats()`（core.py:187-201）与 `core.monthly_stats()`（core.py:204-240）。

### 2.4 平均值行

月表最后一行"平均值"**只统计有数据的月份**，且按列分别判断：

- `成功百分比`：只对 `总交 > 0` 的月份取平均；
- `平均收益` / `最大收益`：只对该列非空（有盈利交易）的月份取平均；
- `平均亏损` / `最大亏损`：只对该列非空（有亏损交易）的月份取平均；
- `上涨日` / `下跌日`：只对有每日盈亏记录（`n_days > 0`）的月份取平均；
- 某列全年都没有数据时，平均值行为 `-`。

实现：`monthly_stats()` 内的 `col_avg()`（core.py:223-238）。

### 2.5 无盈利月显示 "-"

某月有交易但全部亏损（无盈利交易）时，`平均收益`、`最大收益` 为 `None`，所有展示层（网页、CSV、Markdown）统一渲染为 `-`；`成功百分比` 正常显示 `0.00%`。数据层用 `None` 表达"无数据"，渲染层通过 `fmt_pct()` / 前端 `fmtPct()` 转 `-`，保证口径一致。

### 2.6 成功百分比

```
成功百分比 = 盈利笔数（收益率 > 0）/ 总平仓笔数 × 100%
```

收益率为 0 的交易计入分母（总交），但不计入分子。

### 2.7 收益/风险比

```
收益/风险比 = 平均收益 / 平均亏损（绝对值）
```

即全年平均盈利幅度是平均亏损幅度的多少倍。任一侧无数据时为 `None`（显示 `-`）。实现：`core.year_summary()`（core.py:243-271）。

### 2.8 调整后的收益/风险比

剔除**单笔最大收益**和**单笔最大亏损**后重新计算平均收益、平均亏损，再求比值：

```
调整后平均收益 = (所有盈利 - 最大一笔盈利) 的平均
调整后平均亏损 = (所有亏损 - 最大一笔亏损) 的平均
调整后的收益/风险比 = 调整后平均收益 / 调整后平均亏损
```

用于剔除一次"运气单"对整体水平的干扰。剔除后某一侧为空时结果为 `None`。

### 2.9 上涨日 / 下跌日

只看 `daily_pnl` 表中每日盈亏记录的**正负号**：

- `pnl > 0` → 上涨日
- `pnl < 0` → 下跌日
- `pnl = 0`（平盘）两侧都不计入

`kind` 字段（金额 / 百分比）不影响统计，只看符号。注意前端渲染时，某月完全没有盈亏记录（`n_days == 0`）显示 0，而有记录时显示实际计数。

### 2.10 收益率为 0 的交易

收益率恰为 0 的平仓交易：**计入总交**（参与成功百分比的分母），但**既不算盈利也不算亏损**（不进平均收益、平均亏损、最大收益、最大亏损的样本）。有专门的单元测试覆盖（`ZeroReturnTest`，test_tracker.py:128-140）。

## 3. 架构说明

### 3.1 为什么零依赖

用户机器网络受限（公司内网），安装 pip 包不可靠；且工具需要在**断网环境可用**。因此：

- 后端只用 Python 标准库（`http.server` / `sqlite3` / `json` / `csv`），Python 3.9+ 即可运行；
- 前端是单个 `static/index.html`，原生 HTML/CSS/JS，无构建步骤、无 CDN 引用；
- 数据库用 SQLite 单文件（`trades.db`），备份 = 复制文件。

### 3.2 模块划分

```
trade-tracker-web/
├── core.py            # 统计与存储引擎：schema、增删改查、统计计算、CSV/Markdown 导出
├── server.py          # HTTP 后端：http.server 路由、参数校验、JSON 响应、错误包装
├── static/index.html  # 单页应用：5 个标签页 + 平仓对话框，原生 fetch 调 API
├── run.py             # 启动入口：python run.py [端口]，默认 8000，自动开浏览器
├── start.bat          # Windows 双击启动：chcp 65001 + cd /d + start python run.py
├── test_tracker.py    # unittest：统计口径单元测试 + API 冒烟测试
└── trades.db          # SQLite 数据（不入库，见 .gitignore）
```

分层原则：`core.py` 不 import `server.py`，统计逻辑可脱离 HTTP 单独测试；`server.py` 只做"HTTP ↔ core 函数"的薄适配。

### 3.3 数据流

```
浏览器表单 → fetch → server.py 路由（校验/解析 JSON）
                   → core.py 业务函数（日期校验、约束检查）
                   → SQLite trades.db（写入/读取）
报表请求    → core.monthly_stats / year_summary / build_report
                   → JSON → 前端渲染表格
导出请求    → core.export_csv / export_markdown → 文本下载
```

每次请求新建连接（`core.connect()` 里 `executescript(SCHEMA)` 保证表存在），请求结束随 `with` 关闭，无需连接池。

## 4. 数据库 Schema

定义在 `core.SCHEMA`（core.py:18-36），SQLite，连接时 `CREATE TABLE IF NOT EXISTS` 自动建表。

### 4.1 trades（交易记录）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| code | TEXT NOT NULL | 股票代码 |
| name | TEXT NOT NULL DEFAULT '' | 名称（可选） |
| buy_date | TEXT NOT NULL | 买入日期，`YYYY-MM-DD` |
| buy_price | REAL NOT NULL | 买入价 |
| sell_date | TEXT | 卖出日期；**NULL 表示持仓中** |
| sell_price | REAL | 卖出价；与 sell_date 同时有值或同时为 NULL |
| shares | INTEGER | 股数（可选，仅记录用，不参与收益率计算） |
| note | TEXT NOT NULL DEFAULT '' | 备注 |

### 4.2 daily_pnl（每日盈亏）

| 字段 | 类型 | 说明 |
|---|---|---|
| day | TEXT PRIMARY KEY | 日期 `YYYY-MM-DD`，主键 → 同日重复保存即覆盖 |
| pnl | REAL NOT NULL | 盈亏数值；统计只看正负号 |
| kind | TEXT NOT NULL DEFAULT 'amount' | `amount`（金额）或 `pct`（涨跌幅 %），仅展示用 |
| note | TEXT NOT NULL DEFAULT '' | 备注 |

## 5. API 列表与契约

所有 API 以 JSON 交互（`ensure_ascii=False`，中文不转义）。统一错误格式：

```json
{"error": "错误信息"}
```

HTTP 状态码约定：参数/业务错误（`ValueError`）→ 400；路径不存在 → 404；未捕获异常 → 500；创建成功 → 201；其余成功 → 200。

### 5.1 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/`、`/index.html` | 返回 `static/index.html`（text/html; charset=utf-8） |

### 5.2 交易

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/trades` | 返回 `{"trades": [...]}`，按买入日期排序。每条含 `id, code, name, buy_date, buy_price, sell_date, sell_price, shares, note, open, return_pct`（`open` 布尔，`return_pct` 未平仓为 null） |
| POST | `/api/trades` | 录入交易。JSON body：`code`（必填）、`buy_date`（必填，`YYYY-MM-DD`）、`buy_price`（必填）、`name` / `sell_date` / `sell_price` / `shares` / `note`（可选）。`sell_date` 与 `sell_price` 必须同时提供或同时留空，否则 400。返回 201 `{"id": n, "ok": true}` |
| PATCH | `/api/trades/{id}` | 平仓。JSON body：`sell_date`（必填）、`sell_price`（必填）。已平仓的交易再次平仓返回 400。返回 `{"ok": true, "trade": {...}}`（含更新后的 `return_pct`） |
| DELETE | `/api/trades/{id}` | 删除交易。ID 不存在返回 400。返回 `{"ok": true}` |

### 5.3 每日盈亏

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/daily` | 返回 `{"days": [...]}`，每条含 `day, pnl, kind, note` |
| POST | `/api/daily` | 记录某日盈亏。JSON body：`day`（必填）、`pnl`（必填）、`kind`（`amount`/`pct`，默认 `amount`）、`note`（可选）。同日重复提交**覆盖**旧记录（SQLite `ON CONFLICT(day) DO UPDATE`）。返回 201 `{"ok": true}` |
| DELETE | `/api/daily/{YYYY-MM-DD}` | 删除某日记录。不存在返回 400。返回 `{"ok": true}` |

### 5.4 报表与导入导出

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/report?year=YYYY` | `year` 必须是 4 位数字，否则 400。返回 `{"year", "months": [12 个月度统计], "avg_row", "summary"}`。月度统计字段：`month, n_trades, n_wins, win_pct, avg_win, avg_loss, max_win, max_loss, up_days, down_days, n_days`（无数据为 null）；`summary` 另含 `ratio, adj_avg_win, adj_avg_loss, adj_ratio` |
| GET | `/api/export?year=YYYY&format=csv\|md` | 导出年度报表。`format=csv` 返回 `text/csv`，**带 UTF-8 BOM**；`format=md` 返回 `text/markdown`。其他 format 返回 400 |
| POST | `/api/import` | CSV 文本导入（body 直接为 CSV 内容，`utf-8-sig` 解码兼容 BOM）。表头：`code,name,buy_date,buy_price,sell_date,sell_price,shares,note`。`sell_date`/`sell_price` 留空 = 持仓中。返回 `{"imported": n, "ok": true}` |

## 6. 前端页面结构与交互逻辑

`static/index.html` 单文件单页应用，顶部固定"今日小抄"条 + 5 个标签页（切换时按需加载数据）：

1. **录入交易**：表单提交 `POST /api/trades`。卖出日期与卖出价同时留空 = 持仓中，同时填写 = 直接录入已平仓交易。
2. **交易列表 / 持仓**：`GET /api/trades` 渲染表格，持仓中行底色黄色。每行有"平仓"（仅持仓行）和"删除"（confirm 确认）按钮。平仓用 `<dialog>` 弹窗，默认卖出日期填当天，提交 `PATCH /api/trades/{id}` 后提示收益率。
3. **每日盈亏**：表单 `POST /api/daily`（同日覆盖）；下方表格列出全部记录，可删除。
4. **月度报表**：选年份 → `GET /api/report`，渲染月度记录表（13 行含平均值行）和交易总结表。
5. **导入导出**：选择本地 CSV 文件 → 读取文本 → `POST /api/import`；导出为两个 `<a>` 链接直接指向 `/api/export`。

通用机制：

- `api()` 封装 fetch：非 2xx 时抛出 `data.error` 信息；`showMsg()` 在页面顶部显示 4 秒绿/红提示条。
- 配色遵循 A 股习惯：**红 = 盈利，绿 = 亏损**（`.pos` / `.neg`）。
- "今日小抄"：加载时拉取当年 `/api/report`，展示成功百分比、收益/风险比、调整后收益/风险比、平仓笔数；录入/平仓/删除后自动刷新。
- 空值渲染：前端 `fmtPct` / `fmtNum` / `fmtDay` 把 `null` 显示为 `-`，与后端 `fmt_pct` 等函数口径一致。
- 用户输入经 `esc()` 转义后插入 HTML，防注入。

## 7. 关键实现决策与坑

- **中文路径**：项目放在 `D:\研究\quant\` 下。所有文件路径在 Python 内用 `os.path` 基于 `__file__` 推导（`core.DEFAULT_DB`、`server.INDEX_HTML`），不硬编码盘符，因此中文路径无影响；`start.bat` 用 `cd /d "%~dp0"` 切到脚本所在目录，同样兼容中文路径。
- **Windows 编码**：控制台默认 GBK 会导致中文输出乱码。措施：`start.bat` 首行 `chcp 65001`；`run.py` 与 `server.py` 的日志输出、测试脚本都 `sys.stdout.reconfigure(encoding="utf-8")`（try/except 兜底）；HTTP 响应统一显式 `charset=utf-8`。
- **CSV 导出加 BOM**：Windows Excel 直接双击无 BOM 的 UTF-8 CSV 会把中文解析为 GBK 乱码，因此 `server.py` 在 `/api/export?format=csv` 输出前加 `\ufeff`（core.py 的注释也注明 BOM 由 server 层加）。导入侧用 `utf-8-sig` 解码，兼容带 BOM 的文件。
- **无盈利月的 null → "-"**：数据层严格用 `None` 表示"无数据"而非 0（0% 和"没有盈利交易"是两回事），三层渲染（网页 / CSV / Markdown）各自只负责把 `None` 显示为 `-`。有单测验证（`test_no_win_month_shows_dash`）。
- **重复平仓报错**：`close_trade()` 先查 `sell_date`，已非 NULL 则抛 `ValueError`（API 层转 400），防止误操作覆盖历史卖出数据。
- **同日盈亏覆盖**：`daily_pnl.day` 为主键，`record_day()` 用 `INSERT ... ON CONFLICT(day) DO UPDATE`，同一天重复保存即更新——页面上也明确提示"同一天重复保存会覆盖旧记录"。
- **卖出日期与卖出价成对约束**：`add_trade()` 检查二者必须同时有值或同时为空，防止出现"有卖出价无卖出日"的脏数据（无法归月）。
- **前端渲染月表上涨/下跌日的细节**：某月 `n_days == 0`（无任何盈亏记录）时显示 0 而非 `-`，与书上手写表格习惯一致（没记录就是 0 天）。
- **ThreadingHTTPServer**：避免单线程下浏览器并发请求（页面 + API）互相阻塞；本地自用足够。
- **api() 的错误提取**：后端统一返回 `{"error": msg}`，前端统一从该字段取错误文案，不做各自为政的错误解析。

## 8. 测试策略与用例清单

`test_tracker.py`，标准库 `unittest`，零依赖。所有测试用临时文件数据库（`tempfile`），不触碰真实 `trades.db`。运行：`python test_tracker.py`。

### 8.1 统计口径单元测试（core 层）

- `MonthlyStatsTest.test_normal_month`：正常月份（+10%、+5%、-4%）的总交、成功百分比（66.67%）、平均收益（7.50%）、平均亏损（4.00%）、最大收益/亏损。
- `MonthlyStatsTest.test_no_win_month_shows_dash`：全亏月的平均收益/最大收益为 `None` 且渲染为 `-`，成功百分比 0.00% 正常显示。
- `MonthlyStatsTest.test_up_down_days`：上涨日 2、下跌日 1，平盘（pnl=0）两侧不计。
- `MonthlyStatsTest.test_avg_row`：平均值行只对有数据的月份取平均（平均收益只取有盈利的 1 月；平均亏损取 1、2 两月）。
- `YearSummaryTest.test_summary`：全年成功百分比（2/5=40%）、平均收益、平均亏损、收益/风险比（≈1.73）。
- `YearSummaryTest.test_adjusted_ratio`：剔除最大收益（+10%）与最大亏损（-6%）后重算，调整后比值 5/3.5≈1.43。
- `ZeroReturnTest`：收益率恰为 0 的交易计入总交与分母，但不算盈亏。
- `ImportTest`：CSV 文本导入（含持仓中行），导入笔数与字段正确性。
- `CloseTradeTest`：持仓 → 平仓 → 收益率正确；重复平仓抛 `ValueError`。
- `DeleteTradeTest`：删除交易；删除不存在的 ID 抛错。

### 8.2 API 冒烟测试

`ApiSmokeTest.test_full_flow`：`threading` 起真实 HTTP 服务（端口 0 随机）+ `urllib` 走完整流程——首页 200；POST 录入（持仓/已平仓）；PATCH 平仓返回收益率；重复平仓 400；GET 列表与 `open` 标志；POST/GET 每日盈亏；GET 报表校验 1 月全部列与 summary（含剔除后亏损侧为空时 `adj_ratio` 为 null）；CSV 导入；DELETE 交易；缺买入价 400、非法年份 400；导出 Markdown 含"月度交易记录/交易总结"、导出 CSV 按 `utf-8-sig` 解码验证 BOM 兼容。

## 9. 与旧 CLI 版的关系及数据库迁移

旧版是命令行工具 `C:/Users/hanxudong/trade-tracker/`（`tracker.py` + 同名 `trades.db`），统计口径已在使用中验证过。网页版的 `core.py` 统计逻辑**直接移植**自旧版，仅做三处改动：

1. 去掉 CLI / 终端渲染部分；
2. CSV 导入从"读文件"改为"接受文本"（`import_csv_text`），以配合网页上传；
3. 新增 `delete_trade()`。

两版 SQLite schema 完全相同（trades / daily_pnl 两表），因此**迁移 = 直接复制旧版 `trades.db` 覆盖本项目目录下的同名文件**，无需任何转换。反过来把网页版的库拷回旧版也兼容。旧版的 `output/`（报表输出目录）属于 CLI 版特有，网页版不使用。

## 10. 后续可扩展方向（TODO）

- [ ] 交易编辑（修改买入信息/备注），目前只能删除重录
- [ ] 按股票代码 / 备注筛选与搜索交易列表
- [ ] 多年对比视图与累计收益曲线图（仍可零依赖，用 canvas 手绘或内嵌 SVG）
- [ ] 持仓市值估算（手动录入现价，计算浮动盈亏）
- [ ] 数据库一键备份按钮（复制 trades.db 到带日期戳的文件）
- [ ] 导入时跳过重复记录（按 code+buy_date 判重）
- [ ] 交易总结表补充书中其余指标（如平均持股天数）
