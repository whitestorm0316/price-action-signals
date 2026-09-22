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
    # 入场方式（"close" = 形态收盘价，默认行为；"breakout" = 形态极值，等突破才进场）
    entry_mode: str = "close"


# ---------------------------------------------------------------------------
# Pinbar
# ---------------------------------------------------------------------------
def detect_pinbar(
    current: Candle,
    wick_ratio: float = 2.0,
    close_zone: float = 1 / 3,
    volume_ratio: Optional[float] = None,
    rr: float = 2.0,
    atr: Optional[float] = None,
    atr_mult: Optional[float] = None,
    entry_mode: str = "close",
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
        rr: 盈亏比 (Reward/Risk) = 目标价距入场 ÷ 止损距入场，默认 2:1。
            目标价按 entry ± rr*风险 推算。
        atr: 当前 ATR 值（可选，无前视）。仅在 atr_mult 给定时用于定价止损。
        atr_mult: 止损改为 ATR 倍数（可选）。给定且 >0、且 atr 有效时，
            止损 = entry ∓ atr_mult × atr，替代"影线极值外侧"的结构止损；
            为 None 时保持原有结构止损（默认行为不变）。
        entry_mode: 入场方式。"close"（默认）= 形态收盘价入场，行为不变；
            "breakout" = 入场价取形态极值（看涨取最高价、看跌取最低价），
            即等价格真正越过形态极值才进场（复盘显示可过滤假信号）。

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
        entry_px = _entry_price(entry_mode, current, "BUY")
        return _build_pattern(
            name="bullish_pinbar",
            action="BUY",
            entry=entry_px,
            stop=_stop_price("BUY", entry_px,
                             current.low - _tick_buffer(current), atr, atr_mult),
            rr=rr,
            volume_confirm=volume_confirm(volume_ratio, "pinbar"),
            volume_ratio=volume_ratio,
            entry_mode=entry_mode,
        )

    # 看跌 Pinbar: 上影线 >= wick_ratio*实体，收盘落在整体区间的底部 1/3
    if upper_wick >= wick_ratio * body and current.close <= current.low + close_zone * rng:
        entry_px = _entry_price(entry_mode, current, "SELL")
        return _build_pattern(
            name="bearish_pinbar",
            action="SELL",
            entry=entry_px,
            stop=_stop_price("SELL", entry_px,
                             current.high + _tick_buffer(current), atr, atr_mult),
            rr=rr,
            volume_confirm=volume_confirm(volume_ratio, "pinbar"),
            volume_ratio=volume_ratio,
            entry_mode=entry_mode,
        )

    return None


# ---------------------------------------------------------------------------
# 吞没 (Engulfing)
# ---------------------------------------------------------------------------
def detect_engulfing(
    previous: Candle,
    current: Candle,
    volume_ratio: Optional[float] = None,
    rr: float = 2.0,
    atr: Optional[float] = None,
    atr_mult: Optional[float] = None,
    entry_mode: str = "close",
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
        atr: 当前 ATR 值（可选，无前视）。仅在 atr_mult 给定时用于定价止损。
        atr_mult: 止损改为 ATR 倍数（可选）。给定且 >0、且 atr 有效时，
            止损 = entry ∓ atr_mult × atr，替代"两K线极值外侧"的结构止损；
            为 None 时保持原有结构止损（默认行为不变）。
        entry_mode: 入场方式。"close"（默认）= 形态收盘价入场，行为不变；
            "breakout" = 入场价取形态极值（看涨取最高价、看跌取最低价）。

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
        entry_px = _entry_price(entry_mode, current, "BUY")
        return _build_pattern(
            name="bullish_engulfing",
            action="BUY",
            entry=entry_px,
            stop=_stop_price("BUY", entry_px,
                             min(current.low, previous.low) - _tick_buffer(current),
                             atr, atr_mult),
            rr=rr,
            volume_confirm=volume_confirm(volume_ratio, "engulfing"),
            volume_ratio=volume_ratio,
            entry_mode=entry_mode,
        )

    # 看跌吞没
    if (
        previous.is_bullish
        and current.is_bearish
        and current.body_top >= previous.body_top
        and current.body_bottom <= previous.body_bottom
    ):
        entry_px = _entry_price(entry_mode, current, "SELL")
        return _build_pattern(
            name="bearish_engulfing",
            action="SELL",
            entry=entry_px,
            stop=_stop_price("SELL", entry_px,
                             max(current.high, previous.high) + _tick_buffer(current),
                             atr, atr_mult),
            rr=rr,
            volume_confirm=volume_confirm(volume_ratio, "engulfing"),
            volume_ratio=volume_ratio,
            entry_mode=entry_mode,
        )

    return None


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _entry_price(entry_mode: str, candle: Candle, action: str) -> float:
    """决定入场价。

    - ``"close"``（默认）：形态收盘价，即原有行为，逐字节不变；
    - ``"breakout"``：形态极值（看涨取最高价、看跌取最低价），
      即等价格真正越过形态极值才进场。复盘显示这可过滤"形态出现但没跟风"
      的假信号；但在实盘中它意味着需要**触发单**而非限价单。

    无法识别的取值一律回退 ``"close"``，保证既有行为不受影响。
    """
    if str(entry_mode or "").strip().lower() == "breakout":
        return candle.high if action == "BUY" else candle.low
    return candle.close


def _stop_price(
    action: str,
    entry: float,
    struct_stop: float,
    atr: Optional[float],
    atr_mult: Optional[float],
) -> float:
    """决定止损价：atr_mult 启用时用 ATR 倍数，否则沿用结构止损。

    ATR 模式：stop = entry ∓ atr_mult × atr（BUY 取减、SELL 取加）。
    为 None / 非正 / atr 无效时一律回退结构止损，保证默认行为完全不变。
    两种口径都取「离入场更远」的那一侧，避免 ATR 止损比结构止损更贴近入场
    而把风险放大到形态之外。
    """
    if atr_mult is None or atr is None:
        return struct_stop
    try:
        mult = float(atr_mult)
        atr_v = float(atr)
    except (TypeError, ValueError):
        return struct_stop
    if mult <= 0 or atr_v <= 0:
        return struct_stop
    if action == "BUY":
        return min(struct_stop, entry - mult * atr_v)
    return max(struct_stop, entry + mult * atr_v)


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
    entry_mode: str = "close",
) -> Pattern:
    """按固定盈亏比计算目标价与风险值。

    Args:
        rr: 固定盈亏比 (Reward / Risk)，默认 2:1
        volume_confirm: 量能确认标签（可选），如 "放量Pinbar"
        volume_ratio: 量能比率（可选）
        entry_mode: 入场方式（"close" / "breakout"），随 Pattern 一并返回，
            供上层决定实盘下单类型（限价 vs 触发）。
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
        entry_mode=entry_mode,
    )


def build_trend_signal(
    candle: Candle,
    action: str,
    atr: Optional[float],
    atr_mult: float = 1.5,
    rr: float = 2.0,
) -> Optional[Pattern]:
    """按**纯趋势**规则构造信号（不使用任何形态）。

    这是第八轮"纯趋势对照实验"（`PRICE_ACTION_SYNTHESIS.md` 步骤 5）的落地：
    方向由趋势决定（由调用方判定 ``action``），止损 = ``entry ∓ atr_mult × ATR``。

    为什么需要它
    ------------
    第八轮证明 **Pinbar/吞没不含方向信息**（匹配随机入场 α ≈ 0、p ≈ 0.83），
    所以 ``trend_wide`` 预设若仍靠形态触发信号，就与自身描述矛盾。
    本函数提供一条**完全绕过形态**的路径：只要趋势判定给出方向，就按
    ATR 止损直接开仓，让"纯趋势 + 宽止损"这一组合能够被真实地表达与回测。

    注意：这**不代表推荐交易**。该组合的 BTC 正 t（+1.93）来自市场漂移
    （与同向随机入场比 α = −0.019，z = −0.22），ETH 上为负。

    Args:
        candle: 信号所在 K 线（入场价取收盘价，与 ``entry_mode=close`` 一致）。
        action: "BUY" 或 "SELL"。
        atr: 当前 ATR（**必填**，否则无法按 ATR 定价，返回 None）。
        atr_mult: 止损的 ATR 倍数，默认 1.5。
        rr: 盈亏比，默认 2.0。

    Returns:
        Pattern；``atr`` 无效或 ``atr_mult <= 0`` 时返回 None（不猜止损）。
    """
    if action not in ("BUY", "SELL"):
        return None
    try:
        mult = float(atr_mult)
        atr_v = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        return None
    if mult <= 0 or atr_v <= 0:
        return None
    entry = candle.close
    stop = entry - mult * atr_v if action == "BUY" else entry + mult * atr_v
    return _build_pattern(
        name="trend_follow",
        action=action,
        entry=entry,
        stop=stop,
        rr=rr,
        entry_mode="close",
    )
