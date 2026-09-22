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

import datetime
import json
import logging
import math
import os
import sys
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

# 复用阶段一模块：把项目根加入搜索路径
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from okx_client import OKXClient
from logs_utils import get_logger
from atr import atr_at
from patterns import build_trend_signal, detect_engulfing, detect_pinbar
from trend import trend_allows, trend_at, trend_slope_at
from volume import volume_ratio_at, volume_signal, should_filter
# 统一策略配置层：预设 / 未验证配置告警 / 同形态最小间隔（见 strategy_config.py）
from strategy_config import (
    ENTRY_MODES,
    SIGNAL_MODES,
    blocking_reasons,
    filter_min_gap,
    pattern_kind,
    unvalidated_reasons,
    warning_lines,
)
from webhook_server.okx_trading import OKXConfigError, OKXTradeError, OKXTradingClient

#: ATR 计算周期（止损按 N×ATR 定价时使用，见 atr.py）
ATR_PERIOD = 14

app = Flask(__name__)
# 模板改动自动重载，避免每次改模板都要重启服务
app.config["TEMPLATES_AUTO_RELOAD"] = True

# 系统日志：logs/system.log（滚动文件，10MB×5）；
# werkzeug 的访问日志收敛到同文件（只记 warning 以上，减少噪音）
log = get_logger("dashboard", console=False, filename="system.log")
log_hz = get_logger("werkzeug", level=logging.WARNING, console=False, filename="system.log")

# ---------------------------------------------------------------------------
# .env 配置加载（含敏感凭证），支持 前端设置 -> 自动保存到 .env
# ---------------------------------------------------------------------------
ENV_FILE = os.path.join(_PROJECT_ROOT, ".env")

# 允许从 .env 读取并可在前端设置的键
CONFIG_KEYS = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE", "OKX_PROXY", "DASH_ACCOUNT_MODE")


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

# 允许的交易对
ALLOWED_TICKERS = {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}

