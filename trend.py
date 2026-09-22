"""趋势过滤模块。

基于均线（SMA/EMA）判断价格处于上升还是下降趋势，用于过滤逆势的
价格行为信号：上升趋势只保留 BUY，下降趋势只保留 SELL。

关键点：均线只使用「信号所在 K 线（含）之前」的数据计算，绝不引入
未来数据，保证回测无前视偏差。
"""
from __future__ import annotations

from typing import Optional

from okx_client import Candle
from patterns import Pattern

# 趋势方向常量
UP = "up"
DOWN = "down"


def sma(values: list[float], period: int) -> Optional[float]:
    """计算最近 period 个值的简单移动平均。数据不足返回 None。"""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema_at(values: list[float], period: int) -> Optional[float]:
    """计算到序列末尾的指数移动平均。

    从第一个值开始累积 EMA（以第一个值作种子），取到末尾的值。
    """
    if not values or period <= 0:
        return None
    alpha = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _moving_average(values: list[float], period: int, kind: str) -> Optional[float]:
    kind = (kind or "sma").lower()
    if kind == "ema":
        return ema_at(values, period)
    return sma(values, period)


def trend_at(
    candles: list[Candle],
    idx: int,
    period: int,
    kind: str = "sma",
) -> Optional[str]:
    """判断 candles[idx] 所处趋势方向。

    Args:
        candles: 升序 K 线列表
        idx: 信号所在 K 线下标
        period: 均线周期
        kind: "sma" 或 "ema"

    Returns:
        "up"  收盘价 > 均线（上升趋势）
        "down" 收盘价 < 均线（下降趋势）
        None  无法计算均线（数据不足），视为趋势不明
    """
    if idx < 0 or idx >= len(candles):
        return None
    # 只用 <= idx 的收盘价，避免前视偏差
    closes = [c.close for c in candles[: idx + 1]]
    ma = _moving_average(closes, period, kind)
    if ma is None:
        return None
    close = candles[idx].close
    if close > ma:
        return UP
    if close < ma:
        return DOWN
    return None  # 恰好等于均线，视为中性


def trend_allows(pattern: Pattern, trend: Optional[str]) -> bool:
    """趋势过滤：判断某信号在当前趋势下是否允许交易。

    - 趋势为 up（上升）: 只允许 BUY
    - 趋势为 down（下降）: 只允许 SELL
    - 趋势为 None（无法判断/中性）: 返回 True，即保留信号（不因数据不足误杀）

    Returns:
        True 表示保留该信号，False 表示逆势应过滤掉。
    """
    if trend is None:
        return True
    if trend == UP:
        return True if pattern.action == "BUY" else False
    if trend == DOWN:
        return True if pattern.action == "SELL" else False
    return True


#: 斜率判定趋势时的默认阈值系数：|MA 斜率| 需超过 ``mult × ATR / price``。
#: 该口径来自第八轮"纯趋势对照实验"（见 `PRICE_ACTION_SYNTHESIS.md` 步骤 5），
#: 用于把"横盘微动"判为无趋势（flat），避免在震荡里反复双向开仓。
DEFAULT_SLOPE_THRESHOLD = 0.2


def ma_slope_at(
    candles: list[Candle],
    idx: int,
    period: int,
    kind: str = "sma",
) -> Optional[float]:
    """计算 ``candles[idx]`` 处的均线**斜率**（相对变化率）。

    取"最近 period 根"与"再前 period 根"的均线之差，再除以价格归一化：

        slope = (MA(收盘价, idx−period+1 … idx) − MA(收盘价, idx−2·period+1 … idx−period))
                / 后者

    **只用 idx 及之前的数据**，无前视偏差。

    Returns:
        相对斜率；数据不足（``idx + 1 < 2 × period``）或参数非法时返回 None。
    """
    try:
        p = int(period)
    except (TypeError, ValueError):
        return None
    if p <= 0 or idx < 0 or idx >= len(candles) or idx + 1 < 2 * p:
        return None
    recent = [c.close for c in candles[idx + 1 - p: idx + 1]]
    older = [c.close for c in candles[idx + 1 - 2 * p: idx + 1 - p]]
    ma_now = _moving_average(recent, p, kind)
    ma_prev = _moving_average(older, p, kind)
    if ma_now is None or ma_prev is None or not ma_prev:
        return None
    return (ma_now - ma_prev) / ma_prev


def trend_slope_at(
    candles: list[Candle],
    idx: int,
    period: int,
    kind: str = "sma",
    atr: Optional[float] = None,
    threshold: float = DEFAULT_SLOPE_THRESHOLD,
) -> Optional[str]:
    """基于**均线斜率**判断趋势方向（第八轮"纯趋势"信号的口径）。

    与 :func:`trend_at`（比较收盘价与均线）不同，本函数比较**两段均线**，
    因此能区分"横盘"与"趋势"：

      * 斜率 > ``threshold × ATR / price`` → ``UP``
      * 斜率 < ``−threshold × ATR / price`` → ``DOWN``
      * 否则 → ``None``（横盘/无趋势）

    Args:
        candles: 升序 K 线列表。
        idx: 信号所在 K 线下标。
        period: 均线周期（如 20）。
        kind: "sma" 或 "ema"。
        atr: 当前 ATR（可选）。提供时阈值随波动率缩放；否则只与固定阈值比较。
        threshold: 阈值系数，默认 0.2。

    Returns:
        ``UP`` / ``DOWN`` / ``None``（横盘或数据不足）。
    """
    slope = ma_slope_at(candles, idx, period, kind)
    if slope is None:
        return None
    # 阈值按波动率缩放：ATR 相对价格的占比 × 系数
    try:
        th = float(threshold)
    except (TypeError, ValueError):
        th = DEFAULT_SLOPE_THRESHOLD
    if atr and candles[idx].close:
        th = th * float(atr) / candles[idx].close
    if slope > th:
        return UP
    if slope < -th:
        return DOWN
    return None
