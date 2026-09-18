"""量能确认模块。

量能**不作为独立的买入/卖出信号**，只作为现有价格行为信号（Pinbar/吞没）
的确认或降级条件，且整体可选用 `--volume` 开关控制：

  - 放量（量能比率 >= STRONG_THRESHOLD）→ 标记"放量"，信号加权（确认）
  - 缩量（量能比率 <  WEAK_THRESHOLD） → 标记"缩量"，信号减权（降级）
  - 介于两者之间 / 无法计算 → 中性，不加权不减权

过滤规则：缩量(weak) 且 盈亏比 < 2 → 过滤掉该信号（弱量 + 低盈亏比 = 不值得冒险）。

所有计算只使用「信号所在 K 线（含）之前」的数据，避免前视偏差。
"""
from __future__ import annotations

from typing import Optional

from okx_client import Candle

# 量能阈值
STRONG_THRESHOLD = 1.2   # 量能比率 >= 1.2 视为放量（信号加权）
WEAK_THRESHOLD = 0.8     # 量能比率 <  0.8 视为缩量（信号减权）
DEFAULT_LOOKBACK = 20    # 平均成交量参考周期（过去 N 根均量）
RR_FILTER = 2.0          # 过滤规则中"低盈亏比"的阈值

# 量能信号取值
STRONG = "strong"
WEAK = "weak"
NEUTRAL = "neutral"

# 形态英文名 → 中文名，用于生成"放量/缩量"标签
_BASE_CN = {
    "pinbar": "Pinbar",
    "engulfing": "吞没",
}


def volume_ratio_at(
    candles: list[Candle],
    idx: int,
    lookback: int = DEFAULT_LOOKBACK,
) -> Optional[float]:
    """量能比率 = 当前成交量 / 过去 lookback 根 K 线平均成交量（不含当前）。

    当前根之前的 K 线不足 lookback 根、或平均成交量为 0 时返回 None
    （无法可靠计算，视为中性，不因量能误杀信号）。
    """
    if idx < lookback or idx >= len(candles):
        return None
    past = candles[idx - lookback:idx]
    if not past:
        return None
    avg = sum(c.volume for c in past) / len(past)
    if avg <= 0:
        return None
    return candles[idx].volume / avg


def volume_signal(ratio: Optional[float]) -> str:
    """把量能比率归类为 strong / weak / neutral。"""
    if ratio is None:
        return NEUTRAL
    if ratio >= STRONG_THRESHOLD:
        return STRONG
    if ratio < WEAK_THRESHOLD:
        return WEAK
    return NEUTRAL


def volume_confirm(ratio: Optional[float], base_name: str) -> Optional[str]:
    """生成量能确认标签：放量Pinbar / 缩量吞没 等。

    base_name: "pinbar" 或 "engulfing"。
    量能比率无法计算（None）或中性时返回 None。
    """
    cn = _BASE_CN.get(base_name)
    if cn is None or ratio is None:
        return None
    if ratio >= STRONG_THRESHOLD:
        return f"放量{cn}"
    if ratio < WEAK_THRESHOLD:
        return f"缩量{cn}"
    return None


def should_filter(ratio: Optional[float], risk_reward: float) -> bool:
    """量能过滤：缩量(weak) 且 盈亏比 < 2 → True（应过滤）。

    ratio 为 None（无法计算量能）时返回 False，交由其他条件决定，不因量能误杀。
    """
    if ratio is None:
        return False
    return volume_signal(ratio) == WEAK and risk_reward < RR_FILTER