# 合约面值（1张对应多少币）：优先动态拉取 OKX ctVal，失败用内置默认
DEFAULT_FACE_VALUE = {"BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1}
_face_value_cache: dict = {}
_face_cache_lock = threading.Lock()


# 内置规格兜底（OKX 2025-01 规格）：ETH 面值0.1 ETH/张、最小0.01张、步长0.01张
DEFAULT_INSTRUMENT = {
    "BTC-USDT-SWAP": {"ctVal": 0.01, "lotSz": 0.01, "minSz": 0.01},
    "ETH-USDT-SWAP": {"ctVal": 0.1, "lotSz": 0.01, "minSz": 0.01},
}
_meta_cache: dict = {}          # inst_id -> {meta, expires: ts}
_META_TTL = 600                 # 10 分钟


def get_instrument_meta(inst_id: str) -> dict:
    """合约规格 {ctVal 面值, lotSz 步长, minSz 最小张数}。动态获取带缓存，失败回退内置默认。"""
    dflt = DEFAULT_INSTRUMENT.get(inst_id) or {"ctVal": 0.01, "lotSz": 1, "minSz": 1}
    with _face_cache_lock:
        cached = _meta_cache.get(inst_id)
        if cached and cached["expires"] > time.time():
            return cached["meta"]
    try:
        import httpx
        base = os.getenv("OKX_PUBLIC_BASE") or "https://www.okx.com"
        r = httpx.get(f"{base}/api/v5/public/instruments",
                      params={"instType": "SWAP", "instId": inst_id},
                      timeout=8, proxy=os.getenv("OKX_PROXY"))
        data = r.json().get("data") or []
        if data:
            i = data[0]
            meta = {
                "ctVal": float(i.get("ctVal") or 0) or dflt["ctVal"],
                "lotSz": float(i.get("lotSz") or 0) or dflt["lotSz"],
                "minSz": float(i.get("minSz") or 0) or dflt["minSz"],
            }
            with _face_cache_lock:
                _meta_cache[inst_id] = {"meta": meta, "expires": time.time() + _META_TTL}
            return meta
    except Exception:  # noqa: BLE001
        pass
    return dflt


def get_face_value(inst_id: str) -> float:
    """兼容接口：合约面值（1张 = ctVal 个币）。"""
    return get_instrument_meta(inst_id)["ctVal"]


def fmt_sz(size: float, lot: float) -> str:
    """按步长格式化下单张数（向下取整到 lotSz 的整数倍，OKX 精度要求）。"""
    if lot >= 1:
        return str(int(size + 1e-9))
    decimals = max(0, len(str(lot).split('.')[-1]) if '.' in str(lot) else 0)
    return f"{math.floor(size / lot + 1e-9) * lot:.{decimals}f}"


CONTRACT_BTC = 0.01   # 兼容引用（默认面值）

# 风险参数
MIN_RISK_REWARD = 1.5       # 最小盈亏比阈值（低于此值不显示）
MIN_CONTRACTS = 1           # OKX BTC-USDT-SWAP 最小下单量：1张

# OKX 手续费率（普通用户）
#   入场：限价单成交 = 挂单 Maker 0.020%
#   出场：attachAlgoOrds 的 tpOrdPx/slOrdPx = "-1"（触发后【市价】平仓）= 吃单 Taker 0.050%
# 因此单笔往返真实费率 = FEE_RATE_MAKER + FEE_RATE_TAKER = 0.070%
FEE_RATE_MAKER = 0.0002     # 0.02% 挂单
FEE_RATE_TAKER = 0.0005     # 0.05% 吃单（止损止盈触发后市价平仓走这个）

# 下单类型映射：BUY/SELL -> 小写 side
ACTION_MAP = {"BUY": "buy", "SELL": "sell"}

# 固定下单数量（张），不做仓位计算
ORDER_SIZE = "0.01"

# 下单模式：DASH_ORDER_SIMULATED=1（默认）时提交到 OKX 模拟盘；
# 凭证缺失自动降级为 mock（接口不报 500，返回 mock=true 说明）。
_ORDER_SIMULATED = os.getenv("DASH_ORDER_SIMULATED", "1").lower() not in {"0", "false"}
_trade_client: OKXTradingClient | None = None
_trade_client_error: str | None = None


def account_mode() -> str:
    """当前账户模式：simulated(模拟盘,默认) / real(实盘)。"""
    return "real" if (os.getenv("DASH_ACCOUNT_MODE", "simulated").strip().lower() == "real") else "simulated"


def get_trade_client() -> tuple[OKXTradingClient | None, str | None]:
    """按账户模式初始化 OKX 交易客户端（走代理）；未配凭证返回 (None, 错误说明)。"""
    global _trade_client, _trade_client_error
    if not _ORDER_SIMULATED:
        return None, None
    if _trade_client is None and _trade_client_error is None:
        try:
            _trade_client = OKXTradingClient(
                simulated=(account_mode() == "simulated"),
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
    """获取 USDT 总权益（eq）；凭证缺失或异常返回 0。"""
    client, _ = get_trade_client()
    if client is None:
        return 0.0
    try:
        balance = client.get_balance()
        for b in balance:
            for d in (b.get("details") or []):
                if d.get("ccy") == "USDT":
                    # 优先用 eq（总权益），其次 availBal（可用余额）
                    return float(d.get("eq") or d.get("availBal") or 0)
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0


def enrich_signals_with_risk(
    signals: list[dict],
    balance: float,
    risk_pct: float | None = None,
    max_leverage: float | None = None,
    min_ratio: float | None = None,
    max_open_trades: int | None = None,
    face_value: float | None = None,
    lot: float = 1,
    min_sz: float = 1,
    require_affordable: bool = True,
) -> list[dict]:
    """仓位管理：固定风险比例模型（Fixed Fractional Risk）。

    每笔交易的风险金额固定为 账户余额 × risk_pct：
        risk_amount    = balance × risk_pct          （本笔最大亏损）
        stop_distance  = |entry - stop_loss|
        stop_pct       = stop_distance / entry
        notional       = risk_amount / stop_pct      （触及止损恰好亏 risk_amount）
        size           = notional / (entry × face_value)，向下取整到张数精度
        size < 最小张数 或 盈亏比 < min_ratio → **标记为不可交易**（默认仍保留在结果里）
        require_affordable=True 时，不可交易的信号被丢弃（下单路径用）

    边界：
        - balance ≤ 0 → 不生成任何信号
        - stop_distance = 0 → 丢弃（防除零）
        - notional 上限 = balance × max_leverage ÷ max_open_trades：
          把"余额×杠杆"的总风险预算平分给最大持仓数（默认10笔），
          保证配额内的信号可以**同时**开出 max_open_trades 笔，
          不会下 1~2 笔就把可用保证金吃光。

    ⚠️ 为什么要有 ``require_affordable``：
        账户余额很小时（如 8.36 USDT），BTC 最小 1 张的名义价值就达 760 USDT，
        所有信号算出的张数都会低于最小张数。若在这里直接丢弃，
        **信号表格会永远是空的**，用户会以为"策略没信号 / 参数不生效"，
        而真相是"余额不足以开出最小仓位"。
        因此**展示路径传 False**（保留信号 + 打 ``affordable=False`` 标记，
        并在顶部汇总条用醒目提示说明原因），**下单路径保持 True**（绝不建议开不出的仓）。
    """
    face_value = face_value or CONTRACT_BTC
    contracts_step = lot           # OKX lotSz 步长（BTC=1张, ETH=0.01张）
    min_contracts = min_sz         # OKX minSz 最小张数
    rp = risk_pct if risk_pct is not None and risk_pct > 0 else 0.01
    lev = max_leverage if max_leverage is not None else 3.0
    ratio_min = min_ratio if min_ratio is not None else MIN_RISK_REWARD
    trades_cap = max_open_trades if max_open_trades and max_open_trades > 0 else 10
    per_trade_budget = balance * lev / trades_cap   # 每笔名义仓位上限

    filtered = []
    for sig in signals:
        if balance <= 0:
            break
        entry = sig.get("price", 0)
        sl = sig.get("sl", 0)
        tp = sig.get("tp", 0)
        if not all([entry, sl, tp, entry > 0]):
            continue

        stop_distance = abs(entry - sl)
        if stop_distance == 0:          # 防除零：入场=止损
            continue
        stop_pct = stop_distance / entry

        reward_distance = abs(tp - entry)
        rr_ratio = round(reward_distance / stop_distance, 2)
        sig["risk_reward_ratio"] = rr_ratio

        # 盈亏比过滤
        if rr_ratio < ratio_min:
            continue

        # 最大名义仓位上限：总预算平分给每个并发持仓
        max_notional = per_trade_budget

        # 名义仓位 = 风险金额 / 止损百分比；超出上限则截断
        risk_amount = balance * rp
        notional = risk_amount / stop_pct

        if notional > max_notional:
            notional = max_notional

        size = notional / (entry * face_value)
        # 按步长 lotSz 向下取整（不假设整数张：BTC=1张、ETH=0.01张）
        size = math.floor(size / contracts_step) * contracts_step
        size = round(size, 8)

        # 可开出性判定：张数低于最小张数 = 该仓位**开不出来**（≠ 信号无效）
        affordable = size >= min_contracts
        if not affordable and require_affordable:
            continue

        sig["affordable"] = affordable
        sig["min_contracts"] = min_contracts
        sig["suggested_contracts"] = size
        # 开不出来时给出"至少要多少余额"的提示，避免用户对着 0 张困惑。
        # 反推：需要 notional ≥ min_contracts×entry×face_value，
        # 而 notional 上限 = balance×lev÷trades_cap  ⇒  balance ≥ notional×trades_cap÷lev
        if not affordable:
            need_notional = min_contracts * entry * face_value
            sig["required_balance"] = round(need_notional * trades_cap / lev, 2)
        sig["risk_amount"] = round(size * stop_distance * face_value, 2)  # 本笔实际最大亏损
        sig["risk_amount_plan"] = round(risk_amount, 2)                   # 计划风险金额
        sig["notional"] = round(size * entry * face_value, 2)             # 实际名义仓位
        sig["stop_pct"] = round(stop_pct, 6)
        sig["expected_profit"] = round(size * reward_distance * face_value, 2)
        sig["expected_loss"] = sig["risk_amount"]
        sig["estimated_fee"] = round(
            size * entry * face_value * (FEE_RATE_MAKER + FEE_RATE_TAKER), 2)
        sig["position_pct"] = round(rp * 100, 2)

        filtered.append(sig)

    return filtered


# ---------------------------------------------------------------------------
# K 线拉取
# ---------------------------------------------------------------------------
def fetch_klines(ticker: str, interval: str, limit: int) -> list:
    """拉取 K 线（升序），返回 lightweight-charts 格式 [{time,open,high,low,close}]。"""
    candles = _fetch_candles_cached(ticker, interval, limit)
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


# ---- K线增量缓存：缓存命中时只从 OKX 拉最新 3 根做合并 ----
_kline_cache: dict = {}                      # (ticker, interval) -> {"candles": [...], "ts": float}
_kline_cache_lock = threading.Lock()
KLINE_CACHE_TTL = 120                        # 秒；超过则全量重拉


def _fetch_candles_cached(ticker: str, interval: str, limit: int) -> list:
    """增量拉取 Candle：缓存存在且未过期时只拉最新 3 根，与缓存合并。"""
    key = (ticker, interval)
    with _kline_cache_lock:
        cached = _kline_cache.get(key)
    stale = cached is None or (time.time() - cached["ts"]) > KLINE_CACHE_TTL

    client = OKXClient(proxy=OKX_PROXY)
    try:
        if cached is None or stale or len(cached["candles"]) < limit:
            # 首次 / 缓存过期 / 缓存长度不够 → 全量
            candles = client.get_candles(ticker, bar=interval, limit=limit)
        else:
            # 增量：只拉最新 3 根，更新缓存里对应时间戳的 K 线（bar 内实时更新）
            fresh = client.get_candles(ticker, bar=interval, limit=3)
            fresh_ts = {c.ts: c for c in fresh}
            merged = [fresh_ts.get(c.ts, c) for c in cached["candles"]]
            last_ts = cached["candles"][-1].ts
            new_bars = [c for c in fresh if c.ts > last_ts]   # 新收线追加
            if new_bars:
                merged.extend(new_bars)
            candles = merged
        with _kline_cache_lock:
            _kline_cache[key] = {"candles": candles[-3000:], "ts": time.time()}
    finally:
        client.close()
    return candles if len(candles) <= limit else candles[-limit:]


# ---------------------------------------------------------------------------
# 信号检测（复用阶段一逻辑）
# ---------------------------------------------------------------------------
def atr_at(candles: list, idx: int, period: int = 14) -> float | None:
    """ATR(period)，只用 candles[:idx]（含当前根），无前视偏差。

    TR = max(high-low, |high-prev_close|, |low-prev_close|)，取最近 period 根均值。
    数据不足返回 None（调用方回退结构止损）。
    """
    if idx < period or idx >= len(candles):
        return None
    trs = []
    for j in range(idx - period + 1, idx + 1):
        c, p = candles[j], candles[j - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    if not trs:
        return None
    avg = sum(trs) / len(trs)
    return avg if avg > 0 else None


ATR_PERIOD = 14   # ATR 周期（固定 14，避免又引入一个可调参数造成过拟合）


def detect_signals(
    ticker: str,
    interval: str,
    limit: int,
    trend_period: int | None = None,
    ma_type: str | None = None,
    scan_window: int | None = None,
    volume_enabled: bool = False,
    candles: list | None = None,
    rr: float | None = None,
    atr_mult: float | None = None,
    entry_mode: str | None = None,
    min_gap: int | None = None,
    signal_mode: str | None = None,
) -> list[dict]:
    """拉取 K 线并在最近 N 根上检测信号（可选趋势过滤）。

    Args:
        trend_period: 均线周期，None/0 表示不启用趋势过滤
        ma_type: 均线类型 sma/ema
        scan_window: 扫描最近多少根 K 线；None 用模块级 SIGNAL_SCAN
        candles: 外部传入的 Candle 列表（回测用，跳过 _fetch_candles_cached；
            必须为升序，长度 ≥ limit 供大窗口扫描）
        rr: 盈亏比 (Reward/Risk)，决定目标价距离 = 止损距离 × rr；None 用默认 2.0
        atr_mult: 止损改用 ATR 倍数（可选）。None/0 = 保持结构止损（默认）。
        entry_mode: 入场方式。"close"（默认）= 形态收盘价入场，行为不变；
            "breakout" = 等价格越过形态极值才进场。未知取值回退 "close"。
        min_gap: 同形态（按 pinbar / engulfing 分类）最小间隔根数。
            None/0/1 = 不过滤（默认行为不变）。
        signal_mode: 信号来源。"pattern"（默认）= 形态触发，行为不变；
            "trend" = 纯趋势模式（不看形态，方向由 MA 斜率决定，止损按 N×ATR）。
            未知取值回退 "pattern"。

    返回列表，每个元素:
        {action, price, sl, tp, strategy, ratio, timestamp, result, result_ts}
    """
    tp_ = (trend_period or 0) if trend_period is not None else TREND_PERIOD
    mt_ = ma_type or MA_TYPE
    sw_ = scan_window if scan_window is not None else SIGNAL_SCAN
    # 信号来源：未知取值一律回退 pattern（保证既有行为不受影响）
    sm_ = str(signal_mode or "pattern").strip().lower()
    if sm_ not in SIGNAL_MODES:
        sm_ = "pattern"
    try:
        rr_ = float(rr) if rr is not None and float(rr) > 0 else 2.0
    except (TypeError, ValueError):
        rr_ = 2.0
    try:
        am_ = float(atr_mult) if atr_mult is not None and float(atr_mult) > 0 else None
    except (TypeError, ValueError):
        am_ = None
    # 入场方式：未知取值一律回退 close（保证既有行为不受影响）
    em_ = str(entry_mode or "close").strip().lower()
    if em_ not in ENTRY_MODES:
        em_ = "close"
    # 同形态最小间隔：非法/<=1 一律不过滤（默认行为不变）
    try:
        gap_ = max(0, int(float(min_gap))) if min_gap is not None else 0
    except (TypeError, ValueError):
        gap_ = 0

    if candles is None:
        candles = _fetch_candles_cached(ticker, interval, limit)

    scan = min(sw_, len(candles) - 1)
    # 先收集候选（含时间序 idx），再统一做最小间隔过滤，最后判定结果
    candidates: list[tuple[int, object, object]] = []   # (idx, pattern, ratio)
    # 从旧到新扫描，保证返回按时间升序
    for idx in range(len(candles) - scan, len(candles)):
        cur = candles[idx]
        # ATR 仅在需要时计算（省掉默认路径的开销）
        atr_v = atr_at(candles, idx, ATR_PERIOD) if (am_ or sm_ == "trend") else None

        if sm_ == "trend":
            # 纯趋势模式：不看形态。方向由 MA 斜率决定，止损按 N×ATR。
            if not tp_ or not atr_v:
                continue
            direction = trend_slope_at(candles, idx, tp_, mt_, atr=atr_v)
            if direction is None:
                continue   # 横盘/无趋势：不发信号
            action = "BUY" if direction == "up" else "SELL"
            p = build_trend_signal(cur, action, atr_v,
                                   atr_mult=(am_ or 1.5), rr=rr_)
            if p is not None:
                candidates.append((idx, p, None))
            continue

        ratio = volume_ratio_at(candles, idx) if volume_enabled else None
        for pattern in (detect_pinbar(cur, volume_ratio=ratio, rr=rr_,
                                      atr=atr_v, atr_mult=am_, entry_mode=em_),
                        detect_engulfing(candles[idx - 1], cur, volume_ratio=ratio, rr=rr_,
                                         atr=atr_v, atr_mult=am_, entry_mode=em_)):
            if pattern is None:
                continue
            if tp_:
                trend = trend_at(candles, idx, tp_, mt_)
                if not trend_allows(pattern, trend):
                    continue
            if volume_enabled and should_filter(ratio, pattern.risk_reward):
                continue
            candidates.append((idx, pattern, ratio))

    # 同形态最小间隔降频：形态会聚集，后续信号多为"追单"（见 STRATEGY_CONCLUSIONS.md）
    # gap_ <= 1 时原样返回，既有行为逐字节不变
    if gap_ > 1:
        candidates = filter_min_gap(
            candidates, gap_,
            kind_of=lambda it: pattern_kind(it[1].name),
            order_of=lambda it: it[0],
        )

    signals: list[dict] = []
    for idx, pattern, ratio in candidates:
        # 用信号之后的价格走势判定成功/失败/持仓中
        result, result_ts = evaluate_signal(idx, pattern, candles)
        sig = {
            "action": pattern.action,
            "price": pattern.entry,
            "sl": pattern.stop,
            "tp": pattern.take_profit,
            "strategy": pattern.name,
            "ratio": pattern.risk_reward,
            "timestamp": candles[idx].ts // 1000,
            "result": result,          # WIN / LOSS / OPEN
            "result_ts": result_ts // 1000 if result_ts else None,
        }
        if volume_enabled:
            sig.update(
                volume_ratio=ratio,
                volume_signal=volume_signal(ratio),
                volume_confirm=pattern.volume_confirm,
            )
        signals.append(sig)
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
    # 盈亏比：目标价 = 入场 ± 止损距离 × rr（默认 2.0，可调 0.5~20）
    rr = request.args.get("rr", 2.0, type=float)
    # 止损口径：atr=多倍ATR（如 1.5），0/空 = 结构止损（默认）
    atr_mult = request.args.get("atr", 0, type=float)
    # 入场方式：entry=close|breakout（默认 close，未知取值回退 close）
    entry_mode = str(request.args.get("entry", "close") or "close").strip().lower()
    if entry_mode not in ENTRY_MODES:
        entry_mode = "close"
    # 同形态最小间隔：gap=根数（<=1 = 不过滤，默认 0）
    try:
        min_gap = int(float(request.args.get("gap", 0) or 0))
        min_gap = min(200, min_gap) if min_gap > 1 else 0
    except (TypeError, ValueError):
        min_gap = 0
    # 信号来源：mode=pattern|trend（默认 pattern，未知取值回退 pattern）
    signal_mode = str(request.args.get("mode", "pattern") or "pattern").strip().lower()
    if signal_mode not in SIGNAL_MODES:
        signal_mode = "pattern"

    if ticker not in ALLOWED_TICKERS:
        return jsonify({"error": f"暂只支持 {sorted(ALLOWED_TICKERS)}"}), 400

    try:
        options = dict(trend_period=trend_period, ma_type=ma_type,
                       scan_window=scan_window,
                       rr=max(0.5, min(20.0, rr or 2.0)),
                       atr_mult=(atr_mult if atr_mult and 0 < atr_mult <= 10 else None),
                       entry_mode=entry_mode, min_gap=min_gap,
                       signal_mode=signal_mode)
        if volume_option in {"1", "true"}:
            options["volume_enabled"] = True
        signals = detect_signals(ticker, interval, limit, **options)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"信号检测失败: {exc}"}), 502

    # 时间倒序（最新的在前），便于前端表格展示
    signals.reverse()

    # 标记进行中K线的信号（formings：形态会重绘，收盘前可能消失）
    _bar_s = BAR_SECONDS.get(interval, 300)   # timestamp 已是秒级
    _now_s = time.time()
    for s in signals:
        s["forming"] = bool(s.get("timestamp", 0) + _bar_s > _now_s)

    # 仓位计算：余额×risk_pct%风险 / 止损距离 → 建议张数；过滤盈亏比<1.5 & 建议金额不足
    balance = get_usdt_balance()
    risk_pct = request.args.get("risk_pct", type=float)
    max_lev = request.args.get("max_lev", type=float)
    max_trades = request.args.get("max_trades", type=int)
    meta = get_instrument_meta(ticker)
    # 展示路径：require_affordable=False —— 余额不足以开出最小仓位时，
    # 仍返回信号（带 affordable=False 标记），否则表格会永远为空、
    # 用户会误以为"策略没信号 / 参数不生效"。真正的下单路径仍会拦掉它们。
    signals = enrich_signals_with_risk(
        signals, balance, risk_pct=risk_pct,
        max_leverage=max_lev, max_open_trades=max_trades,
        face_value=meta["ctVal"], lot=meta["lotSz"], min_sz=meta["minSz"],
        require_affordable=False)
    for s in signals:
        s["face_value"] = meta["ctVal"]     # 1张 = ctVal 币
        s["lot_sz"] = meta["lotSz"]         # 张数步长
        s["min_sz"] = meta["minSz"]         # 最小张数
        s["ticker"] = ticker

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
            "account_mode": account_mode(),
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
    keys = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE", "OKX_PROXY", "DASH_ACCOUNT_MODE")
    # 只更新传入的键；空字符串视为清除（账户模式除外）
    updates = {}
    for k in keys:
        if k in data:
            updates[k] = data[k].strip()
    if "DASH_ACCOUNT_MODE" in updates and updates["DASH_ACCOUNT_MODE"] not in ("simulated", "real"):
        return jsonify({"error": "DASH_ACCOUNT_MODE 只能是 simulated 或 real"}), 400
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
    log.info("配置更新 keys=%s 凭证完整=%s 客户端=%s 模式=%s",
             sorted(updates),
             all(os.getenv(k) for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE")),
             "OK" if client else f"失败:{cfg_err}",
             account_mode())
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

    # 杠杆倍数（可选）：限价单数量不变，开仓前设置该交易对保证金倍数
    lever = data.get("lever")
    lever_str = ""
    if lever is not None:
        try:
            lever_f = min(20.0, max(1.0, float(lever)))
            lever_str = str(int(lever_f)) if lever_f == int(lever_f) else f"{lever_f:g}"
        except (TypeError, ValueError):
            lever_str = ""
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
                "price": price, "sl": sl, "tp": tp, "size": size, "lever": lever,
                "message": f"[mock] 未接真实模拟盘，已模拟提交 {action} {size} 张 {ticker}",
            }
        )

    cl_ord_id = "dash" + uuid.uuid4().hex[:24]   # OKX clOrdId 仅允许字母数字，不能用连字符
    pos_side_ = "long" if action == "BUY" else "short"
    # 按弹窗选择的倍数设置该交易对杠杆（先设杠杆再下单；空头/多头分别设置）
    if lever_str:
        try:
            client.set_leverage(inst_id=ticker, lever=lever_str, pos_side=pos_side_)
        except OKXTradeError as lev_exc:
            # 对冲模式下 posSide 可能不被接受（净模式账户），去掉 posSide 再试一次
            try:
                client.set_leverage(inst_id=ticker, lever=lever_str)
            except OKXTradeError as lev_exc2:
                return jsonify({
                    "error": f"设置杠杆失败: {lev_exc2}（账户模式或倍数不被支持）",
                    "mock": False,
                }), 502
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass   # 杠杆已设置过/等于当前值时 OKX 可能报错，不阻断下单
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
            pos_side=pos_side_,
        )
    except OKXTradeError as exc:
        log.error("手动下单失败 %s %s %s张 @%s: %s", action, ticker, size, p, exc)
        return jsonify({"error": f"OKX 下单失败: {exc}", "mock": False}), 502
    except Exception as exc:  # noqa: BLE001  网络等未知异常
        log.error("手动下单异常 %s %s: %s", action, ticker, exc)
        return jsonify({"error": f"下单请求异常: {exc}", "mock": False}), 502

    log.info("手动下单 %s %s %s张 @%s 杠杆%s 订单号%s 模式%s",
             action, ticker, size, p, lever_str or "-",
             order.get("ordId") or cl_ord_id, account_mode())

    return jsonify(
        {
            "status": "ok",
            "mock": False,
            "order_id": order.get("ordId") or cl_ord_id,
            "cl_ord_id": cl_ord_id,
            "action": action,
            "ticker": ticker,
            "price": price, "sl": sl, "tp": tp, "size": size,
            "message": f"[{'实盘🔴' if account_mode() == 'real' else '模拟盘🟢'}] 已提交 {action} 限价单 {size} 张 {ticker}"
                       f" @ {p}，杠杆 {lever_str or '默认'}x，止盈 {tp} / 止损 {sl} 已附带",
            "account_mode": account_mode(),
        }
    )


