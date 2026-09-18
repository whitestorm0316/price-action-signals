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
        return pattern.action == "BUY"
    if trend == DOWN:
        return pattern.action == "SELL"
    return True
