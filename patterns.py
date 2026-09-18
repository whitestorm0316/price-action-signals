"""价格行为形态检测（Pinbar / 吞没）。

所有函数均以「当前 K 线 + 前一根 K 线」为输入，返回信号字典或 None。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from okx_client import Candle
from volume import volume_confirm


@dataclass(frozen=True)
class Pattern:
    """一个已识别出的价格行为形态。"""

    name: str          # 形态名称，如 "bullish_pinbar"
    action: str        # BUY / SELL
    entry: float       # 入场价（形态收盘价）
    stop: float        # 止损价
    take_profit: float # 目标价
    risk_reward: float # 盈亏比
    risk: float        # 单笔风险 (entry - stop 的绝对值)
    # 量能确认（可选，未启用量能时为 None）
    volume_confirm: Optional[str] = None   # 如 "放量Pinbar" / "缩量吞没"，中性或未启用为 None
    volume_ratio: Optional[float] = None   # 量能比率 = 当前量 / 过去20根均量，未启用为 None


# ---------------------------------------------------------------------------
# Pinbar
# ---------------------------------------------------------------------------
def detect_pinbar(
    current: Candle,
    wick_ratio: float = 2.0,
    close_zone: float = 1 / 3,
    volume_ratio: Optional[float] = None,
) -> Optional[Pattern]:
    """检测看涨/看跌 Pinbar。

    规则:
      - 影线长度 >= 实体长度的 wick_ratio 倍
      - 看涨 Pinbar: 下影线长，收盘价位于 K 线高点的 upper 1/3 区域内
      - 看跌 Pinbar: 上影线长，收盘价位于 K 线低点的 lower 1/3 区域内

    Args:
        current: 当前 K 线
        wick_ratio: 影线/实体最小倍数，默认 2
        close_zone: 收盘价须处于的方向区间比例，默认 1/3
        volume_ratio: 当前 K 线的量能比率（可选）。提供时给形态附加
            量能确认标签（放量Pinbar / 缩量Pinbar），作为信号加权/降级依据。

    Returns:
        Pattern 或 None
    """
    body = current.body
    if body <= 0:
        return None  # 十字星无实体，不构成 Pinbar

    upper_wick = current.upper_wick
    lower_wick = current.lower_wick
    rng = current.high - current.low
    if rng <= 0:
        return None

    # 看涨 Pinbar: 下影线 >= wick_ratio*实体，收盘落在整体区间的顶部 1/3
    if lower_wick >= wick_ratio * body and current.close >= current.high - close_zone * rng:
        return _build_pattern(
            name="bullish_pinbar",
            action="BUY",
            entry=current.close,
            stop=current.low - _tick_buffer(current),  # 影线(下)极值外侧
            volume_confirm=volume_confirm(volume_ratio, "pinbar"),
            volume_ratio=volume_ratio,
        )

    # 看跌 Pinbar: 上影线 >= wick_ratio*实体，收盘落在整体区间的底部 1/3
    if upper_wick >= wick_ratio * body and current.close <= current.low + close_zone * rng:
        return _build_pattern(
            name="bearish_pinbar",
            action="SELL",
            entry=current.close,
            stop=current.high + _tick_buffer(current),  # 影线(上)极值外侧
            volume_confirm=volume_confirm(volume_ratio, "pinbar"),
            volume_ratio=volume_ratio,
        )

    return None


# ---------------------------------------------------------------------------
# 吞没 (Engulfing)
# ---------------------------------------------------------------------------
def detect_engulfing(
    previous: Candle,
    current: Candle,
    volume_ratio: Optional[float] = None,
) -> Optional[Pattern]:
    """检测看涨/看跌吞没。

    规则:
      - 当前 K 线实体完全覆盖前一根 K 线实体
      - 方向与前一根相反

    看涨吞没: 前一根阴线，当前阳线实体覆盖其整个实体
    看跌吞没: 前一根阳线，当前阴线实体覆盖其整个实体

    Args:
        previous: 前一根 K 线
        current:  当前 K 线
        volume_ratio: 当前 K 线的量能比率（可选）。提供时给形态附加
            量能确认标签（放量吞没 / 缩量吞没），作为信号加权/降级依据。

    Returns:
        Pattern 或 None
    """
    if previous.body <= 0 or current.body <= 0:
        return None

    # 看涨吞没
    if (
        previous.is_bearish
        and current.is_bullish
        and current.body_bottom <= previous.body_bottom
        and current.body_top >= previous.body_top
    ):
        return _build_pattern(
            name="bullish_engulfing",
            action="BUY",
            entry=current.close,
            stop=min(current.low, previous.low) - _tick_buffer(current),  # 形态(两K线)下极值外侧
            volume_confirm=volume_confirm(volume_ratio, "engulfing"),
            volume_ratio=volume_ratio,
        )

    # 看跌吞没
    if (
        previous.is_bullish
        and current.is_bearish
        and current.body_top >= previous.body_top
        and current.body_bottom <= previous.body_bottom
    ):
        return _build_pattern(
            name="bearish_engulfing",
            action="SELL",
            entry=current.close,
            stop=max(current.high, previous.high) + _tick_buffer(current),  # 形态(两K线)上极值外侧
            volume_confirm=volume_confirm(volume_ratio, "engulfing"),
            volume_ratio=volume_ratio,
        )

    return None


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _tick_buffer(candle: Candle) -> float:
    """根据价格量级估算一个最小缓冲，确保止损严格在极值“外侧”。

    对 BTC 这类价格，取价差的 1%（至少一个有意义的小数位），
    避免 stop 恰好等于极值被后续价格精确触及。
    """
    price = (candle.high + candle.low) / 2
    return max(price * 0.001, 0.01)


def _build_pattern(
    name: str,
    action: str,
    entry: float,
    stop: float,
    rr: float = 2.0,
    volume_confirm: Optional[str] = None,
    volume_ratio: Optional[float] = None,
) -> Pattern:
    """按固定盈亏比计算目标价与风险值。

    Args:
        rr: 固定盈亏比 (Reward / Risk)，默认 2:1
        volume_confirm: 量能确认标签（可选），如 "放量Pinbar"
        volume_ratio: 量能比率（可选）
    """
    risk = abs(entry - stop)
    take_profit = entry + rr * risk if action == "BUY" else entry - rr * risk
    return Pattern(
        name=name,
        action=action,
        entry=entry,
        stop=stop,
        take_profit=take_profit,
        risk_reward=rr,
        risk=risk,
        volume_confirm=volume_confirm,
        volume_ratio=volume_ratio,
    )