# ---------------------------------------------------------------------------
# 自动交易引擎

_auto_trade_config = {
    "enabled": False,
    "ticker": "BTC-USDT-SWAP",
    "interval": "5m",
    "scan_window": 50,
    "limit": 100,
    "max_open_trades": 10,      # 最大同时持仓数
    "cooldown_seconds": 300,    # 同方向信号冷却时间（5分钟）
    "min_rr_ratio": 1.5,        # 最小盈亏比
    "max_leverage": 3.0,        # 最大杠杆倍数（决定每笔仓位预算上限）
    "volume_filter": True,       # 量能确认（默认开）
    "trend_filter": True,        # 趋势过滤（默认开）
    "trend_period": 50,          # 均线周期（默认50）
    "ma_type": "sma",            # 均线类型
    "rr": 2.0,                   # 盈亏比（目标价 = 止损距离 × rr）
    "atr_mult": 0,               # 止损口径：0=结构止损（默认）；>0=改用 N×ATR(14)
    "entry_mode": "close",       # 入场方式：close=形态收盘价（默认）；breakout=突破形态极值
    "min_gap": 0,                # 同形态最小间隔(根)：0=不过滤（默认）；>1=降频
    "signal_mode": "pattern",    # 信号来源：pattern=形态触发（默认）；trend=纯趋势（不用形态）
}
# 未验证配置的显式确认标记（内存态）：本次"开始自动交易"是否已确认过告警
_auto_trade_unvalidated_ack = {"acknowledged": False}
_auto_trade_log: list[dict] = []     # 最近200条自动交易日志
_auto_trade_positions: dict = {}     # 活跃仓位跟踪 {signal_key: {entry, sl, tp, side, size, time}}
_auto_trade_history: list[dict] = [] # 已成交订单历史（持久化）
_auto_pending_orders: dict = {}      # 挂单跟踪 {ord_id: {..., sig_ts: N根K线开盘ts}}
_auto_failed_sigs: dict = {}         # 本根K线内下单失败的信号 {sig_key: sig_ts_ms} —— 同根不再重试

