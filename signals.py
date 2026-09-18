"""把识别出的价格行为形态序列化为 TradingView Webhook 兼容的 JSON。"""
from __future__ import annotations

import json
from typing import Any

from patterns import Pattern
from volume import volume_signal


def pattern_to_signal(p: Pattern, ticker: str) -> dict[str, Any]:
    """将 Pattern 转为 JSON 可序列化的信号字典。

    字段与 TradingView Webhook 常见约定保持一致：
      action / ticker / price / sl / tp / strategy

    Args:
        p: 识别出的形态
        ticker: 交易对（将直接作为信号中的 ticker 字段）

    Returns:
        可直接 json.dumps 的字典。
    """
    sig: dict[str, Any] = {
        "action": p.action,          # BUY / SELL
        "ticker": ticker,
        "price": _round(p.entry),    # 入场价（形态收盘价）
        "sl": _round(p.stop),        # 止损价
        "tp": _round(p.take_profit), # 目标价
        "strategy": p.name,          # 形态名称
        # 附加字段（方便排查，不参与 TradingView 必需字段）
        "risk": _round(p.risk),
        "rr": p.risk_reward,
    }

    # 量能确认字段：仅在启用量能（volume_ratio 非 None）时输出，
    # 保证默认行为与之前完全一致（量能可选）。
    if p.volume_ratio is not None:
        sig["volume_ratio"] = _round(p.volume_ratio)   # 量能比率数值
        sig["volume_signal"] = volume_signal(p.volume_ratio)  # strong / weak / neutral
        if p.volume_confirm is not None:
            sig["volume_confirm"] = p.volume_confirm   # 放量Pinbar / 缩量吞没 等

    return sig


def print_signal_json(sig: dict[str, Any]) -> None:
    """把信号以紧凑 JSON 打印到控制台。"""
    print(json.dumps(sig, ensure_ascii=False))


def print_signal_pretty(sig: dict[str, Any]) -> None:
    """以更易读的格式打印信号（调试用）。"""
    print(json.dumps(sig, ensure_ascii=False, indent=2))


def _round(v: float) -> float:
    """统一保留 6 位有效小数，避免浮点噪声，同时保持精度。"""
    return round(float(v), 6)
