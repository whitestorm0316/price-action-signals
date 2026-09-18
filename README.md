# OKX 价格行为学交易信号生成器

从 OKX V5 **公开 API** 拉取 K 线数据，检测 **Pinbar** 和 **吞没（Engulfing）** 两种价格行为形态，
生成 TradingView Webhook 兼容的 JSON 信号并打印到控制台（当前**未**接入真实 Webhook）。

## 技术栈

- Python 3.10+
- `httpx` 请求 OKX 公开行情接口（无需 API Key）
- 无前端、无图表渲染

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. 运行（默认 BTC-USDT-SWAP，5 分钟线）
./.venv/bin/python main.py

# 3. 指定交易对与周期
./.venv/bin/python main.py --inst ETH-USDT-SWAP --bar 15m

# 4. 易读格式输出（调试用）
./.venv/bin/python main.py --pretty

# 5. 离线演示（内置模拟数据，不请求网络）
./.venv/bin/python main.py --mock

# 6. OKX 被墙时，走本地代理（Clash/V2Ray 等）
./.venv/bin/python main.py --proxy http://127.0.0.1:7890

# 7. 使用自定义数据端点（公司内网镜像 / 自建聚合服务）
./.venv/bin/python main.py --host https://your-mirror.example.com/api/v5

# 8. 扫描最近 N 根 K 线的历史信号，验证数据链路与形态检测
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --scan 100

# 9. 对最近 N 根 K 线的信号做回测：跟踪每笔信号后续走势，统计成功/失败/胜率
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --backtest 300

# 10. 启用量能确认：放量信号加权、缩量信号减权，并过滤"缩量且盈亏比<2"的信号
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --volume
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--inst` | `BTC-USDT-SWAP` | OKX 交易对 |
| `--bar` | `5m` | K 线周期（1m/3m/5m/15m/30m/1H/4H/1D 等） |
| `--limit` | `300` | 拉取 K 线条数（OKX 单次上限 300） |
| `--pretty` | 关 | 易读格式打印 JSON |
| `--mock` | 关 | 使用内置模拟数据（离线验证） |
| `--proxy` | 无 | HTTP 代理地址，OKX 被墙时使用 |
| `--host` | 无 | 自定义数据端点前缀 |
| `--timeout` | `10` | 单次请求超时秒数 |
| `--trend` | `0` | 启用均线趋势过滤（周期 N，如 50）；0=不启用 |
| `--ma-type` | `sma` | 趋势均线类型：`sma` / `ema` |
| `--scan` | `0` | 扫描最近 N 根 K 线的历史信号（默认 0=仅检测最新一根） |
| `--backtest` | `0` | 对最近 N 根 K 线上的信号做回测（WIN/LOSS/OPEN + 胜率） |
| `--volume` | 关 | 启用量能确认（放量加权 / 缩量降级 / 弱量低盈亏比过滤），默认关闭 |

## 信号输出格式

触发信号时打印紧凑 JSON，字段与 TradingView Webhook 约定一致：

```json
{"action":"BUY","ticker":"BTC-USDT-SWAP","price":99.9,"sl":96.7015,"tp":106.297,"strategy":"bullish_pinbar"}
```

| 字段 | 说明 |
|------|------|
| `action` | `BUY` / `SELL` |
| `ticker` | 交易对 |
| `price` | 入场价（形态收盘价） |
| `sl` | 止损价 |
| `tp` | 目标价（固定 2:1 盈亏比，tp = 入场 ± 2×risk） |
| `strategy` | 形态名称：`bullish_pinbar` / `bearish_pinbar` / `bullish_engulfing` / `bearish_engulfing` |

> 附加字段 `risk`、`rr` 便于排查，不参与 TradingView 必需字段。

启用量能后（`--volume`），信号会额外包含量能确认字段（见下方"量能确认"）：
```json
{"action":"BUY","ticker":"BTC-USDT-SWAP","price":102.0,"sl":97.3998,"tp":111.2005,
 "strategy":"bullish_engulfing","risk":4.6003,"rr":2.0,
 "volume_ratio":5.0,"volume_signal":"strong","volume_confirm":"放量吞没"}