BAR_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1H": 3600, "2H": 7200, "4H": 14400, "1D": 86400,
}


def _bar_seconds(interval: str) -> int:
    """K线周期 → 秒。未知周期默认 300（5分钟）。"""
    return BAR_SECONDS.get(interval, 300)
_auto_trade_lock = threading.Lock()
_auto_trade_thread: threading.Thread | None = None

# ---- 自动交易状态持久化（重启不丢）----
# 路径可用环境变量 PA_AUTO_TRADE_STATE 覆盖：测试/并发实例应指向临时文件，
# 避免把真实运行状态（配置、成交历史、持仓跟踪）写脏或覆盖。
_AUTO_TRADE_STATE_FILE = os.environ.get(
    "PA_AUTO_TRADE_STATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_trade_state.json"))


def _load_auto_trade_state() -> None:
    """启动时从磁盘恢复日志 / 持仓跟踪 / 成交历史。"""
    global _auto_trade_log, _auto_trade_positions, _auto_trade_history
    global _auto_trade_config
    try:
        with open(_AUTO_TRADE_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[auto-trade] 状态文件损坏，忽略: {exc}")
        return
    # 配置恢复（enabled 不恢复——重启后不自动交易，需手动开始）
    saved_cfg = data.get("config", {})
    if isinstance(saved_cfg, dict):
        for k in ("ticker", "interval", "limit", "scan_window", "max_open_trades",
                  "cooldown_seconds", "min_rr_ratio", "max_leverage", "volume_filter",
                  "trend_filter", "trend_period", "ma_type", "rr", "atr_mult",
                  "entry_mode", "min_gap", "signal_mode"):
            if k in saved_cfg:
                _auto_trade_config[k] = saved_cfg[k]
    _auto_trade_config["enabled"] = False
    _auto_trade_log = data.get("log", [])[-50:]
    _auto_trade_history = data.get("history", [])[-500:]
    _auto_trade_positions = data.get("positions", {})
    _auto_pending_orders.update(data.get("pending", {}))
    # 清理过期的持仓跟踪（超过冷却时间2倍的）
    now = time.time()
    cooldown = _auto_trade_config["cooldown_seconds"] * 2
    _auto_trade_positions = {
        k: v for k, v in _auto_trade_positions.items()
        if isinstance(v, dict) and now - v.get("time", 0) < cooldown
    }
    print(f"[auto-trade] 已恢复状态：历史{len(_auto_trade_history)}条 "
          f"持仓跟踪{len(_auto_trade_positions)}条")


def _save_auto_trade_state() -> None:
    """原子写入状态文件（tmp + rename）。"""
    with _auto_trade_lock:
        snapshot = {
            "config": dict(_auto_trade_config),
            "log": _auto_trade_log[-200:],
            "positions": {k: dict(v) for k, v in _auto_trade_positions.items()},
            "history": _auto_trade_history[-200:],
            "pending": {k: dict(v) for k, v in _auto_pending_orders.items()},
        }
    tmp = _AUTO_TRADE_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _AUTO_TRADE_STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"[auto-trade] 状态保存失败: {exc}")


_load_auto_trade_state()


