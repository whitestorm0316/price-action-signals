# TradingView Webhook 信号执行器（OKX 模拟盘）

接收 TradingView Alert Webhook 发来的信号 JSON，自动在 **OKX 模拟盘（Demo）** 下限价单
并附带止盈止损。**只走模拟盘，绝不触碰真实账户。**

## 工作原理

```
TradingView Alert --(Webhook POST JSON)--> 本服务(FastAPI) --> OKX V5 模拟盘下单
                                          |
                                          +--> 结果写入本地 SQLite
```

接收的消息格式固定：
```json
{"action":"BUY","ticker":"BTC-USDT-SWAP","price":75800,"sl":75600,"tp":76200,"strategy":"bullish_pinbar"}
```

## OKX 接口规格（已核对官方文档）

**下单（限价单 + 附带止盈止损）: `POST /api/v5/trade/order`**

| 参数 | 必填 | 说明 |
|------|------|------|
| `instId` | 是 | 交易对，`BTC-USDT-SWAP` |
| `tdMode` | 是 | 交易模式 `cross`(全仓)/`isolated`(逐仓) |
| `side` | 是 | `buy` / `sell` |
| `ordType` | 是 | `limit` 限价单 |
| `sz` | 是 | 委托数量（张），本项目固定 `0.01` |
| `px` | 是 | 限价单价格（用信号中的 price） |
| `clOrdId` | 否 | 客户端订单 ID |
| `attachAlgoOrds[]` | 否 | 附带止盈止损：`tpTriggerPx`(止盈触发价)、`tpOrdPx`(止盈委托价, `-1`=市价)、`slTriggerPx`(止损触发价)、`slOrdPx`(止损委托价, `-1`=市价) |

> 采用 **attachAlgoOrds** 方式：一次下单同时携带止盈止损（原子操作，挂单与止盈止损不分离）。
> 独立策略委托接口 `POST /api/v5/trade/order-algo` 也可行，但需要挂单后再单独调用一次，故未采用。

**签名方式**（每个私有请求）：
```
timestamp = ISO8601 UTC，如 2026-09-17T03:04:05.000Z
OK-ACCESS-SIGN = Base64( HMAC_SHA256(timestamp + method + requestPath + body, secretKey) )
```

**模拟盘标识**：请求头加 `x-simulated-trading: 1`。

**下单响应**：`{"code":"0","msg":"","data":[{"clOrdId","ordId","sCode","sMsg"}]}`，
`ordId` 为订单 ID，`sCode` 非 0 表示该笔被拒。

## 环境变量（必须设置，绝不硬编码）

| 变量 | 说明 |
|------|------|
| `OKX_API_KEY` | 模拟盘 API Key（Demo Trading API Key） |
| `OKX_API_SECRET` | 模拟盘 API Secret |
| `OKX_PASSPHRASE` | API 口令（创建 API Key 时设置） |
| `OKX_TD_MODE` | 可选，交易模式，默认 `cross` |
| `OKX_ORDERS_DB` | 可选，SQLite 文件路径，默认 `webhook_server/orders.db` |

> 如何拿模拟盘 Key：OKX → 交易 → 模拟交易(Demo) → 个人中心 → 模拟交易 API → 创建。
> 务必使用 **模拟盘** 的 API Key，本服务会在每个请求带上 `x-simulated-trading: 1`，
> 即使误用实盘 Key 也只会失败而不会在实盘成交（但请一定用模拟盘 Key）。

## 运行

```bash
cd price-action-signals
./.venv/bin/pip install -r webhook_server/requirements.txt

export OKX_API_KEY=xxx
export OKX_API_SECRET=xxx
export OKX_PASSPHRASE=xxx

./.venv/bin/uvicorn webhook_server.app:app --host 0.0.0.0 --port 9000
```

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/webhook` | 接收信号并下单（TradingView Webhook 指向这里） |
| GET | `/health` | 健康检查 |
| GET | `/orders?limit=20` | 查看最近下单记录（本地 SQLite） |

## TradingView Alert 配置

在 TradingView Alert 的 "Notifications" 里选择 **Webhook URL**，填 `http://<你的IP>:9000/webhook`，
Message 填第一阶段的信号引擎输出的 JSON（或用 `{{strategy.order.action}}` 等占位符拼装）。

> 注意：TradingView Webhook 需要公网可达地址。若在本地测试，需用内网穿透（如 ngrok）
> 或部署到有公网 IP 的机器。测试阶段也可用 `curl` 直接模拟。

## 本地测试

```bash
# 冒烟测试（不连真实 OKX）：用 curl 模拟 TradingView 发信号
curl -X POST http://127.0.0.1:9000/webhook \
  -H 'Content-Type: application/json' \
  -d '{"action":"BUY","ticker":"BTC-USDT-SWAP","price":75800,"sl":75600,"tp":76200,"strategy":"bullish_pinbar"}'

# 查看记录
curl http://127.0.0.1:9000/orders
```

单元测试（不连接真实网络，mock 交易所响应）：
```bash
./.venv/bin/python -m unittest webhook_server.test_okx_trading -v
```

## 目录结构

```
webhook_server/
├── __init__.py
├── app.py               # FastAPI 服务：接收信号 -> 下单 -> 记录
├── okx_trading.py       # OKX 模拟盘客户端：签名 + 限价单 + attachAlgoOrds
├── storage.py           # 本地 SQLite 订单记录
├── test_okx_trading.py  # 单元测试（mock 网络）
├── requirements.txt     # fastapi / uvicorn / pydantic / httpx
└── orders.db            # 运行后生成的下单记录
```

## 安全提示

- 本服务**硬编码**只交易 `BTC-USDT-SWAP` 固定 `0.01` 张，不做事先未校验的仓位计算。
- 请求字段做了严格校验：action 只允许 BUY/SELL、价格关系必须合理（BUY: sl<price<tp；SELL: tp<price<sl）。
- 所有请求都带 `x-simulated-trading: 1`，生产部署前请务必确认这是**模拟盘**。
