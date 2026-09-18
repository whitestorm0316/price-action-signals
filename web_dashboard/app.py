"""Web 仪表盘后端 —— 展示信号与 K 线，并提供手动下单入口。

复用阶段一模块（okx_client / patterns / trend），不重写检测逻辑。
仅做读取与 mock 下单，不接真实账户。

运行:
    cd price-action-signals
    export OKX_PROXY=http://127.0.0.1:1087   # 可选，拉取 K 线时走代理
    ./.venv/bin/python -m web_dashboard.app
    # 浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import os
import sys
import uuid

from flask import Flask, jsonify, render_template, request

# 复用阶段一模块：把项目根加入搜索路径
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from okx_client import OKXClient
from patterns import detect_engulfing, detect_pinbar
from trend import trend_allows, trend_at
from volume import volume_ratio_at, volume_signal, should_filter
from webhook_server.okx_trading import OKXConfigError, OKXTradeError, OKXTradingClient

app = Flask(__name__)
# 模板改动自动重载，避免每次改模板都要重启服务
app.config["TEMPLATES_AUTO_RELOAD"] = True
# 模板改动自动重载，避免每次改模板都要重启服务
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ---------------------------------------------------------------------------
# .env 配置加载（含敏感凭证），支持 前端设置 -> 自动保存到 .env
# ---------------------------------------------------------------------------
ENV_FILE = os.path.join(_PROJECT_ROOT, ".env")

# 允许从 .env 读取并可在前端设置的键
CONFIG_KEYS = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE", "OKX_PROXY")


def _load_env_file() -> None:
    """把 .env 中尚未被环境变量覆盖的键写入 os.environ。

    优先级：已存在的环境变量 > .env 文件值。
    简单解析 KEY=VALUE，忽略 # 注释与空行，支持值含 #（拆一次）。
    """
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 已由真实环境变量提供时，不覆盖
                if key in os.environ:
                    continue
                os.environ[key] = value
    except OSError:
        pass


def _save_env_file(values: dict) -> None:
    """把配置写回 .env（保留现有键与注释）。"""
    new_values = {k: str(v) for k, v in values.items() if k in CONFIG_KEYS}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()
    except OSError:
        existing_lines = []
    # 按 .env 现有顺序更新对应键，缺失的追加
    written = {k: False for k in new_values}
    out: list[str] = []
    for line in existing_lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in new_values:
                out.append(f"{key}={new_values[key]}\n")
                written[key] = True
                continue
        out.append(line)
    for key, val in new_values.items():
        if not written[key]:
            out.append(f"{key}={val}\n")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(out)


_load_env_file()

# 默认参数
DEFAULT_TICKER = os.getenv("DASH_TICKER", "BTC-USDT-SWAP")
DEFAULT_INTERVAL = os.getenv("DASH_INTERVAL", "5m")
OKX_PROXY = os.getenv("OKX_PROXY") or None
# 信号扫描窗口：检查最近多少根 K 线；趋势过滤周期(0=关闭)
SIGNAL_SCAN = int(os.getenv("DASH_SIGNAL_SCAN", "30"))
TREND_PERIOD = int(os.getenv("DASH_TREND_PERIOD", "0"))   # 0 = 不启用趋势过滤
MA_TYPE = os.getenv("DASH_MA_TYPE", "sma")

# 允许的交易对（阶段一/二约束：先只 BTC-USDT-SWAP）
ALLOWED_TICKERS = {"BTC-USDT-SWAP"}

# 合约面值（BTC-USDT-SWAP：1张 = 0.01 BTC）
CONTRACT_BTC = 0.01

# 风险参数
RISK_PCT = 0.05             # 每笔风险 = 余额 × 5%（可并行10-20笔）
MIN_RISK_REWARD = 1.5       # 最小盈亏比阈值（低于此值不显示）
MIN_CONTRACTS = 1           # OKX BTC-USDT-SWAP 最小下单量：1张

# OKX 手续费（限价单 Maker 费率，Taker 约0.05%）
FEE_RATE_MAKER = 0.0002     # 0.02%

# 下单类型映射：BUY/SELL -> 小写 side
ACTION_MAP = {"BUY": "buy", "SELL": "sell"}

# 固定下单数量（张），不做仓位计算
ORDER_SIZE = "0.01"

# 下单模式：DASH_ORDER_SIMULATED=1（默认）时提交到 OKX 模拟盘；
# 凭证缺失自动降级为 mock（接口不报 500，返回 mock=true 说明）。
_ORDER_SIMULATED = os.getenv("DASH_ORDER_SIMULATED", "1").lower() not in {"0", "false"}
_trade_client: OKXTradingClient | None = None
_trade_client_error: str | None = None


def get_trade_client() -> tuple[OKXTradingClient | None, str | None]:
    """惰性初始化 OKX 模拟盘客户端（走 OKX_PROXY 代理）；未配凭证返回 (None, 错误说明)。"""
    global _trade_client, _trade_client_error
    if not _ORDER_SIMULATED:
        return None, None
    if _trade_client is None and _trade_client_error is None:
        try:
            _trade_client = OKXTradingClient(
                simulated=True,
                proxy=os.getenv("OKX_PROXY") or None,
            )
        except OKXConfigError as exc:
            _trade_client_error = str(exc)
    return _trade_client, _trade_client_error


def reset_trade_client() -> None:
    """凭证/代理变更后重建交易客户端与代理（热更新，无需重启进程）。"""
    global _trade_client, _trade_client_error, OKX_PROXY
    if _trade_client is not None:
        _trade_client.close()
    _trade_client = None
    _trade_client_error = None
    OKX_PROXY = os.getenv("OKX_PROXY") or None


def get_usdt_balance() -> float:
    """获取 USDT 可用余额；凭证缺失或异常返回 0（不下单/不计算）。"""
    client, _ = get_trade_client()
    if client is None:
        return 0.0
    try:
        balance = client.get_balance()
        for b in balance:
            for d in (b.get("details") or []):
                if d.get("ccy") == "USDT":
                    return float(d.get("availEq") or d.get("availBal") or 0)
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0


def enrich_signals_with_risk(
    signals: list[dict],
    balance: float,
    risk_pct: float | None = None,
) -> list[dict]:
    """给每个信号附加风险计算字段并过滤。

    计算公式：
        risk_amount = balance × risk_pct (默认5%)
        risk_distance = |entry - sl| (价格距离)
        risk_contracts = floor(risk_amount / (risk_distance × CONTRACT_BTC))

        可用保证金上限 = balance × 0.80 (留 20% 安全余量)
        margin_contracts = floor(可用保证金上限 / (CONTRACT_BTC × entry))

        suggested_contracts = min(risk_contracts, margin_contracts)
        expected_profit = suggested × |tp - entry| × CONTRACT_BTC
        expected_loss   = suggested × |entry - sl| × CONTRACT_BTC
    """
    _risk_pct = (risk_pct / 100.0) if risk_pct is not None else RISK_PCT
    risk_amount = balance * _risk_pct  # 美元风险金额
    safe_balance = balance * 0.80
    filtered = []

    for sig in signals:
        entry = sig.get("price", 0)
        sl = sig.get("sl", 0)
        tp = sig.get("tp", 0)

        if not all([entry, sl, tp, entry > 0]):
            continue

        risk_distance = abs(entry - sl)
        reward_distance = abs(tp - entry)

        # 盈亏比
        rr_ratio = round(reward_distance / risk_distance, 2) if risk_distance > 0 else 0
        sig["risk_reward_ratio"] = rr_ratio

        # 风险计算张数（基于止损距离）
        if risk_distance > 0 and balance > 0:
            risk_contracts = int(risk_amount / (risk_distance * CONTRACT_BTC))
            risk_contracts = max(risk_contracts, 0)
        else:
            risk_contracts = 0

        # 保证金计算张数（基于可用权益）
        notional_per = CONTRACT_BTC * entry
        margin_contracts = int(safe_balance / notional_per) if notional_per > 0 else 0
        margin_contracts = max(margin_contracts, 0)

        # 最终建议张数
        suggested = min(risk_contracts, margin_contracts)
        sig["suggested_contracts"] = suggested

        # 建议金额（USDT）
        sig["suggested_value"] = round(suggested * CONTRACT_BTC * entry, 2)

        # 预计盈利/亏损（USDT）—— 按建议张数计算
        notional = suggested * CONTRACT_BTC * entry
        sig["expected_profit"] = round(suggested * reward_distance * CONTRACT_BTC, 2)
        sig["expected_loss"] = round(suggested * risk_distance * CONTRACT_BTC, 2)

        # 手续费估算（开仓+平仓，按 Maker 0.02% 计算）
        sig["estimated_fee"] = round(notional * FEE_RATE_MAKER * 2, 2)  # ×2 = 开+平

        # 仓位信息
        sig["risk_amount"] = round(risk_amount, 2)
        sig["risk_pct"] = round(_risk_pct * 100, 1)   # 显示为百分比（如 5.0 表示 5%）
        sig["risk_contracts"] = risk_contracts
        sig["margin_contracts"] = margin_contracts

        # ---- 过滤 ----
        if rr_ratio < MIN_RISK_REWARD:
            continue
        if suggested < MIN_CONTRACTS:
            continue

        filtered.append(sig)

    return filtered


# ---------------------------------------------------------------------------
# K 线拉取
# ---------------------------------------------------------------------------
def fetch_klines(ticker: str, interval: str, limit: int) -> list:
    """拉取 K 线（升序），返回 lightweight-charts 格式 [{time,open,high,low,close}]。"""
    client = OKXClient(proxy=OKX_PROXY)
    try:
        candles = client.get_candles(ticker, bar=interval, limit=limit)
    finally:
        client.close()
    # time 需秒级 Unix 时间戳
    return [
        {
            "time": c.ts // 1000,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]


# ---------------------------------------------------------------------------
# 信号检测（复用阶段一逻辑）
# ---------------------------------------------------------------------------
def detect_signals(
    ticker: str,
    interval: str,
    limit: int,
    trend_period: int | None = None,
    ma_type: str | None = None,
    scan_window: int | None = None,
    volume_enabled: bool = False,
) -> list[dict]:
    """拉取 K 线并在最近 N 根上检测信号（可选趋势过滤）。

    Args:
        trend_period: 均线周期，None/0 表示不启用趋势过滤
        ma_type: 均线类型 sma/ema
        scan_window: 扫描最近多少根 K 线；None 用模块级 SIGNAL_SCAN

    返回列表，每个元素:
        {action, price, sl, tp, strategy, ratio, timestamp, result, result_ts}
    """
    tp_ = (trend_period or 0) if trend_period is not None else TREND_PERIOD
    mt_ = ma_type or MA_TYPE
    sw_ = scan_window if scan_window is not None else SIGNAL_SCAN

    client = OKXClient(proxy=OKX_PROXY)
    try:
        candles = client.get_candles(ticker, bar=interval, limit=limit)
    finally:
        client.close()

    signals: list[dict] = []
    scan = min(sw_, len(candles) - 1)
    # 从旧到新扫描，保证返回按时间升序
    for idx in range(len(candles) - scan, len(candles)):
        cur = candles[idx]
        ratio = volume_ratio_at(candles, idx) if volume_enabled else None
        for pattern in (detect_pinbar(cur, volume_ratio=ratio),
                        detect_engulfing(candles[idx - 1], cur, volume_ratio=ratio)):
            if pattern is None:
                continue
            if tp_:
                trend = trend_at(candles, idx, tp_, mt_)
                if not trend_allows(pattern, trend):
                    continue
            if volume_enabled and should_filter(ratio, pattern.risk_reward):
                continue
            # 用信号之后的价格走势判定成功/失败/持仓中
            result, result_ts = evaluate_signal(idx, pattern, candles)
            signals.append(
                {
                    "action": pattern.action,
                    "price": pattern.entry,
                    "sl": pattern.stop,
                    "tp": pattern.take_profit,
                    "strategy": pattern.name,
                    "ratio": pattern.risk_reward,
                    "timestamp": cur.ts // 1000,
                    "result": result,          # WIN / LOSS / OPEN
                    "result_ts": result_ts // 1000 if result_ts else None,
                }
            )
            if volume_enabled:
                signals[-1].update(
                    volume_ratio=ratio,
                    volume_signal=volume_signal(ratio),
                    volume_confirm=pattern.volume_confirm,
                )
    return signals


def evaluate_signal(idx: int, pattern, candles: list) -> tuple[str, object]:
    """判定信号结果（复用阶段一 _evaluate 逻辑）。

    从信号所在 K 线的下一根开始，逐根判断是否先触及止损或止盈。

    Returns:
        (result, ts)：result ∈ {WIN, LOSS, OPEN}
          WIN  = 先到目标价（成功）
          LOSS = 先触发止损（失败）
          OPEN = 到数据末尾仍未触及（持仓中）
        ts 为触发/最新 K 线的毫秒时间戳。
    """
    sl, tp = pattern.stop, pattern.take_profit
    is_buy = pattern.action == "BUY"

    for j in range(idx + 1, len(candles)):
        c = candles[j]
        if is_buy:
            hit_sl = c.low <= sl
            hit_tp = c.high >= tp
        else:  # SELL：sl 在价格上方，tp 在下方
            hit_sl = c.high >= sl
            hit_tp = c.low <= tp

        if hit_sl and hit_tp:
            # 同根既触止损又触止盈：无法确定先后，保守按止损计
            return "LOSS", c.ts
        if hit_sl:
            return "LOSS", c.ts
        if hit_tp:
            return "WIN", c.ts

    return "OPEN", candles[-1].ts if candles else None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index() -> str:
    return render_template("index.html", default_ticker=DEFAULT_TICKER,
                           default_interval=DEFAULT_INTERVAL)


@app.route("/api/klines")
def api_klines():
    ticker = request.args.get("ticker", DEFAULT_TICKER).upper()
    interval = request.args.get("interval", DEFAULT_INTERVAL)
    limit = request.args.get("limit", 100, type=int)
    limit = max(2, min(limit, 3000))

    if ticker not in ALLOWED_TICKERS:
        return jsonify({"error": f"暂只支持 {sorted(ALLOWED_TICKERS)}"}), 400

    try:
        data = fetch_klines(ticker, interval, limit)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"拉取 K 线失败: {exc}"}), 502

    return jsonify(data)


@app.route("/api/signals")
def api_signals():
    ticker = request.args.get("ticker", DEFAULT_TICKER).upper()
    interval = request.args.get("interval", DEFAULT_INTERVAL)
    limit = request.args.get("limit", 100, type=int)
    limit = max(30, min(limit, 3000))
    # 趋势交易过滤：trend=均线周期(0/空=关闭)，ma_type=sma|ema
    trend_period = request.args.get("trend", type=int, default=None)
    ma_type = request.args.get("ma_type", default=None)
    # 信号扫描窗口：检查最近多少根 K 线（默认 DASH_SIGNAL_SCAN）
    scan_window = request.args.get("scan", type=int, default=None)
    if scan_window is not None:
        scan_window = max(1, min(scan_window, 3000))
    volume_option = request.args.get("volume", "0").lower()
    if volume_option not in {"0", "1", "false", "true"}:
        return jsonify({"error": "volume 必须为 0/1 或 false/true"}), 400

    if ticker not in ALLOWED_TICKERS:
        return jsonify({"error": f"暂只支持 {sorted(ALLOWED_TICKERS)}"}), 400

    try:
        options = dict(trend_period=trend_period, ma_type=ma_type,
                       scan_window=scan_window)
        if volume_option in {"1", "true"}:
            options["volume_enabled"] = True
        signals = detect_signals(ticker, interval, limit, **options)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"信号检测失败: {exc}"}), 502

    # 时间倒序（最新的在前），便于前端表格展示
    signals.reverse()

    # 仓位计算：余额×risk_pct%风险 / 止损距离 → 建议张数；过滤盈亏比<1.5 & 建议金额不足
    balance = get_usdt_balance()
    risk_pct = request.args.get("risk_pct", type=float)
    signals = enrich_signals_with_risk(signals, balance, risk_pct=risk_pct)

    return jsonify(signals)


def _mask_secret(value: str) -> str:
    """脱敏展示：只保留前4位与后4位，中间用 * 填充；空则返回空串。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """读取当前配置（敏感字段脱敏）。"""
    return jsonify(
        {
            "configured": all(os.getenv(k) for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE")),
            "proxy": os.getenv("OKX_PROXY") or "",
            "simulated": _ORDER_SIMULATED,
            "api_key_masked": _mask_secret(os.getenv("OKX_API_KEY") or ""),
            "secret_masked": _mask_secret(os.getenv("OKX_API_SECRET") or ""),
            "passphrase_masked": _mask_secret(os.getenv("OKX_PASSPHRASE") or ""),
        }
    )


@app.route("/api/config", methods=["POST"])
def api_config_save():
    """保存配置到 .env 并热更新（无需重启进程）。

    前端传值覆盖对应键；未传的键保留原值。
    """
    data = request.get_json(silent=True) or {}
    keys = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE", "OKX_PROXY")
    # 只更新传入的键；空字符串视为清除
    updates = {}
    for k in keys:
        if k in data:
            updates[k] = data[k].strip()
    if not updates:
        return jsonify({"error": "没有可更新的配置项"}), 400

    # 写入 .env，同时更新当前进程环境变量
    _save_env_file(updates)
    for k, v in updates.items():
        os.environ[k] = v
    # 重建交易客户端与代理（热更新）
    reset_trade_client()
    # 立即触发一次客户端初始化，尽早暴露凭证/网络问题
    client, cfg_err = get_trade_client()
    return jsonify(
        {
            "status": "ok",
            "saved": {k: _mask_secret(v) for k, v in updates.items()},
            "configured": all(os.getenv(k) for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE")),
            "client_ok": client is not None,
            "client_error": cfg_err,
        }
    )


@app.route("/api/account", methods=["GET"])
def api_account():
    """查询账户汇总：余额 / 当前仓位 / 下单历史 / 当前挂单。

    依赖 OKX 模拟盘凭证；未配置时返回 mock 提示，不伪装数据。
    """
    ticker = request.args.get("ticker", DEFAULT_TICKER).upper()
    if ticker not in ALLOWED_TICKERS:
        return jsonify({"error": f"暂只支持 {sorted(ALLOWED_TICKERS)}"}), 400

    client, cfg_err = get_trade_client()
    if client is None:
        return jsonify({
            "mock": True,
            "mock_reason": cfg_err or "未配置 OKX 模拟盘凭证",
            "balance": [], "positions": [], "orders": [], "pending": [],
        }), 200

    try:
        return jsonify({
            "mock": False,
            "balance": client.get_balance(),
            "positions": client.get_positions(ticker),
            "orders": client.get_order_history(ticker, state="filled", limit=20),
            "pending": client.get_pending_orders(ticker),
        })
    except OKXTradeError as exc:
        return jsonify({"error": f"查询账户失败: {exc}"}), 502
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"查询账户异常: {exc}"}), 502


