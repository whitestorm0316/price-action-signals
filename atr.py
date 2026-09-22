"""ATR（平均真实波幅）计算模块。

用途
----
本项目的第八轮研究（见 `PRICE_ACTION_SYNTHESIS.md`）得到一个**唯一被数据支持**的
结论：在价格行为学框架里，形态本身不含方向信息（匹配随机入场 α ≈ 0），
真正能改变净期望的是**止损宽度**——

  * 止损越窄，``f``（往返费率 ÷ 止损占价比例）越大，手续费吃掉的比例越高；
  * 在 4H 上把止损从"形态结构极值"（f 中位 0.060R）放宽到 ``1.5 × ATR``
    （f 中位 0.036R），BTC 净期望由 −0.0718R 提升到 +0.0375R，
    但该改善**来自成本而非信息**（形态源与随机源曲线形状一致）。

因此本模块提供 ATR 计算，供 ``main.py`` / 仪表盘把止损定为
``entry ∓ atr_mult × ATR``，从而把"止损宽度"变成一个**可调、可复现**的参数。

无前视偏差
----------
``atr_at(candles, idx, period)`` 只使用 ``candles[: idx + 1]``，即**含信号所在 K 线**
（其 OHLC 在收盘后已知），绝不使用 idx 之后的数据。这与 ``trend.py`` 的口径一致。

ACF
---
本模块为纯计算，不依赖网络，可独立测试（见 ``test_strategy_config.py``）。
"""
from __future__ import annotations

from typing import Optional

from okx_client import Candle

#: 默认 ATR 周期（与常见技术分析口径一致）
DEFAULT_PERIOD = 14


def true_range(prev_close: Optional[float], candle: Candle) -> float:
    """单根 K 线的真实波幅（True Range）。

    ``TR = max(high − low, |high − prev_close|, |low − prev_close|)``

    ``prev_close`` 为 None（第一根）时退化为 ``high − low``。
    """
    hl = candle.high - candle.low
    if prev_close is None:
        return hl
    return max(hl, abs(candle.high - prev_close), abs(candle.low - prev_close))


def atr_at(
    candles: list[Candle],
    idx: int,
    period: int = DEFAULT_PERIOD,
) -> Optional[float]:
    """计算 ``candles[idx]`` 处的 ATR（**只用 idx 及之前的数据，无前视**）。

    采用简单移动平均（SMA）而非 Wilder 平滑，理由是：
      * 与 ``trend.py`` 的 SMA 口径一致，便于解释；
      * 在本项目的 ATR 止损扫描（``probe_pa_stopwidth.py``）中用的就是 SMA 口径，
        保持回测与实盘一致，避免"研究用 SMA、实盘用 Wilder"的隐性偏差。

    Args:
        candles: 升序 K 线列表。
        idx: 目标下标（信号所在 K 线）。
        period: 平均周期，默认 14。

    Returns:
        ATR 值；数据不足（``idx + 1 < period``）或参数非法时返回 None。
    """
    try:
        p = int(period)
    except (TypeError, ValueError):
        return None
    if p <= 0 or idx < 0 or idx >= len(candles):
        return None
    if idx + 1 < p:
        return None

    start = idx + 1 - p
    trs: list[float] = []
    for i in range(start, idx + 1):
        prev_close = candles[i - 1].close if i > 0 else None
        trs.append(true_range(prev_close, candles[i]))
    if len(trs) < p:
        return None
    return sum(trs) / p


def atr_series(
    candles: list[Candle],
    period: int = DEFAULT_PERIOD,
) -> list[Optional[float]]:
    """批量计算 ATR 序列（第 i 项 = ``atr_at(candles, i, period)``）。

    数据不足的前 ``period − 1`` 项为 None。便于回测一次性预算，
    避免在循环里重复计算。
    """
    return [atr_at(candles, i, period) for i in range(len(candles))]
