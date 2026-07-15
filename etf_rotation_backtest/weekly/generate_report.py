import json

with open('output/report_data_v8.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

ETF_NAMES = {
    '518880': '黄金ETF', '510880': '红利ETF', '513500': '标普500ETF',
    '513180': '恒生科技ETF', '510300': '沪深300ETF', '159915': '创业板ETF'
}

# 交易明细表格
trades_html = ''
for t in d.get('trades_list', []):
    color = '#4caf50' if t['action'] == '买入' else '#f44336'
    name = t.get('name', ETF_NAMES.get(t['code'], t['code']))
    trades_html += f'<tr><td>{t["date"]}</td><td style="color:{color}">{t["action"]}</td><td>{name}({t["code"]})</td><td>{t["price"]}</td><td>{t["shares"]}</td><td>{t["commission"]}</td></tr>\n'

# 年度收益表格
yearly_html = ''
for y in d['yearly']:
    color = '#4caf50' if y['return'] >= 0 else '#f44336'
    yearly_html += f'<tr><td>{y["year"]}</td><td style="color:{color}">{y["return"]:+.1f}%</td></tr>\n'

html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>ETF轮动策略 V8 回测报告</title>
<style>
body {{ font-family: 'Microsoft YaHei', sans-serif; background: #1a1a2e; color: #eee; margin: 20px; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ color: #e94560; text-align: center; }}
h2 {{ color: #0f3460; background: #16213e; padding: 10px 20px; border-radius: 5px; }}
.metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin: 20px 0; }}
.metric-card {{ background: #16213e; padding: 20px; border-radius: 10px; text-align: center; }}
.metric-value {{ font-size: 28px; font-weight: bold; color: #e94560; }}
.metric-label {{ font-size: 14px; color: #aaa; margin-top: 5px; }}
.positive {{ color: #4caf50; }}
.negative {{ color: #f44336; }}
.chart-container {{ background: #16213e; padding: 20px; border-radius: 10px; margin: 20px 0; }}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }}
th {{ background: #0f3460; }}
.improvements {{ background: #2d4a2d; padding: 15px; border-radius: 8px; margin: 15px 0; border-left: 4px solid #4caf50; }}
.improvements li {{ margin: 5px 0; }}
.version-compare {{ background: #16213e; padding: 15px; border-radius: 8px; margin: 15px 0; }}
.version-compare td {{ text-align: center; }}
.highlight {{ background: #2d4a2d; }}
.etf-table {{ background: #16213e; padding: 15px; border-radius: 8px; margin: 15px 0; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
<div class="container">
<h1>🔄 ETF轮动策略 V8 回测报告</h1>
<h2 style="text-align:center;color:#e94560;background:none;">6个ETF + 4层止损/止盈 + 等权重分配</h2>

<div class="improvements">
<strong>✅ 策略特性：</strong>
<ul>
<li>6个ETF轮动：黄金/红利/标普500/恒生科技/沪深300/创业板</li>
<li>4层止损/止盈体系：成本价-8% / 分档移动止盈 / 组合回撤-10% / 暴跌反弹</li>
<li>分档移动止盈：低波ETF(-10%)更早锁定利润，高波ETF(-20%)给足空间</li>
<li>日频止损：每天检查，次日执行</li>
<li>等权重分配：持仓2只各50%，3只各33%</li>
<li>数据缓存：本地CSV，回测结果可复现</li>
</ul>
</div>

<div class="etf-table">
<h3>📊 ETF池</h3>
<table>
<tr><th>ETF</th><th>代码</th><th>资产类别</th><th>特点</th><th>止盈阈值</th></tr>
<tr><td>黄金ETF</td><td>518880</td><td>避险资产</td><td>低波动</td><td>-10%</td></tr>
<tr><td>红利ETF</td><td>510880</td><td>A股价值</td><td>中低波动</td><td>-12%</td></tr>
<tr><td>标普500ETF</td><td>513500</td><td>美股宽基</td><td>中波动</td><td>-15%</td></tr>
<tr><td>恒生科技ETF</td><td>513180</td><td>港股科技</td><td>高波动</td><td>-20%</td></tr>
<tr><td>沪深300ETF</td><td>510300</td><td>A股宽基</td><td>中低波动</td><td>-12%</td></tr>
<tr><td>创业板ETF</td><td>159915</td><td>A股成长</td><td>高波动</td><td>-18%</td></tr>
</table>
</div>

<p style="text-align:center;color:#aaa;">回测区间: {d["nav_dates"][0]} ~ {d["nav_dates"][-1]} | 初始资金: 100,000元 | 调仓频率: 周频</p>

<h2>📈 核心指标</h2>
<div class="metrics">
  <div class="metric-card">
    <div class="metric-value positive">+{d["total_return"]}%</div>
    <div class="metric-label">总收益</div>
  </div>
  <div class="metric-card">
    <div class="metric-value positive">+{d["annual_return"]}%</div>
    <div class="metric-label">年化收益</div>
  </div>
  <div class="metric-card">
    <div class="metric-value negative">{d["max_drawdown"]}%</div>
    <div class="metric-label">最大回撤</div>
  </div>
  <div class="metric-card">
    <div class="metric-value">{d["sharpe"]}</div>
    <div class="metric-label">夏普比率</div>
  </div>
  <div class="metric-card">
    <div class="metric-value">{d["win_rate"]}%</div>
    <div class="metric-label">周胜率</div>
  </div>
  <div class="metric-card">
    <div class="metric-value">{d["calmar"]}</div>
    <div class="metric-label">Calmar比率</div>
  </div>
</div>

<div class="metrics" style="grid-template-columns: repeat(4, 1fr);">
  <div class="metric-card">
    <div class="metric-value">{d["trades"]}</div>
    <div class="metric-label">交易次数</div>
  </div>
  <div class="metric-card">
    <div class="metric-value">{d["commission"]:,.0f}元</div>
    <div class="metric-label">总佣金</div>
  </div>
  <div class="metric-card">
    <div class="metric-value">+{d["benchmark_return"]}%</div>
    <div class="metric-label">基准(沪深300)</div>
  </div>
  <div class="metric-card">
    <div class="metric-value positive">+{d["excess_return"]}%</div>
    <div class="metric-label">超额收益</div>
  </div>
</div>

<h2>📈 净值曲线（策略 vs 基准）</h2>
<div class="chart-container">
  <canvas id="navChart" height="300"></canvas>
</div>

<h2>📅 年度收益</h2>
<table class="year-table">
<tr><th>年份</th><th>收益</th></tr>
{yearly_html}
</table>

<h2>🔄 版本对比</h2>
<table class="version-compare">
<tr><th>版本</th><th>总收益</th><th>年化</th><th>回撤</th><th>夏普</th></tr>
<tr><td>无暴跌止损</td><td>+42.4%</td><td>+12.2%</td><td>-7.0%</td><td>0.94</td></tr>
<tr><td>全部ETF暴跌止损</td><td>+40.5%</td><td>+11.7%</td><td>-8.2%</td><td>0.88</td></tr>
<tr><td>仅恒科暴跌止损(周五检查)</td><td>+45.8%</td><td>+13.1%</td><td>-9.7%</td><td>1.02</td></tr>
<tr><td>仅恒科+日频止损</td><td>+48.0%</td><td>+13.6%</td><td>-7.1%</td><td>1.19</td></tr>
<tr><td>日频止损+移动止盈(统一-12%)</td><td>+54.0%</td><td>+15.1%</td><td>-7.4%</td><td>1.24</td></tr>
<tr><td>日频止损+分档止盈(5个ETF)</td><td>+58.2%</td><td>+16.1%</td><td>-7.4%</td><td>1.38</td></tr>
<tr class="highlight"><td><strong>V8 6个ETF+分档止盈</strong></td><td><strong>+{d["total_return"]}%</strong></td><td><strong>+{d["annual_return"]}%</strong></td><td><strong>{d["max_drawdown"]}%</strong></td><td><strong>{d["sharpe"]}</strong></td></tr>
</table>

<h2>📋 交易明细（共{d["trades"]}笔）</h2>
<div style="max-height:400px;overflow-y:auto;">
<table>
<tr><th>日期</th><th>操作</th><th>ETF</th><th>价格</th><th>数量</th><th>佣金</th></tr>
{trades_html}
</table>
</div>

<h2>⚠️ 风险提示</h2>
<div style="background:#4a2d2d;padding:15px;border-radius:8px;border-left:4px solid #f44336;">
<ul>
<li>回测不等于实盘，历史表现不代表未来收益</li>
<li>数据仅3年，未经历完整牛熊周期</li>
<li>QDII ETF（标普500/恒生科技）有溢价风险</li>
<li>日频止损仍基于收盘价，盘中暴跌无法捕捉</li>
<li>移动止盈存在跳空缺口风险（如黄金ETF 2026-02回撤17%才触发）</li>
</ul>
</div>

<p style="text-align:center;color:#666;margin-top:40px;">生成时间: 2026-07-15 | 策略版本: V8 6个ETF + 分档止盈</p>
</div>

<script>
const navDates = {json.dumps(d['nav_dates'])};
const navValues = {json.dumps(d['nav_values'])};
const benchDates = {json.dumps(d['bench_dates'])};
const benchValues = {json.dumps(d['bench_values'])};

new Chart(document.getElementById('navChart'), {{
  type: 'line',
  data: {{
    labels: navDates,
    datasets: [
      {{
        label: '策略净值',
        data: navValues,
        borderColor: '#e94560',
        backgroundColor: 'rgba(233,69,96,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }},
      {{
        label: '基准(沪深300)',
        data: benchValues,
        borderColor: '#4caf50',
        borderDash: [5,5],
        fill: false,
        tension: 0.3,
        pointRadius: 0,
      }}
    ]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ labels: {{ color: '#eee' }} }},
    }},
    scales: {{
      x: {{
        ticks: {{ color: '#aaa', maxTicksLimit: 10 }},
        grid: {{ color: '#333' }}
      }},
      y: {{
        ticks: {{ color: '#aaa' }},
        grid: {{ color: '#333' }}
      }}
    }}
  }}
}});
</script>
</body>
</html>'''

with open('output/backtest_report_v8.html', 'w', encoding='utf-8') as f:
    f.write(html)
print('报告已生成: output/backtest_report_v8.html')
