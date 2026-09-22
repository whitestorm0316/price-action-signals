# Web 信号仪表盘（Flask + lightweight-charts）

单页面 Web 界面，展示指定交易对的 **K 线** 与 **价格行为信号**，并提供**手动下单**入口
（当前下单为 mock，预留 OKX 模拟盘接入）。

- 复用阶段一的信号检测逻辑（`patterns.py` / `trend.py` / `atr.py` / `okx_client.py`），**不重写**。

## 功能

- 前端用 lightweight-charts（CDN 引入，无 npm / 无 React/Vue）
- K 线图：显示指定交易对的蜡烛图，横轴按**本地时区**显示（与表格一致）
- 信号价格线：**只显示当前选中信号**的入场/止损/止盈三条水平线（买入=绿色，卖出=红色，
  线型区分：入场=实线、止损=虚线、止盈=点线）
- **入场 K 线标记**：在信号所在 K 线位置画箭头（买入 ↑ 绿色、卖出 ↓ 红色），随选中信号切换
- 信号表格：列出最近信号（方向/策略/入场/止损/止盈/盈亏比/**结果**/时间），
  **点击行切换图上显示的信号**，每行一个「下单」按钮
- 表格「结果」列：复用阶段一回测逻辑，标注每个信号 **✅成功 / ❌失败 / ⏳持仓中**
- **趋势交易开关**：勾选后用均线判断大趋势，只顺势交易（升势留 BUY、跌势留 SELL），
  可调均线周期与类型（SMA/EMA）
- 手动刷新 + 可选 30s 自动轮询（无 WebSocket）
- 无用户登录，单页面

## 运行

```bash
cd price-action-signals
./.venv/bin/pip install -r requirements.txt

# 可选：拉取 K 线走代理（OKX 主站在部分网络被墙时必需）
export OKX_PROXY=http://127.0.0.1:1087

# 可选：默认交易对/周期/信号扫描窗口/趋势过滤
# export DASH_TICKER=BTC-USDT-SWAP
# export DASH_INTERVAL=5m
# export DASH_SIGNAL_SCAN=30
# export DASH_TREND_PERIOD=0    # 0=不启用趋势过滤
# export DASH_PORT=8000

./.venv/bin/python -m web_dashboard.app
# 浏览器打开 http://127.0.0.1:8000
```

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 前端页面 |
| GET | `/api/klines?ticker=BTC-USDT-SWAP&interval=5m&limit=100` | 返回 lightweight-charts 格式 `[{time,open,high,low,close}]` |
| GET | `/api/signals?ticker=...&interval=...&scan=100&trend=50&ma_type=sma` | 返回检测到的信号；`scan`=扫描窗口(最近多少根K线)，`trend`=均线周期(0/空=关)，`ma_type`=sma/ema，`entry`=close/breakout（入场方式），`gap`=同形态最小间隔根数，`atr`=止损 ATR 倍数(0=结构止损)，`mode`=pattern/trend（信号来源） |
| GET | `/api/backtest?ticker=...&bars=35040&trend=0&rr=2&atr=1.5&entry=close&gap=0&mode=trend` | 整年历史回测，参数与 `/api/signals` 同口径 |
| POST | `/api/order` | 手动下单，body `{action,ticker,price,sl,tp,size}`，**当前 mock** |

`/api/signals` 的 `result` 字段为信号结果判定（复用阶段一回测逻辑）：
`WIN`=先到止盈(成功)、`LOSS`=先触止损(失败)、`OPEN`=仍在持仓。

**`entry`（入场方式）**：`close`=形态收盘价入场（原行为，默认）；`breakout`=等价格越过
形态极值再入场（多头取最高价、空头取最低价）。

**`gap`（同形态最小间隔）**：同一形态连续出现时，距上次信号不足 `gap` 根 K 线的**忽略**。
`0`/`1`=不过滤（原行为，默认）；pinbar 与吞没**各自独立计间隔**。调大即"降频换质量"。

**`atr`（止损宽度）**：`0`=结构止损（形态极值外侧，**原有默认**）；`>0`=改用 `N×ATR(14)`。
第八轮唯一被数据支持的成分——止损越宽，手续费占 R 的比例（`f`）越低。
ATR 止损若比结构止损更贴近入场，程序取**离入场更远**的一侧，不会把风险缩到形态边界内。

