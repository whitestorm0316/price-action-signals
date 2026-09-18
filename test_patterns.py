"""价格行为形态检测的单元测试（纯算法，无需网络）。"""
from __future__ import annotations

import unittest

from okx_client import Candle
from patterns import detect_engulfing, detect_pinbar


def c(o, high, low, close, ts=0):
    # 参数名避开内置名 open，内部再映射到 Candle.open
    return Candle(ts=ts, open=o, high=high, low=low, close=close, volume=1.0)


class TestPinbar(unittest.TestCase):
    def test_bullish_pinbar(self):
        # 下影线很长，收盘接近高点 => 看涨 Pinbar
        candle = c(o=100.0, high=101.0, low=90.0, close=100.5)
        p = detect_pinbar(candle)
        self.assertIsNotNone(p)
        self.assertEqual(p.action, "BUY")
        self.assertEqual(p.name, "bullish_pinbar")
        # 入场 = 收盘价
        self.assertEqual(p.entry, 100.5)
        # 止损在影线下极值外侧
        self.assertLess(p.stop, 90.0)
        # 目标 = 入场 + 2 * risk
        self.assertAlmostEqual(p.take_profit, p.entry + 2 * (p.entry - p.stop), places=3)

    def test_bearish_pinbar(self):
        # 上影线很长，收盘接近低点 => 看跌 Pinbar
        # open=100 close=100.5 实体=0.5, 上影=112-100.5=11.5 >= 2*0.5
        candle = c(o=100.0, high=112.0, low=99.0, close=100.5)
        p = detect_pinbar(candle)
        self.assertIsNotNone(p)
        self.assertEqual(p.action, "SELL")
        self.assertGreater(p.stop, 112.0)

    def test_not_pinbar_when_wick_too_short(self):
        # 普通 K 线：下影线 0.5 < 2*实体0.5=1，且收盘不在顶部1/3
        candle = c(o=100.0, high=102.0, low=99.5, close=100.5)
        self.assertIsNone(detect_pinbar(candle))

    def test_doji_not_pinbar(self):
        # 十字星无实体，不构成 Pinbar
        candle = c(o=100.0, high=101.0, low=99.0, close=100.0)
        self.assertIsNone(detect_pinbar(candle))


class TestEngulfing(unittest.TestCase):
    def test_bullish_engulfing(self):
        # 前阴后阳，阳线实体完全覆盖阴线实体
        prev = c(o=100.0, high=100.5, low=98.0, close=98.5)
        curr = c(o=98.0, high=102.0, low=97.5, close=101.5)
        p = detect_engulfing(prev, curr)
        self.assertIsNotNone(p)
        self.assertEqual(p.action, "BUY")
        self.assertEqual(p.name, "bullish_engulfing")
        self.assertEqual(p.entry, 101.5)
        # 止损 = 两K线下极值外侧
        self.assertLess(p.stop, min(curr.low, prev.low))

    def test_bearish_engulfing(self):
        # 前阳后阴，阴线实体完全覆盖阳线实体
        prev = c(o=98.0, high=102.0, low=97.5, close=101.5)
        curr = c(o=101.5, high=103.0, low=97.0, close=97.8)  # 实体[97.8,101.5] 完全覆盖 prev 实体[98,101.5]
        p = detect_engulfing(prev, curr)
        self.assertIsNotNone(p)
        self.assertEqual(p.action, "SELL")
        self.assertEqual(p.name, "bearish_engulfing")
        self.assertGreater(p.stop, max(curr.high, prev.high))

    def test_not_engulfing_same_direction(self):
        # 两根同向 K 线不是吞没
        prev = c(o=98.0, high=102.0, low=97.5, close=101.5)
        curr = c(o=101.5, high=104.0, low=100.0, close=103.0)
        self.assertIsNone(detect_engulfing(prev, curr))

    def test_not_engulfing_when_not_fully_cover(self):
        # 当前实体未完全覆盖前一根实体
        prev = c(o=98.0, high=102.0, low=97.5, close=101.5)
        curr = c(o=98.0, high=101.0, low=96.0, close=100.0)
        self.assertIsNone(detect_engulfing(prev, curr))


class TestSignalSerialization(unittest.TestCase):
    def test_signal_fields(self):
        from signals import pattern_to_signal
        p = detect_pinbar(c(o=100.0, high=101.0, low=90.0, close=100.5))
        sig = pattern_to_signal(p, "BTC-USDT-SWAP")
        # 必须包含 TradingView Webhook 所需字段
        for field in ("action", "ticker", "price", "sl", "tp", "strategy"):
            self.assertIn(field, sig)
        self.assertEqual(sig["ticker"], "BTC-USDT-SWAP")
        self.assertEqual(sig["action"], "BUY")
        self.assertEqual(sig["strategy"], "bullish_pinbar")
        # 盈亏比固定为 2
        self.assertEqual(sig["rr"], 2.0)


if __name__ == "__main__":
    unittest.main()
