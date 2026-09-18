"""趋势过滤模块单元测试（纯算法，无需网络）。"""
from __future__ import annotations

import unittest

from okx_client import Candle
from patterns import detect_pinbar
from trend import ema_at, sma, trend_allows, trend_at, UP, DOWN


def _mk(closes: list[float]) -> list[Candle]:
    out = []
    for i, c in enumerate(closes):
        # 构造：open=close 的十字线，方便趋势判断只看收盘
        out.append(Candle(ts=i * 1000, open=c, high=c, low=c, close=c, volume=1.0))
    return out


class TestAverages(unittest.TestCase):
    def test_sma(self):
        self.assertIsNone(sma([1, 2], 3))
        self.assertAlmostEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertAlmostEqual(sma([10, 20, 30], 3), 20.0)

    def test_ema(self):
        # 单值 EMA = 自身
        self.assertAlmostEqual(ema_at([5], 5), 5.0)
        # 全相等序列 EMA 保持该值
        self.assertAlmostEqual(ema_at([7, 7, 7, 7], 3), 7.0)
        # 递增序列 EMA 应低于最新值但高于起点
        ema = ema_at([1, 2, 3, 4, 5], 3)
        self.assertTrue(1 < ema < 5)


class TestTrendAt(unittest.TestCase):
    def test_uptrend(self):
        # 收盘一路上升，最新收盘 > SMA => up
        candles = _mk([10, 11, 12, 13, 14, 15])
        self.assertEqual(trend_at(candles, len(candles) - 1, 3), UP)

    def test_downtrend(self):
        candles = _mk([15, 14, 13, 12, 11, 10])
        self.assertEqual(trend_at(candles, len(candles) - 1, 3), DOWN)

    def test_insufficient_data(self):
        candles = _mk([1, 2])
        # period=5 但只有 3 根 <= idx(2)，无法计算
        self.assertIsNone(trend_at(candles, 2, 5))

    def test_no_lookahead(self):
        # 趋势判断只依赖信号之前的数据；即便后面有暴跌也不影响信号点的 up 判定
        candles = _mk([10, 11, 12, 13, 14, 15, 0, 0, 0])
        self.assertEqual(trend_at(candles, 4, 3), UP)


class TestTrendAllows(unittest.TestCase):
    def setUp(self):
        self.buy = detect_pinbar(
            Candle(ts=0, open=100.0, high=101.0, low=90.0, close=100.5, volume=1.0)
        )  # bullish_pinbar -> BUY
        self.sell = detect_pinbar(
            Candle(ts=0, open=100.0, high=112.0, low=99.0, close=100.5, volume=1.0)
        )  # bearish_pinbar -> SELL

    def test_buy_allowed_in_uptrend(self):
        self.assertTrue(trend_allows(self.buy, UP))
        self.assertFalse(trend_allows(self.buy, DOWN))  # 逆势过滤

    def test_sell_allowed_in_downtrend(self):
        self.assertTrue(trend_allows(self.sell, DOWN))
        self.assertFalse(trend_allows(self.sell, UP))

    def test_unknown_trend_allows(self):
        self.assertTrue(trend_allows(self.buy, None))
        self.assertTrue(trend_allows(self.sell, None))


if __name__ == "__main__":
    unittest.main()