**`mode`（信号来源）**：`pattern`=检测 Pinbar/吞没触发（**原有默认**）；
`trend`=**纯趋势**，只看均线斜率判方向，**完全不使用形态**。
`mode=trend` 时需配合 `trend` 周期（`trend=0` 则不发任何信号——没有周期就无从判方向，不猜）。
未知取值一律回退 `pattern`。

### 参数与安全闸门

仪表盘工具栏的**止损**（`结构止损` / `N×ATR`）、**入场**（`收盘价` / `突破极值`）、
**来源**（`形态` / `纯趋势`）、**间隔**四个控件，以及回测弹窗的对应项，默认值分别为
`0`（结构止损）、`close`、`pattern`、`0` —— **即原有行为逐字节不变**。

自动交易使用**未验证配置**时的保护（服务端 `blocking_reasons` 返回 409 兜底，
前端弹窗已同步预判）：

| 偏离类型 | 例子 | 处理 |
|----------|------|------|
| **阻断类** | `entry_mode=breakout`（需触发单）、`signal_mode=trend`（不用形态）、`atr_mult>0`（改止损定价）、`candidate`/`trend_wide` 预设 | 启动前必须勾选确认，否则拒绝 |
| **仅提示类** | 单独关闭趋势过滤 | 只在告警横幅提示，不阻断启动 |

> ⚠️ `atr_mult>0` 与 `signal_mode=trend` 被归为**阻断类**，理由同样与**双重基线**有关：
> 仪表盘的"原有行为"基线是**趋势过滤开启 + 结构止损 + 形态触发**，
> 改动其中任一项都改变了"下单触发/风险定价"的定义，需显式确认。

### 余额不足 ≠ 参数失效

信号表格若**有信号但整列都是「余额不足」**，说明余额开不出 OKX 最小下单量，
**不是**策略无信号、也不是参数没生效。展示路径会**保留**这些信号并打
`affordable=false` + `required_balance`，同时在顶部黄色提示条、汇总的
`可下单信号 0 / N` 指标、每行的「开不出」徽章上说明原因；
**下单路径**（`/api/order`、自动交易）则严格丢弃，绝不提交开不出的仓。

> 早期版本展示路径也走严格过滤，导致余额小时**表格永远为空**，
> 容易被误判为"策略没信号 / 参数选了没用"。此行为已修正并加回归测试
> （`test_api_signals_keeps_unaffordable_when_balance_too_small`）。

`trend`/`ma_type` 参数启用**趋势交易过滤**：信号处收盘高于均线(升势)只留 BUY，
低于均线(跌势)只留 SELL，过滤逆势信号。

`scan` 控制**图表/表格显示多少信号**（扫描最近多少根 K 线）。默认 30，调大（如 100）
可显示更多历史信号；窗口越大信号越多（受 K 线拉取上限限制）。

### POST /api/order 说明

当前实现返回 **mock 成功**（含 `mock:true` 和假 `order_id`），不会真下单。

要接 OKX 模拟盘真实下单，可在 `web_dashboard/app.py` 的 `api_order` 中，复用第二阶段的
`webhook_server/okx_trading.OKXTradingClient.place_limit_order(...)`（需设置
`OKX_API_KEY`/`OKX_API_SECRET`/`OKX_PASSPHRASE` 环境变量，并保留模拟盘安全头
`x-simulated-trading: 1`）。

## 测试

```bash
./.venv/bin/python -m unittest web_dashboard.test_app -v
```

## 目录结构

```
web_dashboard/
├── __init__.py
├── app.py                # Flask 后端：/api/klines, /api/signals, /api/order
├── templates/
│   └── index.html        # 单页前端（lightweight-charts CDN）
├── test_app.py           # 接口单元测试（mock 掉 K 线/信号，离线）
└── README.md
```

## 说明与限制

- 当前仅支持 `BTC-USDT-SWAP`（后端白名单限制），前端下拉框也已限定。
- K 线拉取依赖 OKX 公开 API；若网络无法直连 OKX，请设置 `OKX_PROXY`。
- `/api/signals` 默认扫描最近 `DASH_SIGNAL_SCAN`(30) 根 K 线，并按 `DASH_TREND_PERIOD`(0=关)
  决定是否应用趋势过滤。