@app.route("/api/order", methods=["POST"])
def api_order():
    """手动下单：限价单 + 附带止盈止损提交到 OKX 模拟盘。

    复用阶段二 OKXTradingClient（POST /api/v5/trade/order，
    attachAlgoOrds 一次挂单+止盈止损，x-simulated-trading: 1，
    HMAC-SHA256 签名串 = timestamp + method + requestPath + body）。
    未配置凭证时自动降级 mock（mock=true + mock_reason），不伪装成功。
    """
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").upper()
    ticker = (data.get("ticker") or "").upper()
    # 优先用前端传来的建议张数；为空则用固定值（兜底）
    size = data.get("size") or data.get("suggested_contracts") or ORDER_SIZE

    if action not in ACTION_MAP:
        return jsonify({"error": "action 必须为 BUY 或 SELL"}), 400
    if ticker not in ALLOWED_TICKERS:
        return jsonify({"error": f"暂只支持 {sorted(ALLOWED_TICKERS)}"}), 400
    try:
        if float(size) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "size 必须为正数"}), 400

    price, sl, tp = data.get("price"), data.get("sl"), data.get("tp")
    if price is None or sl is None or tp is None:
        return jsonify({"error": "缺少 price / sl / tp"}), 400

    # 价格关系校验（BUY: sl < price < tp；SELL 对称），与 webhook 服务一致
    p, s, t = float(price), float(sl), float(tp)
    if action == "BUY" and not (s < p < t):
        return jsonify({"error": "BUY 要求 sl < price < tp"}), 400
    if action == "SELL" and not (t < p < s):
        return jsonify({"error": "SELL 要求 tp < price < sl"}), 400

    client, cfg_err = get_trade_client()
    if client is None:
        # 未配凭证：mock 降级，明确告知而非伪装成功
        return jsonify(
            {
                "status": "ok",
                "mock": True,
                "mock_reason": cfg_err or "DASH_ORDER_SIMULATED=0（下单模拟模式）",
                "order_id": f"mock-{uuid.uuid4().hex[:16]}",
                "action": action,
                "ticker": ticker,
                "price": price, "sl": sl, "tp": tp, "size": size,
                "message": f"[mock] 未接真实模拟盘，已模拟提交 {action} {size} 张 {ticker}",
            }
        )

    cl_ord_id = "dash" + uuid.uuid4().hex[:24]   # OKX clOrdId 仅允许字母数字，不能用连字符
    try:
        order = client.place_limit_order(
            inst_id=ticker,
            side=ACTION_MAP[action],
            sz=str(size),
            px=str(p),
            take_profit=t,
            stop_loss=s,
            cl_ord_id=cl_ord_id,
            # U本位永续合约(BTC-USDT-SWAP)下单必须指定持仓方向
            pos_side="long" if action == "BUY" else "short",
        )
    except OKXTradeError as exc:
        return jsonify({"error": f"OKX 下单失败: {exc}", "mock": False}), 502
    except Exception as exc:  # noqa: BLE001  网络等未知异常
        return jsonify({"error": f"下单请求异常: {exc}", "mock": False}), 502

    return jsonify(
        {
            "status": "ok",
            "mock": False,
            "order_id": order.get("ordId") or cl_ord_id,
            "cl_ord_id": cl_ord_id,
            "action": action,
            "ticker": ticker,
            "price": price, "sl": sl, "tp": tp, "size": size,
            "message": f"[模拟盘] 已提交 {action} 限价单 {size} 张 {ticker}"
                       f" @ {p}，止盈 {tp} / 止损 {sl} 已附带",
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("DASH_PORT", "8000"))
    # 默认只绑定本机回环，更安全；需局域网访问时设 DASH_HOST=0.0.0.0
    host = os.getenv("DASH_HOST", "127.0.0.1")
    # DASH_DEBUG=1 才开调试(reloader)；默认关，避免 reloader 在后台运行时误杀子进程导致服务退出
    debug = os.getenv("DASH_DEBUG", "0").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug, use_reloader=debug)