```

## 关于"未检测到信号"

脚本**默认只检测最新一根 K 线**是否构成 Pinbar/吞没。绝大多数时候，最新一根
恰好构成标准形态的概率不高，因此打印"未检测到价格行为信号"是**正常现象**，
不代表脚本或数据有问题。

若想确认数据链路与形态检测工作正常，用 `--scan N` 扫描最近 N 根 K 线的历史信号，
例如 `--scan 100` 会在最近 100 根里统计出若干 Pinbar / 吞没信号。

## 信号回测（判断信号质量）

用 `--backtest N` 可验证历史信号"如果真按它交易"的结局：对扫描到的每个信号，
跟踪其**之后**的价格走势，判断是止盈、止损还是仍在持仓。

```bash
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --backtest 300
```

输出示例（节选）：
```
[1789479300000] bearish_engulfing action=SELL entry=76979.7 sl=77227.1 tp=76485   rr=2.0  ->  ✅ 止盈成功
[1789469400000] bearish_engulfing action=SELL entry=76950.8 sl=77114.2 tp=76624.1 rr=2.0  ->  ❌ 止损触发
[1789554900000] bullish_pinbar    action=BUY  entry=75971   sl=75855.2 tp=76202.5 rr=2.0  ->  ⏳ 持仓中
—— 回测最近 299 根 5m K 线：共 104 个信号，止盈 31 / 止损 63 / 持仓中 10，已平仓胜率 33.0% ——
```

判定规则（对 BUY 信号，SELL 对称）：
- ✅ **止盈**：后续某根 `high ≥ tp`（且未先触 sl）
- ❌ **止损**：后续某根 `low ≤ sl`（若同一根同时触及两者，按保守计为止损）
- ⏳ **持仓中**：到数据末尾仍未触及任一价位

> 回测仅用 OKX 公开历史 K 线做**事后**统计，不模拟滑点/手续费，结果偏理想。
> 胜率低（如 33%）但配合 2:1 盈亏比，期望值约为 33%×2 − 67%×1 ≈ 0，
> 说明该简单形态在所选周期下单独使用并无显著正期望，属于信号质量评估信息。

## 量能确认（可选）

量能**不作为独立的买入/卖出信号**，只作为现有价格行为信号（Pinbar/吞没）的
**确认或降级**条件，默认关闭，用 `--volume` 开关启用：

```bash
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --volume
```

**量能比率** = 当前 K 线成交量 ÷ 过去 20 根 K 线平均成交量（不含当前，无前视偏差）。

**信号加权 / 降级**：

| 量能比率 | 状态 | 效果 |
|---------|------|------|
| ≥ 1.2 | 放量 | 信号**加权**（确认）：标记"放量Pinbar" / "放量吞没" |
| < 0.8 | 缩量 | 信号**减权**（降级）：标记"缩量Pinbar" / "缩量吞没" |
| 0.8 ~ 1.2 / 无法计算 | 中性 | 不加权不减权 |

**过滤规则**：若 `volume_signal` 为 `weak`（缩量）**且** 盈亏比 < 2，过滤掉该信号
（弱量 + 低盈亏比 = 不值得冒险）。

**输出字段**（启用量能后附加在信号 JSON 中）：

| 字段 | 说明 |
|------|------|
| `volume_ratio` | 量能比率（数值） |
| `volume_signal` | `strong` / `weak` / `neutral` |
| `volume_confirm` | 形态量能标签："放量Pinbar" / "缩量Pinbar" / "放量吞没" / "缩量吞没"（中性时省略） |

> 不传 `--volume` 时，信号 JSON 与之前完全一致，不输出任何量能字段（量能可选）。
> 注意：当前入场采用**固定 2:1 盈亏比**，因此"盈亏比 < 2"在固定盈亏比下不会触发；
> 该过滤逻辑保留，待盈亏比改为动态时即自动生效。

## 趋势过滤

用均线判断大趋势，**只顺势交易**：上升趋势（收盘 > 均线）只保留 BUY，
下降趋势（收盘 < 均线）只保留 SELL，过滤掉逆势信号。

```bash
# 启用 50 周期 SMA 趋势过滤
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --trend 50

# 指定 EMA 均线
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --trend 50 --ma-type ema