def _auto_trade_scan_and_execute():
    """自动交易核心：扫描信号 → 过滤 → 下单 → 跟踪。"""
    while True:
        try:
            with _auto_trade_lock:
                cfg = dict(_auto_trade_config)
            if not cfg["enabled"]:
                time.sleep(10)
                continue

            ticker = cfg["ticker"]
            client, _ = get_trade_client()
            if client is None:
                time.sleep(30)
                continue

            # 挂单有效期检查（1根K线）：未成交到点撤销
            try:
                _auto_check_pending_orders(client, cfg["interval"])
            except Exception as exc:
                _auto_log(f"挂单检查异常: {exc}", success=False)

            # 对账清理：positions 里的占位超过 冷却时间×2 即失效，
            # 防止撤单/平仓后残留记录无限累积（污染冷却判断与面板显示）
            _auto_prune_positions(cfg["cooldown_seconds"] * 2)

            # 检查当前持仓数
            try:
                positions = client.get_positions(ticker)
                open_count = sum(1 for p in positions if float(p.get("pos", 0)) != 0)
            except Exception:
                open_count = 0

            if open_count >= cfg["max_open_trades"]:
                _auto_log(f"持仓数 {open_count} ≥ {cfg['max_open_trades']}，跳过")
                time.sleep(30)
                continue

            # 扫描参数（与表盘同源：ticker/周期/窗口/量能/趋势/倍数）
            _meta = get_instrument_meta(ticker)   # 币种规格：面值/步长/最小张数
            scan_cfg_msg = (f"{ticker} {cfg['interval']} K线{cfg['limit']} "
                            f"窗口{cfg['scan_window']} 量能{'开' if cfg['volume_filter'] else '关'} "
                            f"趋势{cfg.get('trend_period', 0) or '关'} "
                            f"入场{cfg.get('entry_mode', 'close')} "
                            f"来源{cfg.get('signal_mode', 'pattern')} "
                            f"止损{('ATR×%g' % cfg['atr_mult']) if cfg.get('atr_mult') else '结构'} "
                            f"间隔{cfg.get('min_gap', 0) or '无'} 倍数{cfg.get('max_leverage', 3.0)}")
            # 扫描信号
            try:
                balance = get_usdt_balance()
                signals = detect_signals(
                    ticker, cfg["interval"], cfg["limit"],
                    scan_window=cfg["scan_window"],
                    volume_enabled=cfg["volume_filter"],
                    trend_period=cfg.get("trend_period", 0),
                    ma_type=cfg.get("ma_type", "sma"),
                    rr=cfg.get("rr") or 2.0,
                    atr_mult=cfg.get("atr_mult") or None,
                    entry_mode=cfg.get("entry_mode") or "close",
                    min_gap=cfg.get("min_gap") or 0,
                    signal_mode=cfg.get("signal_mode") or "pattern",
                )
                _meta = get_instrument_meta(ticker)
                # require_affordable=False：**保留**开不出的信号，好在日志里说清
                # "不是没信号，而是余额开不出最小仓位"。
                # 下单仍由下面的逐条判定拦截（绝不真的开出开不出的仓）。
                signals = enrich_signals_with_risk(
                    signals, balance, max_leverage=cfg.get("max_leverage", 3.0),
                    max_open_trades=cfg["max_open_trades"],
                    face_value=_meta["ctVal"], lot=_meta["lotSz"], min_sz=_meta["minSz"],
                    require_affordable=False)
            except Exception as exc:
                _auto_log(f"扫描失败: {exc}")
                time.sleep(30)
                continue

            # 筛选可下单信号 — 只交易"最新一根【已收盘】K线"的信号
            # （进行中的K线形态会随价格变动/重绘，收盘后可能消失，不用于交易）
            now = time.time()
            bar = _bar_seconds(cfg["interval"])          # K线周期(秒)；timestamp 同为秒
            # 最新已收盘 K 线时间戳 = 最新一根"开盘ts+周期<=now"的K线
            closed_sigs = [s for s in signals if s.get("timestamp", 0) + bar <= now]
            forming_sigs = [s for s in signals
                            if s.get("timestamp", 0) + bar > now]

            # 进行中K线的信号：只打印，不交易（等收盘确认）
            for sig in forming_sigs:
                desc = (f"{sig['action']} {sig['strategy']} @{sig['price']:.1f} "
                        f"SL:{sig['sl']:.1f} TP:{sig['tp']:.1f} RR:{sig['risk_reward_ratio']}")
                _auto_log(f"🚧 进行中K线信号（未收盘，等收盘确认）{desc}")

            # 找最新【已收盘】K线的信号
            # 注意：以"日历上的最新收盘K线"为基准（floor(now/bar) 的前一根），
            # 而不是"有信号的最新K线"——否则当新收盘的K线没有信号时，
            # 一根旧K线的信号会被反复当成最新信号重试下单（如15:45的信号在17点仍被交易）
            bar_s = int(bar)   # bar 本身为秒
            latest_ts = (int(now) // bar_s) * bar_s - bar_s
            latest_count = sum(1 for sig in closed_sigs if sig.get("timestamp", 0) == latest_ts)

            # —— 本轮扫描摘要（检测到K线信号必打印）——
            # 区分两种"看不到东西"：① 真的没有形态 ② 有信号但余额开不出
            # （②若不区分，会显示成"本轮无信号"，让人误以为策略或参数失效）
            if not signals:
                _auto_log(f"🔍 扫描 {scan_cfg_msg} → 本轮无信号")
            else:
                extra = f"，另进行中{len(forming_sigs)}个(不交易)" if forming_sigs else ""
                unaffordable = [s for s in signals if not s.get("affordable", True)]
                if unaffordable and len(unaffordable) == len(signals):
                    need = max((s.get("required_balance") or 0) for s in unaffordable)
                    _auto_log(
                        f"🔍 扫描 {scan_cfg_msg} → 检测到{len(signals)}个信号，"
                        f"但余额不足全部开不出（需余额 ≥ {need:.0f} USDT，"
                        f"当前 {balance:.2f}）{extra}", success=False)
                else:
                    _auto_log(f"🔍 扫描 {scan_cfg_msg} → 已收盘信号{len(closed_sigs)}个，"
                              f"最新收盘K线{latest_count}个{extra}"
                              + (f"，其中{len(unaffordable)}个余额不足" if unaffordable else ""))

            for sig in closed_sigs:
                ts = sig.get("timestamp", 0)
                desc = (f"{sig['action']} {sig['strategy']} @{sig['price']:.1f} "
                        f"SL:{sig['sl']:.1f} TP:{sig['tp']:.1f} RR:{sig['risk_reward_ratio']}")
                # 非最新K线的信号（历史参考），简要打印
                if ts < latest_ts:
                    _auto_log(f"📄 历史K线信号（不交易）{desc}")
                    continue
                # —— 以下为最新K线信号，逐个打印判定 ——
                if not sig.get("affordable", True) or sig.get("suggested_contracts", 0) < _meta["minSz"]:
                    need = sig.get("required_balance")
                    need_txt = f"，建议余额 ≥ {need:.0f} USDT" if need else ""
                    _auto_log(f"⏭️ 最新K线信号 {desc}｜资金不足：最小{_meta['minSz']}张"
                              f"（名义 {_meta['minSz'] * sig['price'] * _meta['ctVal']:.0f} USDT）"
                              f"超出当前余额可开仓位{need_txt}，不下单", success=False)
                    continue
                if sig.get("risk_reward_ratio", 0) < cfg["min_rr_ratio"]:
                    _auto_log(f"⏭️ 最新K线信号 {desc}｜盈亏比<{cfg['min_rr_ratio']}，跳过")
                    continue

                # 冷却检查：同方向+同价格附近不重复下单
                sig_key = f"{sig['action']}_{sig['price']:.0f}"
                # 本根K线内已重试失败过的信号不重复下单（下一根K线重新算新信号）
                if _auto_failed_sigs.get(sig_key) == ts:
                    _auto_log(f"⏭️ 最新K线信号 {desc}｜本根K线重试失败过，本根内不再重试")
                    continue
                if sig_key in _auto_trade_positions:
                    _auto_log(f"⏭️ 最新K线信号 {desc}｜重复信号已持仓跟踪，跳过")
                    continue

                # 冷却时间检查
                for pos_key, pos_info in _auto_trade_positions.items():
                    if (pos_info["side"] == sig["action"]
                            and now - pos_info["time"] < cfg["cooldown_seconds"]):
                        _auto_log(f"⏭️ 最新K线信号 {desc}｜{sig['action']}方向冷却中"
                                  f"({int(cfg['cooldown_seconds'] - (now - pos_info['time']))}s)，跳过")
                        break
                else:
                    # 没有冷却中的同方向仓位 → 下单
                    ok = _execute_auto_trade(sig, ticker, client)
                    if not ok:
                        _auto_failed_sigs[sig_key] = ts   # 本根K线内不再重试该信号
                        if len(_auto_failed_sigs) > 200:
                            _auto_failed_sigs.clear()
                    time.sleep(2)  # 下单间隔

            time.sleep(30)  # 扫描间隔

        except Exception as exc:
            _auto_log(f"自动交易异常: {exc}")
            time.sleep(60)


def _execute_auto_trade(sig: dict, ticker: str, client):
    """执行单笔自动交易。"""
    _meta = get_instrument_meta(ticker)   # 币种规格：ctVal / lotSz / minSz
    size = sig["suggested_contracts"]
    entry = sig["price"]
    sl = sig["sl"]
    tp = sig["tp"]
    side = sig["action"]

    # ---- SL/TP 价格校验（OKX 要求）----
    if side == "BUY":
        if sl >= entry:
            _auto_log(f"⚠️ {side} SL:{sl} ≥ 入场:{entry}，跳过", success=False)
            return
        if tp <= entry:
            _auto_log(f"⚠️ {side} TP:{tp} ≤ 入场:{entry}，跳过", success=False)
            return
    else:  # SELL
        if sl <= entry:
            _auto_log(f"⚠️ {side} SL:{sl} ≤ 入场:{entry}，跳过", success=False)
            return
        if tp >= entry:
            _auto_log(f"⚠️ {side} TP:{tp} ≥ 入场:{entry}，跳过", success=False)
            return

    cl_ord_id = "auto" + uuid.uuid4().hex[:22]
    sig_ts = sig.get("timestamp")  # 信号K线(N)开盘时间戳（毫秒）
    # 信号所在K线定位（成功/失败/重试日志统一带）：开盘本地时间 + 周期级别
    bar_s = _bar_seconds(cfg_interval := _auto_trade_config.get("interval", "5m"))
    sig_bar = ""
    if sig_ts:
        bar_dt = datetime.datetime.fromtimestamp(sig_ts).strftime("%m-%d %H:%M")
        sig_bar = f"｜信号K线: {bar_dt} ({bar_s // 60}分钟级)"
    try:
        # OKX 偶发 50001/503 服务临时不可用 → 自动重试（最多3次，间隔2s/4s/8s）
        order = None
        for attempt in range(3):
            try:
                order = client.place_limit_order(
                    inst_id=ticker,
                    side=ACTION_MAP[side],
                    sz=fmt_sz(size, _meta["lotSz"]),
                    px=str(entry),
                    take_profit=tp,
                    stop_loss=sl,
                    cl_ord_id=cl_ord_id,
                    pos_side="long" if side == "BUY" else "short",
                )
                break
            except Exception as exc:
                retryable = ("50001" in str(exc) or "503" in str(exc)
                             or "temporarily unavailable" in str(exc).lower())
                if retryable and attempt < 2:
                    wait_s = 2 * (2 ** attempt)
                    _auto_log(f"⚠️ OKX服务临时不可用(50001)，{wait_s}s后重试 "
                              f"({attempt + 1}/3) {side} {size}张 @{entry}{sig_bar}", success=None)

                    time.sleep(wait_s)
                    continue
                raise
        if order is None:
            raise RuntimeError("下单重试耗尽")
        # 记录
        with _auto_trade_lock:
            sig_key = f"{side}_{entry:.0f}"
            _auto_trade_positions[sig_key] = {
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "size": size,
                "time": time.time(),
                "ord_id": order.get("ordId"),
            }
            ord_id = order.get("ordId")
            _auto_trade_history.append({
                "time": datetime.datetime.now().strftime("%m-%d %H:%M:%S"),
                "ts": time.time(),
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "size": size,
                "rr": sig.get("risk_reward_ratio"),
                "risk_amount": sig.get("risk_amount"),
                "ord_id": ord_id,
                "ticker": ticker,
                "status": "pending",   # pending → filled / expired
                "sig_ts": sig_ts,
            })
            if len(_auto_trade_history) > 500:
                _auto_trade_history.pop(0)
            # 挂单跟踪：有效期 = 1根K线（第 N+1 根收盘时未成交即撤销）
            if ord_id:
                _auto_pending_orders[str(ord_id)] = {
                    "ticker": ticker,
                    "side": side,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "size": size,
                    "sig_ts": sig_ts * 1000,   # 毫秒（sig_ts 为秒）
                    "placed_at": time.time(),
                    "cl_ord_id": cl_ord_id,
                    "sig_key": sig_key,
                }
        _save_auto_trade_state()
        _auto_log(
            f"✅ {side} {size}张 @{entry} "
            f"SL:{sl} TP:{tp} RR:{sig['risk_reward_ratio']}{sig_bar}",
            success=True,
        )
        return True
    except Exception as exc:
        _auto_log(f"❌ {side} {size}张 @{entry} 失败{sig_bar}: {exc}", success=False)
        return False


def _auto_prune_positions(max_age_seconds: float) -> int:
    """清理超过 max_age_seconds 的持仓跟踪占位（对账用）。

    `_auto_trade_positions` 只在启动时清理过一次，运行期若不回收，
    撤单/平仓后的残留会无限累积：冷却判断的遍历集合持续膨胀，
    面板"活跃仓位"也会显示早已失效的条目。这里按时间兜底回收，
    返回清理条数（有清理才落盘，避免每轮都写文件）。
    """
    now = time.time()
    with _auto_trade_lock:
        stale = [k for k, v in _auto_trade_positions.items()
                 if not isinstance(v, dict) or now - v.get("time", 0) >= max_age_seconds]
        for k in stale:
            _auto_trade_positions.pop(k, None)
    if stale:
        _save_auto_trade_state()
    return len(stale)


def _auto_check_pending_orders(client, interval: str):
    """挂单有效期检查 = 1 根 K 线。

    流程：
      1. 信号在第 N 根 K 线收盘后生成 → 挂单（记录 sig_ts=N 根开盘时间戳）；
      2. 第 N+1 根 K 线收盘时（now >= sig_ts + 2×bar）检查订单状态：
         - 已成交 → 进入正常持仓（交易所侧自带止盈止损）；从跟踪中移除；
         - 未成交 → 调用 OKX 撤单接口撤销；信号作废，不再补挂。
    """
    bar = _bar_seconds(interval) * 1000  # 毫秒
    now_ms = time.time() * 1000
    with _auto_trade_lock:
        entries = {oid: dict(po) for oid, po in _auto_pending_orders.items()}
    for oid, po in entries.items():
        sig_ts = po.get("sig_ts") or 0
        if sig_ts and sig_ts < 1e11:            # 兼容旧的秒级数据 → 转毫秒
            sig_ts *= 1000
        expiry_ms = (sig_ts + 2 * bar) if sig_ts else (po.get("placed_at", 0) + 2 * bar / 1000) * 1000
        if now_ms < expiry_ms:
            continue  # 还在第 N+1 根K线内，继续等待
        # 到期：查订单状态
        state = None
        try:
            od = client.get_order(po["ticker"], ord_id=oid)
            state = od.get("state")
        except Exception as exc:
            # 查不到该订单（已归档/已撤销）→ 视为已处理
            with _auto_trade_lock:
                _auto_pending_orders.pop(oid, None)
            _auto_log(f"⏰ 挂单 {po['side']} @{po['entry']} 查询失败({exc})，移除跟踪", success=True)
            continue
        if state in ("filled", "partially_filled"):
            with _auto_trade_lock:
                _auto_pending_orders.pop(oid, None)
                # 已成交 → 保留 positions 跟踪（真正持仓中，靠冷却时间自然过期）
                for h in reversed(_auto_trade_history):
                    if str(h.get("ord_id")) == oid:
                        h["status"] = "filled"
                        h["fill_px"] = od.get("fillPx") or od.get("avgPx")
                        break
            _save_auto_trade_state()
            _auto_log(f"✅ 挂单已成交 {po['side']} {po['size']}张 @{po['entry']}（持仓进入SL/TP管理）", success=True)
        elif state in ("canceled",):
            with _auto_trade_lock:
                _auto_pending_orders.pop(oid, None)
                # 已撤销 → 从未持仓，必须同时清掉 positions 里的占位，
                # 否则这个未成交信号会一直占着冷却名额
                _auto_trade_positions.pop(po.get("sig_key"), None)
            _save_auto_trade_state()
        else:
            # 未成交 → 撤单，信号作废
            try:
                client.cancel_limit_order(po["ticker"], ord_id=oid)
                _auto_log(f"⏰ 第N+1根收盘未成交，撤单 {po['side']} {po['size']}张 @{po['entry']}（信号作废，不再补挂）", success=None)
            except Exception as exc:
                _auto_log(f"⚠️ 撤单失败 {po['side']} @{po['entry']}: {exc}（下一轮重试）", success=False)
                continue
            with _auto_trade_lock:
                _auto_pending_orders.pop(oid, None)
                # 撤单成功 → 撤销持仓占位（该信号从未真正成交）
                _auto_trade_positions.pop(po.get("sig_key"), None)
                for h in reversed(_auto_trade_history):
                    if str(h.get("ord_id")) == oid:
                        h["status"] = "expired"
                        break
            _save_auto_trade_state()


def _auto_log(msg: str, success: bool | None = None):
    """记录自动交易日志（最多50条）并持久化；同时写入系统日志文件。"""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    entry = {"time": ts, "msg": msg, "success": success}
    with _auto_trade_lock:
        _auto_trade_log.append(entry)
        if len(_auto_trade_log) > 200:
            _auto_trade_log.pop(0)
    _save_auto_trade_state()
    log.info("自动交易 %s", msg)


def _start_auto_trade_thread():
    """启动自动交易后台线程（只启动一次）。"""
    global _auto_trade_thread
    if _auto_trade_thread is None or not _auto_trade_thread.is_alive():
        _auto_trade_thread = threading.Thread(
            target=_auto_trade_scan_and_execute,
            daemon=True,
            name="auto-trade",
        )
        _auto_trade_thread.start()


# 回测允许的最大 K 线数（一年 15m≈35040；OKX_MAX_CANDLES 覆盖后可更大）
BT_MAX_BARS = min(60000, int(os.getenv("OKX_MAX_CANDLES", "60000")))


@app.route("/api/backtest", methods=["GET"])
def api_backtest():
    """长周期回测：对最近 N 根 K 线上的信号统计 止盈/止损/持仓中 与胜率。

    走磁盘缓存（.cache 目录）：整年数据首次拉取约 1~4 分钟，
    之后秒出。term 端点保留量取尽后自动切 history-candles 翻页。

    Query:
        ticker, interval, bars(1..BT_MAX_BARS), trend(0/均线周期), volume(1/0),
        rr, atr(ATR倍数), entry(close|breakout), gap(同形态最小间隔根数)
    """
    t0 = time.time()
    ticker = request.args.get("ticker", DEFAULT_TICKER).upper()
    interval = request.args.get("interval", "15m")
    try:
        bars = int(request.args.get("bars", 35040))
    except (TypeError, ValueError):
        return jsonify({"error": "bars 必须为整数"}), 400
    bars = min(max(bars, 50), BT_MAX_BARS)
    try:
        trend = int(request.args.get("trend", 0) or 0)
    except (TypeError, ValueError):
        trend = 0
    volume_on = request.args.get("volume", "0") in ("1", "true", "on")
    try:
        rr = float(request.args.get("rr", 2.0))
        rr = max(0.5, min(20.0, rr))
    except (TypeError, ValueError):
        rr = 2.0
    try:
        atr_mult = float(request.args.get("atr", 0) or 0)
        atr_mult = atr_mult if 0 < atr_mult <= 10 else None   # 0/超界 = 用结构止损
    except (TypeError, ValueError):
        atr_mult = None
    # 入场方式（未知取值回退 close）与同形态最小间隔（<=1 = 不过滤）
    entry_mode = str(request.args.get("entry", "close") or "close").strip().lower()
    if entry_mode not in ENTRY_MODES:
        entry_mode = "close"
    try:
        min_gap = int(float(request.args.get("gap", 0) or 0))
        min_gap = min(200, min_gap) if min_gap > 1 else 0
    except (TypeError, ValueError):
        min_gap = 0
    # 信号来源：mode=pattern|trend（默认 pattern，未知取值回退 pattern）
    signal_mode = str(request.args.get("mode", "pattern") or "pattern").strip().lower()
    if signal_mode not in SIGNAL_MODES:
        signal_mode = "pattern"

    if ticker not in ALLOWED_TICKERS:
        return jsonify({"error": f"暂只支持 {sorted(ALLOWED_TICKERS)}"}), 400

    # 拉取：小窗口走常规增量缓存；大窗口走磁盘缓存（回测口径）
    if bars <= 3000:
        candles = _fetch_candles_cached(ticker, interval, bars)
    else:
        client = OKXClient(
            proxy=OKX_PROXY,
            cache_dir=os.path.join(_PROJECT_ROOT, ".cache"),
        )
        try:
            candles = client.get_candles(ticker, bar=interval, limit=bars, use_cache=True)
        except Exception as exc:  # noqa: BLE001
            log.error("回测拉取失败 %s %s %d: %s", ticker, interval, bars, exc)
            return jsonify({"error": f"K 线拉取失败: {exc}"}), 502
        finally:
            client.close()

    # 用完整 K 线做检测+结局判定（scan_window = 全部数据）
    signals = detect_signals(
        ticker, interval, bars,
        trend_period=trend, ma_type=MA_TYPE,
        scan_window=min(bars, len(candles) - 1),
        volume_enabled=volume_on,
        candles=candles,
        rr=rr,
        atr_mult=atr_mult,
        entry_mode=entry_mode,
        min_gap=min_gap,
        signal_mode=signal_mode,
    )

    stats = {"WIN": 0, "LOSS": 0, "OPEN": 0}
    for s in signals:
        stats[s["result"]] = stats.get(s["result"], 0) + 1
    closed = stats["WIN"] + stats["LOSS"]
    win_rate = (stats["WIN"] / closed * 100) if closed else 0.0

    span = ((candles[-1].ts - candles[0].ts) / 86_400_000) if len(candles) > 1 else 0.0

    elapsed = round(time.time() - t0, 2)
    log.info("前端回测 %s %s bars=%d 趋势=%s 量能=%s rr=%s atr=%s 入场=%s 间隔=%s 来源=%s 信号=%d 胜率%.1f%% 耗时%s s",
             ticker, interval, bars, trend, volume_on, rr, atr_mult,
             entry_mode, min_gap, signal_mode, len(signals), win_rate, elapsed)

    return jsonify({
        "ticker": ticker,
        "interval": interval,
        "bars": len(candles),
        "span_days": round(span, 1),
        "total": len(signals),
        "win": stats["WIN"],
        "loss": stats["LOSS"],
        "open": stats["OPEN"],
        "win_rate": round(win_rate, 1),
        "with_trend": bool(trend),
        "with_volume": volume_on,
        "rr": rr,
        "atr_mult": atr_mult,
        "entry_mode": entry_mode,
        "min_gap": min_gap,
        "signal_mode": signal_mode,
        "elapsed_sec": elapsed,
        "signals": signals[-200:],   # 最近 200 笔明细（避免超大响应）
    })


@app.route("/api/order/cancel", methods=["POST"])
def api_order_cancel():
    """撤销未成交的限价委托（撤单）。

    Body JSON: {"ticker": "BTC-USDT-SWAP", "ord_id": "..."}（ord_id 必选，
    也可传 cl_ord_id 二选一）。未配置凭证时返回 mock 提示，不伪装成功。
    """
    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or DEFAULT_TICKER).upper()
    ord_id = str(data.get("ord_id") or "").strip()
    cl_ord_id = str(data.get("cl_ord_id") or "").strip()

    if ticker not in ALLOWED_TICKERS:
        return jsonify({"error": f"暂只支持 {sorted(ALLOWED_TICKERS)}"}), 400
    if not ord_id and not cl_ord_id:
        return jsonify({"error": "缺少 ord_id 或 cl_ord_id"}), 400

    client, cfg_err = get_trade_client()
    if client is None:
        return jsonify({
            "status": "ok",
            "mock": True,
            "mock_reason": cfg_err or "DASH_ORDER_SIMULATED=0（下单模拟模式）",
            "ord_id": ord_id,
            "ticker": ticker,
            "account_mode": account_mode(),
            "message": f"[mock] 已模拟撤单 {ticker} 订单 {ord_id or cl_ord_id}",
        })

    pos_side_ = None
    try:
        result = client.cancel_limit_order(
            inst_id=ticker, ord_id=ord_id or None, cl_ord_id=cl_ord_id or None,
        )
        cancelled_id = result.get("ordId") or ord_id
        log.info("撤单成功 %s 订单%s 模式%s", ticker, cancelled_id, account_mode())
        return jsonify({
            "status": "ok",
            "mock": False,
            "account_mode": account_mode(),
            "ticker": ticker,
            "ord_id": cancelled_id,
            "message": f"[{'模拟盘🟢' if account_mode() == 'simulated' else '实盘🔴'}] "
                       f"撤单成功 {ticker} 订单 {cancelled_id}",
        })
    except OKXTradeError as exc:
        log.error("撤单失败 %s 订单%s: %s", ticker, ord_id or cl_ord_id, exc)
        return jsonify({"error": f"撤单失败: {exc}", "mock": False,
                        "account_mode": account_mode()}), 502
    except Exception as exc:  # noqa: BLE001
        log.error("撤单异常 %s 订单%s: %s", ticker, ord_id or cl_ord_id, exc)
        return jsonify({"error": f"撤单异常: {exc}", "mock": False}), 502


