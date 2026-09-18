"""量能确认模块的单元测试（纯算法，无需网络）。"""
from __future__ import annotations

import unittest

from okx_client import Candle
from patterns import detect_engulfing, detect_pinbar
from volume import (
    should_filter,
    volume_confirm,
    volume_ratio_at,
    volume_signal,
)


def c(o, high, low, close, volume=1.0, ts=0):
    return Candle(ts=ts, open=o, high=high, low=low, close=close, volume=volume)


def candles(*volumes):
    """构造一串普通 K 线，仅 volume 不同（其余价格随便给）。"""
    return [c(100.0, 101.0, 99.0, 100.5, volume=v, ts=i) for i, v in enumerate(volumes)]


class TestVolumeRatio(unittest.TestCase):
    def test_ratio_is_current_over_past_avg(self):
        # 过去 20 根均量 = (1*19 + 99)/20 = 118/20 = 5.9
        # 当前 volume=20.65，比率 = 20.65/5.9 = 3.5
        vols = [1.0] * 19 + [99.0, 20.65]
        cs = candles(*vols)
        r = volume_ratio_at(cs, len(cs) - 1)
        self.assertAlmostEqual(r, 20.65 / ((1.0 * 19 + 99.0) / 20), places=6)

    def test_ratio_ignores_current_in_avg(self):
        # 当前 100，但过去 20 根均量是 1 => 比率 100（而不是 100/avg 含自身）
        vols = [1.0] * 20 + [100.0]
        cs = candles(*vols)
        r = volume_ratio_at(cs, len(cs) - 1)
        self.assertAlmostEqual(r, 100.0 / 1.0, places=6)

    def test_ratio_none_when_insufficient_history(self):
        # 只有 19 根，不足 20 根均量 => None
        cs = candles(*([1.0] * 19))
        self.assertIsNone(volume_ratio_at(cs, 19 - 1))

    def test_ratio_none_when_zero_avg(self):
        # 过去 20 根成交量全为 0 => 无法计算，返回 None
        vols = [0.0] * 20 + [10.0]
        cs = candles(*vols)
        self.assertIsNone(volume_ratio_at(cs, len(cs) - 1))


class TestVolumeSignal(unittest.TestCase):
    def test_strong(self):
        self.assertEqual(volume_signal(1.2), "strong")
        self.assertEqual(volume_signal(3.5), "strong")

    def test_weak(self):
        self.assertEqual(volume_signal(0.79), "weak")
        self.assertEqual(volume_signal(0.1), "weak")

    def test_neutral(self):
        self.assertEqual(volume_signal(0.9), "neutral")
        self.assertEqual(volume_signal(1.0), "neutral")
        self.assertEqual(volume_signal(None), "neutral")


class TestVolumeConfirm(unittest.TestCase):
    def test_pinbar_labels(self):
        self.assertEqual(volume_confirm(1.2, "pinbar"), "放量Pinbar")
        self.assertEqual(volume_confirm(0.7, "pinbar"), "缩量Pinbar")
        self.assertIsNone(volume_confirm(1.0, "pinbar"))

    def test_engulfing_labels(self):
        self.assertEqual(volume_confirm(1.5, "engulfing"), "放量吞没")
        self.assertEqual(volume_confirm(0.5, "engulfing"), "缩量吞没")
        self.assertIsNone(volume_confirm(1.0, "engulfing"))

    def test_none_ratio_gives_none(self):
        self.assertIsNone(volume_confirm(None, "pinbar"))
        self.assertIsNone(volume_confirm(None, "engulfing"))

    def test_unknown_base_gives_none(self):
        self.assertIsNone(volume_confirm(1.5, "triple_top"))


class TestShouldFilter(unittest.TestCase):
    def test_weak_and_low_rr_filtered(self):
        self.assertTrue(should_filter(0.7, 1.5))   # 缩量 + 盈亏比<2 => 过滤

    def test_weak_but_high_rr_kept(self):
        self.assertFalse(should_filter(0.7, 2.0))  # 缩量但盈亏比=2 => 保留
        self.assertFalse(should_filter(0.7, 3.0))  # 缩量但盈亏比高 => 保留

    def test_not_weak_never_filtered(self):
        self.assertFalse(should_filter(1.0, 1.5))  # 中性量 + 低盈亏比 => 不因量能过滤
        self.assertFalse(should_filter(1.5, 1.0))  # 放量 => 不因量能过滤

    def test_none_ratio_not_filtered(self):
        self.assertFalse(should_filter(None, 1.5)) # 无法计算量能 => 不因量能过滤


class TestPatternVolumeThreading(unittest.TestCase):
    """验证量能比率确实附加到了形态与信号上。"""

    def test_pinbar_carries_volume(self):
        p = detect_pinbar(c(100.0, 101.0, 90.0, 100.5), volume_ratio=1.5)
        self.assertIsNotNone(p)
        self.assertEqual(p.volume_ratio, 1.5)
        self.assertEqual(p.volume_confirm, "放量Pinbar")

    def test_pinbar_without_volume_unchanged(self):
        p = detect_pinbar(c(100.0, 101.0, 90.0, 100.5))
        self.assertIsNotNone(p)
        self.assertIsNone(p.volume_ratio)
        self.assertIsNone(p.volume_confirm)

    def test_engulfing_carries_volume(self):
        prev = c(100.0, 100.5, 98.0, 98.5)
        curr = c(98.0, 102.0, 97.5, 101.5)
        p = detect_engulfing(prev, curr, volume_ratio=0.6)
        self.assertIsNotNone(p)
        self.assertEqual(p.volume_ratio, 0.6)
        self.assertEqual(p.volume_confirm, "缩量吞没")

    def test_signal_json_includes_volume_fields_when_enabled(self):
        from signals import pattern_to_signal
        p = detect_pinbar(c(100.0, 101.0, 90.0, 100.5), volume_ratio=1.5)
        sig = pattern_to_signal(p, "BTC-USDT-SWAP")
        self.assertEqual(sig["volume_ratio"], 1.5)
        self.assertEqual(sig["volume_signal"], "strong")
        self.assertEqual(sig["volume_confirm"], "放量Pinbar")

    def test_signal_json_omits_volume_fields_when_disabled(self):
        from signals import pattern_to_signal
        p = detect_pinbar(c(100.0, 101.0, 90.0, 100.5))
        sig = pattern_to_signal(p, "BTC-USDT-SWAP")
        self.assertNotIn("volume_ratio", sig)
        self.assertNotIn("volume_signal", sig)
        self.assertNotIn("volume_confirm", sig)


if __name__ == "__main__":
    unittest.main()