# 回测中对比趋势过滤的效果
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --bar 1H --backtest 100 --trend 50
```

过滤规则：
- 均线只使用**信号所在 K 线（含）之前**的数据计算，**无前视偏差**
- 信号处收盘 > 均线 → 上升趋势，只留 BUY
- 信号处收盘 < 均线 → 下降趋势，只留 SELL
- 数据不足无法算均线时，保留信号（不误杀）

### 实测效果（BTC 真实行情回测）

| 周期 | 趋势过滤 | 信号数 | 已平仓胜率 |
|------|---------|--------|-----------|
| 5m | 无 | 103 | 33.7% |
| 5m | SMA50 | 60 | 26.8% ⬇️ |
| 1H | 无 | 44 | 31.6% |
| 1H | SMA20 | 25 | **40.0%** ⬆️ |
| 1H | SMA50 | 21 | **41.2%** ⬆️ |

> **结论**：趋势过滤在 **1H 等较大周期**上显著提升胜率（31.6% → 40%+），
> 但在 **5m 等高噪声周期**上反而降低胜率（33.7% → 26.8%）。
> 原因是均线趋势过滤依赖"价格相对有序运动"，周期越小噪声越大、过滤越容易误杀有效信号。
> 建议在较大周期上使用趋势过滤，并可用 `--backtest` 自行对比不同 `--bar` / `--trend` 参数。

## 形态判定规则

**Pinbar（针形线）**
- 影线长度 ≥ 实体长度的 **2 倍**
- 看涨：下影线长，收盘价位于 K 线高点的顶部 **1/3** 区域
- 看跌：上影线长，收盘价位于 K 线低点的底部 **1/3** 区域

**吞没（Engulfing）**
- 当前 K 线实体**完全覆盖**前一根 K 线实体，且方向相反
- 看涨吞没：前一根阴线被当前阳线实体覆盖
- 看跌吞没：前一根阳线被当前阴线实体覆盖

## 入场 / 止损 / 目标

| 项 | 规则 |
|----|------|
| 入场价 | 形态收盘价 |
| 止损价 | Pinbar：影线**极值外侧**；吞没：两根 K 线**极值外侧** |
| 目标价 | 固定 **2:1**（Reward = 2 × Risk） |

## 接入 Webhook（第二阶段）

第一阶段的信号引擎把 JSON 打印到控制台。要真正自动下单，可搭配第二阶段的
**Webhook 信号执行器**（`webhook_server/`）：它接收 TradingView Alert 发来的
`{action, ticker, price, sl, tp, strategy}`，并自动在 **OKX 模拟盘**下限价单 +
止盈止损。

```bash
export OKX_API_KEY=xxx OKX_API_SECRET=xxx OKX_PASSPHRASE=xxx
./.venv/bin/uvicorn webhook_server.app:app --host 0.0.0.0 --port 9000
```

详情见 [webhook_server/README.md](webhook_server/README.md)（含 OKX 接口规格、签名方式、
TradingView 配置、模拟盘安全说明）。

## Web 仪表盘（第三阶段）

一个 Flask + lightweight-charts 的单页面仪表盘，展示 K 线与价格行为信号，并提供手动下单入口
（当前下单为 mock）。复用第一阶段检测逻辑。

```bash
export OKX_PROXY=http://127.0.0.1:1087     # 拉 K 线走代理（OKX 被墙时）
./.venv/bin/python -m web_dashboard.app
# 浏览器打开 http://127.0.0.1:8000
```

详情见 [web_dashboard/README.md](web_dashboard/README.md)。

## 测试

```bash
# 第一阶段：形态 + 趋势 + 量能
./.venv/bin/python -m unittest test_patterns test_trend test_volume -v
# 第二阶段：webhook 执行器（mock 交易所，不连真实网络）
./.venv/bin/python -m unittest webhook_server.test_okx_trading -v
# 第三阶段：Web 仪表盘接口
./.venv/bin/python -m unittest web_dashboard.test_app -v
```

## 目录结构

```
price-action-signals/
├── okx_client.py        # OKX 公开 API 客户端（K 线拉取，含镜像重试）
├── patterns.py          # Pinbar / 吞没 形态检测 + 入场/止损/目标计算
├── trend.py             # 均线趋势判断与过滤（SMA/EMA，无前视偏差）
├── volume.py            # 量能确认（量能比率 / 加权降级 / 弱量低盈亏比过滤）
├── signals.py           # Pattern -> TradingView JSON 信号序列化
├── main.py              # 主入口（CLI）
├── test_patterns.py     # 形态检测单元测试（纯算法，无需网络）
├── test_trend.py        # 趋势过滤单元测试
├── test_volume.py       # 量能确认单元测试
├── requirements.txt     # 依赖
├── README.md
├── webhook_server/      # 第二阶段：Webhook 信号执行器（OKX 模拟盘下单）
│   ├── app.py           #   FastAPI 服务：接收信号 -> 下单 -> 记录
│   ├── okx_trading.py   #   OKX 模拟盘客户端：签名 + 限价单 + attachAlgoOrds
    ├── storage.py       #   本地 SQLite 订单记录
    ├── test_okx_trading.py
    ├── requirements.txt
    └── README.md
└── web_dashboard/        # 第三阶段：Web 信号仪表盘（Flask + lightweight-charts）
    ├── app.py            #   Flask 后端：/api/klines, /api/signals, /api/order(mock)
    ├── templates/
    │   └── index.html    #   单页前端（lightweight-charts CDN）
    ├── test_app.py       #   接口单元测试
    └── README.md
```


> **注意**：OKX 域名（`www.okx.com` / `aws.okx.com`）在部分网络（尤其中国大陆）会被 **DNS 污染**，
> 导致 `nodename nor servname provided` 或 `502` 之类的连接错误。
> 脚本已内置三端点镜像切换、系统代理读取、指数退避重试，并输出友好的诊断信息。
> 若仍无法访问，按诊断提示依次尝试：
> 1. `--proxy http://127.0.0.1:7890`（替换为你的代理端口）
> 2. `--host <可访问的镜像端点>`
> 3. `--mock` 离线演示