@app.route("/api/auto-trade", methods=["GET"])
def api_auto_trade_get():
    """获取自动交易状态和配置。"""
    with _auto_trade_lock:
        cfg = dict(_auto_trade_config)
        auto_log_list = list(_auto_trade_log)
        positions = {k: {kk: vv for kk, vv in v.items()} for k, v in _auto_trade_positions.items()}
        history = list(_auto_trade_history)
        pending = list(_auto_pending_orders.values())
    return jsonify({"config": cfg, "account_mode": account_mode(), "log": auto_log_list[-60:],
                    "history": history[-50:], "positions": positions, "pending": pending,
                    # 未验证配置提示：UI 据此显示告警横幅（blocking = 启动前需确认）
                    "unvalidated": bool(unvalidated_reasons(cfg)),
                    "blocking": blocking_reasons(cfg),
                    "notice": unvalidated_reasons(cfg),
                    "warning": warning_lines(cfg)})


@app.route("/api/auto-trade", methods=["POST"])
def api_auto_trade_update():
    """开始自动交易：复用表盘当前参数（快照），运行中不可改，只能暂停。

    前端把表盘当前参数整包传过来：
    ticker / interval / limit / scan_window / volume_filter /
    trend_period(0=关) / ma_type / max_open_trades / cooldown_seconds / min_rr_ratio
    / entry_mode(close|breakout) / min_gap / atr_mult / signal_mode(pattern|trend)

    安全闸门：若参数命中"阻断类未验证特征"（breakout 入场 / candidate 或 trend_wide
    预设 / atr_mult>0 / signal_mode=trend），
    必须同时传 ``confirm_unvalidated: true`` 才会启动；否则返回 409 并附告警文本。
    仅关闭趋势过滤属"提示类"，不阻断，只在告警横幅中提示。
    """
    data = request.get_json(silent=True) or {}
    with _auto_trade_lock:
        if _auto_trade_config["enabled"]:
            return jsonify({
                "status": "error",
                "message": "自动交易运行中，参数已锁定；请先暂停再重新开始",
            }), 409
        # 快照表盘参数（只接受白名单字段，缺省用当前默认值）
        for key in ("ticker", "interval", "ma_type"):
            if data.get(key):
                _auto_trade_config[key] = str(data[key]).upper() if key == "ticker" else str(data[key])
        for key in ("limit", "scan_window", "max_open_trades",
                    "cooldown_seconds", "trend_period"):
            if key in data:
                try:
                    _auto_trade_config[key] = max(0, int(data[key]))
                except (TypeError, ValueError):
                    pass
        if "min_rr_ratio" in data:
            try:
                _auto_trade_config["min_rr_ratio"] = min(5.0, max(1.0, float(data["min_rr_ratio"])))
            except (TypeError, ValueError):
                pass
        if "max_leverage" in data:
            try:
                _auto_trade_config["max_leverage"] = min(10.0, max(0.1, float(data["max_leverage"])))
            except (TypeError, ValueError):
                pass
        if "volume_filter" in data:
            _auto_trade_config["volume_filter"] = bool(data["volume_filter"])
        if "rr" in data:
            try:
                _auto_trade_config["rr"] = min(20.0, max(0.5, float(data["rr"])))
            except (TypeError, ValueError):
                pass
        if "atr_mult" in data:
            try:
                v = float(data["atr_mult"])
                # 0/负数 → 结构止损；上限 10×ATR 防止止损宽到不合理
                _auto_trade_config["atr_mult"] = v if 0 < v <= 10 else 0
            except (TypeError, ValueError):
                pass
        # 入场方式：未知取值回退 close（不改行为）
        if "entry_mode" in data:
            em = str(data["entry_mode"] or "close").strip().lower()
            _auto_trade_config["entry_mode"] = em if em in ENTRY_MODES else "close"
        # 同形态最小间隔：0/1 = 不过滤；上限 200 根防误设
        if "min_gap" in data:
            try:
                g = int(float(data["min_gap"]))
                _auto_trade_config["min_gap"] = min(200, g) if g > 1 else 0
            except (TypeError, ValueError):
                pass
        # 信号来源：未知取值回退 pattern（不改行为）
        if "signal_mode" in data:
            sm = str(data["signal_mode"] or "pattern").strip().lower()
            _auto_trade_config["signal_mode"] = sm if sm in SIGNAL_MODES else "pattern"
        # 趋势：trend_period>0 即启用
        _auto_trade_config["trend_filter"] = _auto_trade_config["trend_period"] > 0

        # ---- 安全闸门：未验证配置必须显式确认 ----
        # 阻断类（breakout 入场 / candidate 预设）需确认；仅关闭趋势过滤只提示不阻断
        cfg = dict(_auto_trade_config)
        reasons = blocking_reasons(cfg)
        if reasons and not bool(data.get("confirm_unvalidated")):
            return jsonify({
                "status": "needs_confirmation",
                "message": "该参数组合未经独立验证，需显式确认后才能启动自动交易",
                "reasons": reasons,
                "notice": unvalidated_reasons(cfg),
                "warning": warning_lines(cfg),
                "config": cfg,
            }), 409

        _auto_trade_config["enabled"] = True
        _auto_trade_unvalidated_ack["acknowledged"] = bool(reasons)

    _start_auto_trade_thread()
    _save_auto_trade_state()
    # 未验证配置：启动时把告警写进日志面板（醒目、可追溯）
    for line in warning_lines(cfg):
        _auto_log(line, success=False)
    _auto_log(f"🚀 自动交易开始：{cfg['ticker']} {cfg['interval']} "
              f"量能:{'开' if cfg['volume_filter'] else '关'} "
              f"趋势:{cfg['trend_period'] or '关'} "
              f"入场:{cfg.get('entry_mode', 'close')} "
              f"来源:{cfg.get('signal_mode', 'pattern')} "
              f"止损:{('ATR×%g' % cfg['atr_mult']) if cfg.get('atr_mult') else '结构'} "
              f"间隔:{cfg.get('min_gap', 0) or '无'}")
    return jsonify({"status": "ok", "config": cfg,
                    "unvalidated": bool(unvalidated_reasons(cfg)),
                    "blocking": blocking_reasons(cfg),
                    "warning": warning_lines(cfg)})


@app.route("/api/auto-trade/stop", methods=["POST"])
def api_auto_trade_stop():
    """停止自动交易。"""
    with _auto_trade_lock:
        _auto_trade_config["enabled"] = False
        # 停止后清空确认标记：下次启动若仍是未验证配置，需重新确认
        _auto_trade_unvalidated_ack["acknowledged"] = False
    _save_auto_trade_state()
    return jsonify({"status": "ok", "message": "自动交易已停止"})


if __name__ == "__main__":
    port = int(os.getenv("DASH_PORT", "8000"))
    # 默认只绑定本机回环，更安全；需局域网访问时设 DASH_HOST=0.0.0.0
    host = os.getenv("DASH_HOST", "127.0.0.1")
    # DASH_DEBUG=1 才开调试(reloader)；默认关，避免 reloader 在后台运行时误杀子进程导致服务退出
    debug = os.getenv("DASH_DEBUG", "0").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug, use_reloader=debug)
