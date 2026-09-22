"""strategy_config 与新增参数（entry_mode / min_gap）的单元测试。

用 ``unittest`` 编写，因此 ``python -m unittest`` 与 ``pytest`` 均可运行
（与仓库既有测试的运行方式保持一致，无需额外依赖）。

重点验证三件事：
1. **默认行为逐字节不变**——不传新参数时与改造前完全一致；
2. **新参数按预期生效**——breakout 取形态极值、min_gap 按形态类别过滤；
3. **未验证配置的识别与分级**——阻断类需确认，提示类只提示。
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

# 隔离副作用：自动交易状态文件指向临时路径（须在导入 web_dashboard.app 前设置），
# 防止测试覆盖真实的 auto_trade_state.json。
os.environ.setdefault(
    "PA_AUTO_TRADE_STATE",
    os.path.join(tempfile.mkdtemp(prefix="pa_test_state_"), "auto_trade_state.json"))

from okx_client import Candle
from patterns import detect_engulfing, detect_pinbar
import strategy_config as sc


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------
def _candle(o: float, h: float, l: float, c: float, ts: int = 0, vol: float = 100.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=vol)


# 看涨 Pinbar：长下影、收盘在上 1/3
_BULL_PINBAR = _candle(98.9, 100.2, 96.8, 99.9)
# 看跌 Pinbar：长上影、收盘在下 1/3
_BEAR_PINBAR = _candle(100.1, 103.4, 99.8, 100.0)
# 看涨吞没用：前阴(实体[100.1,100.6]) 被当前阳(实体[100.0,101.0]) 完全覆盖
_ENG_PREV_BEAR = _candle(100.6, 100.7, 99.9, 100.1)
_ENG_CUR_BULL = _candle(100.0, 101.2, 99.8, 101.0)


def _dash_candles() -> list:
    """构造一段含同类聚集信号的 K 线（连续 5 根看涨 Pinbar + 中性尾部）。

    末尾的中性 K 线保证有足够数据判定信号结局（WIN/LOSS/OPEN）。
    """
    candles: list[Candle] = []
    ts = 1_700_000_000_000
    step = 300_000
    for i in range(5):
        candles.append(_candle(100.0, 100.3, 95.0, 100.1, ts=ts + i * step))
    for i in range(10):
        candles.append(_candle(100.0, 100.2, 99.8, 100.0, ts=ts + (5 + i) * step))
    return candles


# ---------------------------------------------------------------------------
# 1. 预设与默认值
# ---------------------------------------------------------------------------
class TestPresets(unittest.TestCase):
    def test_default_preset_is_legacy(self):
        self.assertEqual(sc.DEFAULT_PRESET, "legacy")
        self.assertEqual(sc.preset_name(None), "legacy")

    def test_legacy_preset_values_unchanged(self):
        """legacy 预设必须保持原有行为取值（回归保护）。"""
        self.assertEqual(sc.LEGACY["entry_mode"], "close")
        self.assertEqual(sc.LEGACY["atr_mult"], 0)
        self.assertEqual(sc.LEGACY["trend_period"], 50)
        self.assertIs(sc.LEGACY["volume_filter"], True)
        self.assertEqual(sc.LEGACY["min_gap"], 0)
        self.assertEqual(sc.LEGACY["rr"], 2.0)

    def test_candidate_preset_is_unvalidated(self):
        """candidate 是"实测最好但不可交易"的候选，不得被当成已验证配置。"""
        cfg = sc.resolve("candidate", use_env=False)
        self.assertEqual(cfg["entry_mode"], "breakout")
        self.assertEqual(cfg["min_gap"], 40)   # 实测最优（g=40），非旧值 8
        self.assertIs(sc.is_unvalidated(cfg), True)
        self.assertIs(sc.needs_confirmation(cfg), True)

    def test_optimal_alias_still_resolves_to_candidate(self):
        """历史名 optimal 必须继续可用，并解析为 candidate（不退化成 legacy）。"""
        cfg = sc.resolve("optimal", use_env=False)
        self.assertEqual(cfg["preset"], "candidate")     # 规范化后的权威名
        self.assertEqual(cfg["min_gap"], 40)
        self.assertIs(sc.is_unvalidated(cfg), True)
        # 大小写与空白也应容忍
        self.assertEqual(sc.resolve("  OPTIMAL  ", use_env=False)["preset"], "candidate")

    def test_normalize_maps_optimal_alias(self):
        """normalize 须把历史别名 optimal 规范化为 candidate。"""
        self.assertEqual(sc.normalize({"preset": "optimal"})["preset"], "candidate")
        self.assertEqual(sc.normalize({"preset": "candidate"})["preset"], "candidate")

    def test_unknown_preset_falls_back_to_legacy(self):
        self.assertEqual(sc.preset_name("nonexistent"), "legacy")
        self.assertEqual(sc.resolve("nonexistent", use_env=False)["entry_mode"], "close")

    def test_resolve_priority_env_over_preset(self):
        """优先级：预设 < 环境变量 < 显式参数。"""
        keys = ("PA_INTERVAL",)
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["PA_INTERVAL"] = "15m"
            self.assertEqual(sc.resolve("legacy", use_env=True)["interval"], "15m")
            self.assertEqual(
                sc.resolve("legacy", use_env=True, interval="1H")["interval"], "1H")
            # 显式传 None 视为"未指定"，不覆盖
            self.assertEqual(
                sc.resolve("legacy", use_env=True, entry_mode=None)["entry_mode"], "close")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_env_overrides_coercion(self):
        keys = ("PA_ENTRY_MODE", "PA_MIN_GAP", "PA_VOLUME_FILTER")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update({"PA_ENTRY_MODE": "breakout",
                               "PA_MIN_GAP": "8",
                               "PA_VOLUME_FILTER": "0"})
            cfg = sc.resolve("legacy", use_env=True)
            self.assertEqual(cfg["entry_mode"], "breakout")
            self.assertEqual(cfg["min_gap"], 8)
            self.assertIs(cfg["volume_filter"], False)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ---------------------------------------------------------------------------
# 2. normalize
# ---------------------------------------------------------------------------
class TestNormalize(unittest.TestCase):
    def test_fixes_entry_mode_and_gap(self):
        cfg = sc.normalize({"entry_mode": "BREAKOUT ", "min_gap": "8"})
        self.assertEqual(cfg["entry_mode"], "breakout")
        self.assertEqual(cfg["min_gap"], 8)

    def test_rejects_bad_values(self):
        cfg = sc.normalize({"entry_mode": "nonsense", "min_gap": "abc"})
        self.assertEqual(cfg["entry_mode"], "close")
        self.assertEqual(cfg["min_gap"], 0)

    def test_derives_preset_from_entry_mode(self):
        """无 preset 键时按字段自洽推导（仪表盘配置字典无 preset）。"""
        self.assertEqual(sc.normalize({"entry_mode": "breakout"}).get("preset"), "candidate")
        self.assertEqual(sc.normalize({"entry_mode": "close"}).get("preset"), "legacy")

    def test_candidate_gap_matches_measured_optimum(self):
        """candidate 的 min_gap 必须是实测最优的 40，而非旧值 8。

        依据 STRATEGY_CONCLUSIONS.md 第五版 §2.4（ETH 4H）：
          g=40 基准 +0.161R / t=1.84（全表最高），全叠加后仅削 24%；
          g=8  基准 +0.065R / t=1.38，全叠加后削 63%（对成本最脆弱）。
        """
        self.assertEqual(sc.CANDIDATE["min_gap"], 40)
        # 别名指向同一份配置
        self.assertIs(sc.OPTIMAL, sc.CANDIDATE)


# ---------------------------------------------------------------------------
# 3. 未验证配置的分级
# ---------------------------------------------------------------------------
class TestUnvalidatedGrading(unittest.TestCase):
    def test_empty_for_safe_config(self):
        cfg = {"preset": "legacy", "entry_mode": "close", "trend_period": 50}
        self.assertEqual(sc.unvalidated_reasons(cfg), [])
        self.assertIs(sc.is_unvalidated(cfg), False)
        self.assertEqual(sc.warning_lines(cfg), [])

    def test_breakout_is_blocking(self):
        cfg = {"entry_mode": "breakout", "trend_period": 50}
        self.assertNotEqual(sc.blocking_reasons(cfg), [])
        self.assertIs(sc.needs_confirmation(cfg), True)
        self.assertTrue(any("需确认" in ln for ln in sc.warning_lines(cfg)))

    def test_trend_off_is_notice_only(self):
        """单独关闭趋势过滤只提示、不阻断（避免告警疲劳）。"""
        cfg = {"preset": "legacy", "entry_mode": "close", "trend_period": 0}
        self.assertNotEqual(sc.unvalidated_reasons(cfg), [])   # 有提示
        self.assertEqual(sc.blocking_reasons(cfg), [])         # 但不阻断
        self.assertIs(sc.needs_confirmation(cfg), False)
        lines = sc.warning_lines(cfg)
        self.assertTrue(lines)
        self.assertTrue(any("仅提示" in ln for ln in lines))

    def test_needs_confirmation_live_flag(self):
        cfg = {"entry_mode": "breakout", "trend_period": 50}
        self.assertIs(sc.needs_confirmation(cfg, live=True), True)
        self.assertIs(sc.needs_confirmation(cfg, live=False), False)

    def test_summary_is_single_line(self):
        s = sc.summary(sc.resolve("legacy", use_env=False))
        self.assertNotIn("\n", s)
        self.assertIn("preset=legacy", s)


# ---------------------------------------------------------------------------
# 4. filter_min_gap
# ---------------------------------------------------------------------------
class TestFilterMinGap(unittest.TestCase):
    def test_noop_when_small(self):
        items = [(0, "a"), (1, "b")]
        self.assertEqual(
            sc.filter_min_gap(items, 0, kind_of=lambda x: "pinbar", order_of=lambda x: x[0]),
            items)
        self.assertEqual(
            sc.filter_min_gap(items, 1, kind_of=lambda x: "pinbar", order_of=lambda x: x[0]),
            items)

    def test_drops_clustered_same_kind(self):
        """同类信号间隔不足则丢弃靠后的，保留最早的那个。"""
        items = [(0, "pinbar"), (1, "pinbar"), (2, "pinbar"), (10, "pinbar")]
        kept = sc.filter_min_gap(items, 3, kind_of=lambda x: x[1], order_of=lambda x: x[0])
        self.assertEqual([x[0] for x in kept], [0, 10])

    def test_separates_by_kind(self):
        """不同类别互不影响（pinbar 与 engulfing 各自计间隔）。"""
        items = [(0, "pinbar"), (1, "engulfing"), (2, "pinbar")]
        kept = sc.filter_min_gap(items, 3, kind_of=lambda x: x[1], order_of=lambda x: x[0])
        self.assertEqual([x[0] for x in kept], [0, 1])

    def test_pattern_kind(self):
        self.assertEqual(sc.pattern_kind("bullish_pinbar"), "pinbar")
        self.assertEqual(sc.pattern_kind("bearish_engulfing"), "engulfing")


# ---------------------------------------------------------------------------
# 5. patterns.entry_mode —— close 必须与改造前逐字节一致
# ---------------------------------------------------------------------------
class TestPatternEntryMode(unittest.TestCase):
    def test_pinbar_default_is_close_entry(self):
        p = detect_pinbar(_BULL_PINBAR)
        self.assertIsNotNone(p)
        self.assertEqual(p.entry, _BULL_PINBAR.close)
        self.assertEqual(p.entry_mode, "close")

    def test_pinbar_breakout_uses_high(self):
        p = detect_pinbar(_BULL_PINBAR, entry_mode="breakout")
        self.assertIsNotNone(p)
        self.assertEqual(p.entry, _BULL_PINBAR.high)   # 看涨取最高价
        self.assertEqual(p.entry_mode, "breakout")

    def test_pinbar_bearish_breakout_uses_low(self):
        p = detect_pinbar(_BEAR_PINBAR, entry_mode="breakout")
        self.assertIsNotNone(p)
        self.assertEqual(p.action, "SELL")
        self.assertEqual(p.entry, _BEAR_PINBAR.low)     # 看跌取最低价

    def test_invalid_entry_mode_falls_back_to_close(self):
        p = detect_pinbar(_BULL_PINBAR, entry_mode="garbage")
        self.assertIsNotNone(p)
        self.assertEqual(p.entry, _BULL_PINBAR.close)

    def test_engulfing_breakout_uses_extreme(self):
        prev, cur = _ENG_PREV_BEAR, _ENG_CUR_BULL
        p_close = detect_engulfing(prev, cur)
        p_break = detect_engulfing(prev, cur, entry_mode="breakout")
        self.assertIsNotNone(p_close)
        self.assertIsNotNone(p_break)
        self.assertEqual(p_close.action, "BUY")
        self.assertEqual(p_close.entry, cur.close)
        self.assertEqual(p_break.entry, cur.high)

    def test_breakout_changes_risk_and_target(self):
        """突破入场抬高入场价 → 风险与目标价同步变化（自洽性）。"""
        p_close = detect_pinbar(_BULL_PINBAR)
        p_break = detect_pinbar(_BULL_PINBAR, entry_mode="breakout")
        self.assertNotEqual(p_break.risk, p_close.risk)
        self.assertAlmostEqual(p_break.take_profit, p_break.entry + 2.0 * p_break.risk, places=9)

    def test_legacy_detection_identical_across_all_args(self):
        """显式传默认值与完全不传，结果必须一致（行为不变的强保证）。"""
        a = detect_pinbar(_BULL_PINBAR)
        b = detect_pinbar(_BULL_PINBAR, rr=2.0, atr=None, atr_mult=None, entry_mode="close")
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# 6. 仪表盘 detect_signals 的新参数
# ---------------------------------------------------------------------------
class TestDashboardDetectSignals(unittest.TestCase):
    @staticmethod
    def _run(candles, **kw):
        from web_dashboard import app as A
        opts = dict(trend_period=0, volume_enabled=False,
                    scan_window=len(candles) - 1, candles=candles)
        opts.update(kw)
        return A.detect_signals("BTC-USDT-SWAP", "5m", len(candles), **opts)

    def test_min_gap_reduces_count(self):
        candles = _dash_candles()
        base = self._run(candles)
        filtered = self._run(candles, min_gap=3)
        self.assertGreater(len(base), len(filtered),
                           f"未降频: {len(base)} vs {len(filtered)}")
        self.assertGreaterEqual(len(filtered), 1)

    def test_default_equals_explicit_defaults(self):
        """不传 entry_mode / min_gap 与显式传默认值结果一致。"""
        candles = _dash_candles()
        self.assertEqual(self._run(candles),
                         self._run(candles, entry_mode="close", min_gap=0))

    def test_breakout_raises_entry(self):
        candles = _dash_candles()
        close_sigs = self._run(candles, entry_mode="close")
        break_sigs = self._run(candles, entry_mode="breakout")
        self.assertTrue(close_sigs)
        self.assertTrue(break_sigs)
        # 看涨突破入场价 = 该 K 线最高价 > 收盘价
        self.assertGreater(break_sigs[0]["price"], close_sigs[0]["price"])
        self.assertEqual(break_sigs[0]["action"], "BUY")

    def test_invalid_entry_mode_falls_back(self):
        candles = _dash_candles()
        self.assertEqual(self._run(candles, entry_mode="close"),
                         self._run(candles, entry_mode="rubbish"))


# ---------------------------------------------------------------------------
# 7. 自动交易确认闸门（HTTP 层）
# ---------------------------------------------------------------------------
class TestAutoTradeGate(unittest.TestCase):
    def setUp(self):
        from web_dashboard import app as A
        self.A = A
        # 隔离副作用：测试不真起线程、也不写真实的 auto_trade_state.json
        self._orig_thread = A._start_auto_trade_thread
        self._orig_save = A._save_auto_trade_state
        A._start_auto_trade_thread = lambda: None
        A._save_auto_trade_state = lambda: None
        with A._auto_trade_lock:
            self._snapshot = dict(A._auto_trade_config)
            A._auto_trade_config["enabled"] = False
        self.c = A.app.test_client()

    def tearDown(self):
        A = self.A
        with A._auto_trade_lock:
            A._auto_trade_config.clear()
            A._auto_trade_config.update(self._snapshot)   # 完整还原，避免污染后续测试
        A._start_auto_trade_thread = self._orig_thread
        A._save_auto_trade_state = self._orig_save

    def test_blocks_breakout_without_confirm(self):
        r = self.c.post("/api/auto-trade", json={
            "ticker": "ETH-USDT-SWAP", "interval": "4H",
            "entry_mode": "breakout", "min_gap": 8, "trend_period": 0})
        self.assertEqual(r.status_code, 409)
        d = r.get_json()
        self.assertEqual(d["status"], "needs_confirmation")
        self.assertTrue(d["reasons"])
        self.assertTrue(d["warning"])
        self.assertIs(self.A._auto_trade_config["enabled"], False)   # 未启动

    def test_allows_breakout_with_confirm(self):
        r = self.c.post("/api/auto-trade", json={
            "ticker": "ETH-USDT-SWAP", "interval": "4H",
            "entry_mode": "breakout", "min_gap": 8, "trend_period": 0,
            "confirm_unvalidated": True})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["status"], "ok")
        self.assertIs(d["unvalidated"], True)
        self.assertIs(self.A._auto_trade_config["enabled"], True)
        self.assertEqual(self.A._auto_trade_config["entry_mode"], "breakout")
        self.assertEqual(self.A._auto_trade_config["min_gap"], 8)

    def test_safe_config_starts_without_confirm(self):
        r = self.c.post("/api/auto-trade", json={
            "ticker": "BTC-USDT-SWAP", "interval": "5m",
            "entry_mode": "close", "min_gap": 0, "trend_period": 50})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIs(d["unvalidated"], False)
        self.assertEqual(d["warning"], [])
        self.assertIs(self.A._auto_trade_config["enabled"], True)

    def test_trend_off_notice_only_no_block(self):
        """仅关闭趋势过滤：不阻断启动，但要带提示。"""
        r = self.c.post("/api/auto-trade", json={
            "ticker": "BTC-USDT-SWAP", "interval": "5m",
            "entry_mode": "close", "trend_period": 0, "min_gap": 0})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIs(d["unvalidated"], True)    # 有提示
        self.assertEqual(d["blocking"], [])      # 不阻断
        self.assertIs(self.A._auto_trade_config["enabled"], True)

    def test_sanitizes_invalid_values(self):
        self.c.post("/api/auto-trade", json={
            "entry_mode": "garbage", "min_gap": -5, "trend_period": 50})
        self.assertEqual(self.A._auto_trade_config["entry_mode"], "close")
        self.assertEqual(self.A._auto_trade_config["min_gap"], 0)

    def test_min_gap_one_normalized_to_zero(self):
        """0 与 1 语义相同（都不过滤），统一存 0 避免歧义。"""
        self.c.post("/api/auto-trade", json={
            "entry_mode": "close", "min_gap": 1, "trend_period": 50})
        self.assertEqual(self.A._auto_trade_config["min_gap"], 0)

    def test_get_exposes_new_fields(self):
        d = self.c.get("/api/auto-trade").get_json()
        self.assertIn("entry_mode", d["config"])
        self.assertIn("min_gap", d["config"])
        for k in ("unvalidated", "blocking", "notice"):
            self.assertIn(k, d)

    def test_stop_resets_and_keeps_params(self):
        self.c.post("/api/auto-trade", json={
            "entry_mode": "breakout", "min_gap": 8,
            "trend_period": 0, "confirm_unvalidated": True})
        self.assertIs(self.A._auto_trade_config["enabled"], True)
        self.c.post("/api/auto-trade/stop")
        self.assertIs(self.A._auto_trade_config["enabled"], False)
        self.assertEqual(self.A._auto_trade_config["entry_mode"], "breakout")  # 参数保留
        self.assertEqual(self.A._auto_trade_config["min_gap"], 8)

    def test_state_persistence_includes_new_keys(self):
        """重启不丢：恢复列表必须包含新字段。"""
        src = open(self.A.__file__, encoding="utf-8").read()
        self.assertIn('"entry_mode", "min_gap"', src)


# ---------------------------------------------------------------------------
# 8. main.py CLI
# ---------------------------------------------------------------------------
class TestCli(unittest.TestCase):
    def test_bare_defaults_unchanged(self):
        """裸命令的参数必须与改造前一致。"""
        import main
        args = main.apply_preset(main.parse_args([]))
        self.assertEqual(args.inst_id, "BTC-USDT-SWAP")
        self.assertEqual(args.bar, "5m")
        self.assertEqual(args.limit, 300)
        self.assertEqual(args.trend, 0)
        self.assertEqual(args.ma_type, "sma")
        self.assertIs(args.volume, False)
        self.assertEqual(args.entry_mode, "close")
        self.assertEqual(args.min_gap, 0)
        self.assertEqual(args.rr, 2.0)
        self.assertIsNone(args.preset)
        # 第八轮新增参数：默认必须是"不介入"的值，否则会改变既有行为
        self.assertEqual(args.atr_mult, 0)
        self.assertEqual(args.signal_mode, "pattern")

    def test_preset_candidate_fills_unspecified(self):
        import main
        args = main.apply_preset(main.parse_args(["--preset", "candidate"]))
        self.assertEqual(args.inst_id, "ETH-USDT-SWAP")
        self.assertEqual(args.bar, "4H")
        self.assertEqual(args.entry_mode, "breakout")
        self.assertEqual(args.min_gap, 40)   # 实测最优间距
        self.assertEqual(args.trend, 0)

    def test_preset_optimal_alias_fills_same(self):
        """历史名 optimal 展开结果须与 candidate 完全一致。"""
        import main
        a = main.apply_preset(main.parse_args(["--preset", "candidate"]))
        b = main.apply_preset(main.parse_args(["--preset", "optimal"]))
        self.assertEqual(a.inst_id, b.inst_id)
        self.assertEqual(a.bar, b.bar)
        self.assertEqual(a.entry_mode, b.entry_mode)
        self.assertEqual(a.min_gap, b.min_gap)
        self.assertEqual(a.trend, b.trend)

    def test_explicit_value_beats_preset(self):
        import main
        args = main.apply_preset(main.parse_args(["--preset", "candidate", "--bar", "1H"]))
        self.assertEqual(args.bar, "1H")                   # 显式值胜出
        self.assertEqual(args.inst_id, "ETH-USDT-SWAP")    # 其余仍取预设

    def test_entry_mode_and_gap_parsed(self):
        import main
        args = main.parse_args(["--entry-mode", "breakout", "--min-gap", "12"])
        self.assertEqual(args.entry_mode, "breakout")
        self.assertEqual(args.min_gap, 12)

    def test_preset_choices_restricted(self):
        import main
        with self.assertRaises(SystemExit):
            main.parse_args(["--preset", "bogus"])

    def test_min_gap_filtering_in_scan(self):
        """--scan + --min-gap 走通的冒烟测试。"""
        import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main.main(["--mock", "--scan", "3", "--min-gap", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("同形态间隔≥2根", buf.getvalue())

    def test_bare_run_prints_no_warning(self):
        """裸命令不应打印任何未验证告警（CLI 基线本就是趋势过滤关闭）。"""
        import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main.main(["--mock"])
        self.assertNotIn("未经独立验证", err.getvalue())

    def test_breakout_run_prints_warning(self):
        import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main.main(["--mock", "--entry-mode", "breakout"])
        self.assertIn("未经独立验证", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# 9. ATR 计算（atr.py）—— 第八轮新增，无前视 + 与扫描脚本口径一致
# ---------------------------------------------------------------------------
class TestAtr(unittest.TestCase):
    @staticmethod
    def _cs():
        # 手工可验的 5 根：TR 依次为 2.0（首根用 high−low）等
        return [
            _candle(100.0, 101.0, 99.0, 100.5),
            _candle(100.5, 102.0, 100.0, 101.5),
            _candle(101.5, 103.0, 101.0, 102.5),
            _candle(102.5, 104.0, 102.0, 103.5),
            _candle(103.5, 105.0, 103.0, 104.5),
        ]

    @staticmethod
    def _ref(cs, idx, period):
        """独立实现一份 TR/ATR 作为参照（与 probe_pa_stopwidth.py 口径一致）。"""
        trs = []
        for i in range(len(cs)):
            if i == 0:
                trs.append(cs[i].high - cs[i].low)
                continue
            p = cs[i - 1].close
            trs.append(max(cs[i].high - cs[i].low,
                           abs(cs[i].high - p), abs(cs[i].low - p)))
        if idx + 1 < period or idx < 0 or idx >= len(cs):
            return None
        return sum(trs[idx + 1 - period: idx + 1]) / period

    def test_matches_reference(self):
        from atr import atr_at
        cs = self._cs()
        for idx in range(len(cs)):
            self.assertEqual(atr_at(cs, idx, 3), self._ref(cs, idx, 3),
                             f"idx={idx} 与参照口径不一致")

    def test_series_equals_pointwise(self):
        from atr import atr_at, atr_series
        cs = self._cs()
        self.assertEqual(atr_series(cs, 3), [self._ref(cs, i, 3) for i in range(len(cs))])

    def test_insufficient_data_returns_none(self):
        from atr import atr_at
        cs = self._cs()
        self.assertIsNone(atr_at(cs, 0, 14))   # 数据不足
        self.assertIsNone(atr_at(cs, 1, 3))    # idx+1 < period

    def test_invalid_args_return_none(self):
        from atr import atr_at
        cs = self._cs()
        self.assertIsNone(atr_at(cs, 4, 0))
        self.assertIsNone(atr_at(cs, 4, -3))
        self.assertIsNone(atr_at(cs, 4, "abc"))
        self.assertIsNone(atr_at(cs, -1, 3))
        self.assertIsNone(atr_at(cs, 99, 3))

    def test_no_lookahead(self):
        """只依赖 idx 及之前的数据：截断之后的结果必须不变。"""
        from atr import atr_at
        cs = self._cs()
        full = atr_at(cs, 3, 3)
        truncated = atr_at(cs[:4], 3, 3)
        self.assertEqual(full, truncated)

    def test_true_range_uses_prev_close(self):
        from atr import true_range
        # 跳空高开：TR 应由 |high − prev_close| 主导，而非 high−low
        c = _candle(110.0, 112.0, 109.0, 111.0)
        self.assertEqual(true_range(100.0, c), 12.0)   # |112 − 100|
        self.assertEqual(true_range(None, c), 3.0)     # 首根退化为 high−low


# ---------------------------------------------------------------------------
# 10. atr_mult 止损宽度 —— 放宽止损（第八轮唯一被数据支持的成分）
# ---------------------------------------------------------------------------
class TestAtrStop(unittest.TestCase):
    def test_default_stop_is_structural(self):
        """不传 atr_mult 时止损口径与原有行为完全一致。"""
        from atr import atr_at
        cs = TestAtr._cs()
        a = atr_at(cs, 4, 3)
        p = detect_pinbar(_BULL_PINBAR, atr=a, atr_mult=None)
        self.assertIsNotNone(p)
        # 结构止损：影线极值外侧
        self.assertLess(p.stop, _BULL_PINBAR.low)

    def test_atr_mult_widens_stop(self):
        """atr_mult 生效时止损应比结构止损更远（风险更大、但 f 更小）。"""
        p_struct = detect_pinbar(_BULL_PINBAR)
        # 结构止损距入场约 3.2（99.9 → 96.70）；
        # atr=4.0、mult=1.5 → ATR 止损距入场 6.0，严格更远。
        p_atr = detect_pinbar(_BULL_PINBAR, atr=4.0, atr_mult=1.5)
        self.assertIsNotNone(p_atr)
        self.assertLess(p_atr.stop, p_struct.stop, "ATR 止损应更宽（更低）")
        self.assertGreater(p_atr.risk, p_struct.risk)

    def test_atr_mult_never_tightens_stop(self):
        """ATR 止损比结构止损更近时，保留更远的结构止损（不缩紧风险）。"""
        p_struct = detect_pinbar(_BULL_PINBAR)
        # atr=1.0、mult=1.5 → ATR 止损距入场仅 1.5 < 3.2，应保留结构止损
        p_near = detect_pinbar(_BULL_PINBAR, atr=1.0, atr_mult=1.5)
        self.assertIsNotNone(p_near)
        self.assertEqual(p_near.stop, p_struct.stop)

    def test_atr_mult_lowers_f(self):
        """核心机制：放宽止损使 f（费率占 R 比例）下降。"""
        p_struct = detect_pinbar(_BULL_PINBAR)
        p_atr = detect_pinbar(_BULL_PINBAR, atr=4.0, atr_mult=1.5)
        rt = 0.0007
        f_struct = rt * p_struct.entry / p_struct.risk
        f_atr = rt * p_atr.entry / p_atr.risk
        self.assertLess(f_atr, f_struct, "ATR 宽止损的 f 必须更低")

    def test_partial_of_newer_stop_kept(self):
        """取"离入场更远"的一侧：即使 ATR 止损更近，也不能比结构止损更近。"""
        from patterns import _stop_price
        # BUY：结构止损 95，ATR 止损 98 → 应保留 95（更远）
        self.assertEqual(_stop_price("BUY", 100.0, 95.0, atr=2.0, atr_mult=1.0), 95.0)
        # BUY：结构止损 99，ATR 止损 96 → 应取 96
        self.assertEqual(_stop_price("BUY", 100.0, 99.0, atr=2.0, atr_mult=2.0), 96.0)
        # SELL 对称
        self.assertEqual(_stop_price("SELL", 100.0, 105.0, atr=2.0, atr_mult=1.0), 105.0)
        self.assertEqual(_stop_price("SELL", 100.0, 101.0, atr=2.0, atr_mult=2.0), 104.0)

    def test_bad_atr_mult_falls_back(self):
        """非法 atr_mult / atr 一律回退结构止损（不猜）。"""
        from patterns import _stop_price
        self.assertEqual(_stop_price("BUY", 100.0, 95.0, atr=2.0, atr_mult=0), 95.0)
        self.assertEqual(_stop_price("BUY", 100.0, 95.0, atr=2.0, atr_mult=-1), 95.0)
        self.assertEqual(_stop_price("BUY", 100.0, 95.0, atr=None, atr_mult=1.5), 95.0)
        self.assertEqual(_stop_price("BUY", 100.0, 95.0, atr=0.0, atr_mult=1.5), 95.0)


# ---------------------------------------------------------------------------
# 11. 纯趋势信号（build_trend_signal）—— 不使用任何形态
# ---------------------------------------------------------------------------
class TestTrendSignal(unittest.TestCase):
    def test_builds_with_atr_stop(self):
        from patterns import build_trend_signal
        p = build_trend_signal(_BULL_PINBAR, "BUY", atr=2.0, atr_mult=1.5, rr=2.0)
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "trend_follow")
        self.assertEqual(p.action, "BUY")
        self.assertAlmostEqual(p.stop, p.entry - 3.0, places=6)   # 1.5 × 2.0
        self.assertAlmostEqual(p.risk, 3.0, places=6)

    def test_sell_side(self):
        from patterns import build_trend_signal
        p = build_trend_signal(_BULL_PINBAR, "SELL", atr=2.0, atr_mult=1.5)
        self.assertAlmostEqual(p.stop, p.entry + 3.0, places=6)

    def test_invalid_atr_returns_none(self):
        """没有 ATR 就不猜止损（宁可不发信号）。"""
        from patterns import build_trend_signal
        self.assertIsNone(build_trend_signal(_BULL_PINBAR, "BUY", atr=None))
        self.assertIsNone(build_trend_signal(_BULL_PINBAR, "BUY", atr=0.0))
        self.assertIsNone(build_trend_signal(_BULL_PINBAR, "BUY", atr=2.0, atr_mult=0))
        self.assertIsNone(build_trend_signal(_BULL_PINBAR, "HOLD", atr=2.0))

    def test_uses_close_as_entry(self):
        from patterns import build_trend_signal
        p = build_trend_signal(_BULL_PINBAR, "BUY", atr=2.0)
        self.assertAlmostEqual(p.entry, _BULL_PINBAR.close, places=9)


# ---------------------------------------------------------------------------
# 12. 趋势斜率判定（trend_slope_at）—— 第八轮纯趋势的口径
# ---------------------------------------------------------------------------
class TestTrendSlope(unittest.TestCase):
    @staticmethod
    def _ramp(n, step, base=100.0):
        """构造单调上升/下降的价格序列（base 保证下行走势价格仍为正）。"""
        return [_candle(base + i * step, base + i * step + 1,
                        base + i * step - 1, base + i * step, ts=i)
                for i in range(n)]

    def test_up_and_down(self):
        """生产路径总会传入 ATR（阈值按波动率缩放）。"""
        from trend import trend_slope_at
        up = self._ramp(60, 1.0)
        down = self._ramp(60, -1.0, base=300.0)
        self.assertEqual(trend_slope_at(up, 59, 20, atr=2.0), "up")
        self.assertEqual(trend_slope_at(down, 59, 20, atr=2.0), "down")

    def test_without_atr_uses_fixed_threshold(self):
        """无 ATR 时退回固定阈值 0.2：足够陡的斜率仍可判向。"""
        from trend import trend_slope_at
        strong = self._ramp(60, 5.0)   # 相对斜率约 0.40 > 0.2
        self.assertEqual(trend_slope_at(strong, 59, 20), "up")
        # 缓坡（相对斜率约 0.15）在固定阈值下判为无趋势
        self.assertIsNone(trend_slope_at(self._ramp(60, 1.0), 59, 20))

    def test_flat_returns_none(self):
        from trend import trend_slope_at
        flat = self._ramp(60, 0.0)
        self.assertIsNone(trend_slope_at(flat, 59, 20), "横盘应判为无趋势")

    def test_insufficient_data_returns_none(self):
        from trend import trend_slope_at
        cs = self._ramp(10, 1.0)
        self.assertIsNone(trend_slope_at(cs, 9, 20))   # 需要 2×period 根

    def test_no_lookahead(self):
        from trend import trend_slope_at, ma_slope_at
        cs = self._ramp(60, 1.0)
        self.assertEqual(ma_slope_at(cs, 40, 20), ma_slope_at(cs[:41], 40, 20))
        self.assertEqual(trend_slope_at(cs, 40, 20), trend_slope_at(cs[:41], 40, 20))

    def test_slope_sign_is_correct(self):
        from trend import ma_slope_at
        up = self._ramp(60, 1.0)
        s = ma_slope_at(up, 59, 20)
        self.assertIsNotNone(s)
        self.assertGreater(s, 0)


# ---------------------------------------------------------------------------
# 13. TREND_WIDE 预设与 signal_mode
# ---------------------------------------------------------------------------
class TestTrendWidePreset(unittest.TestCase):
    def test_preset_registered(self):
        self.assertIn("trend_wide", sc.PRESETS)

    def test_values(self):
        c = sc.resolve("trend_wide", use_env=False)
        self.assertEqual(c["signal_mode"], "trend")     # 纯趋势，不用形态
        self.assertEqual(c["atr_mult"], 1.5)            # ★ 核心
        self.assertEqual(c["trend_period"], 20)
        self.assertEqual(c["entry_mode"], "close")
        self.assertEqual(c["interval"], "4H")

    def test_legacy_and_candidate_stay_pattern(self):
        """legacy / candidate 必须仍是形态驱动，否则行为会变。"""
        self.assertEqual(sc.resolve("legacy", use_env=False)["signal_mode"], "pattern")
        self.assertEqual(sc.resolve("candidate", use_env=False)["signal_mode"], "pattern")
        self.assertEqual(sc.resolve("legacy", use_env=False)["atr_mult"], 0)
        self.assertEqual(sc.resolve("candidate", use_env=False)["atr_mult"], 0)

    def test_normalize_defaults_signal_mode(self):
        self.assertEqual(sc.normalize({})["signal_mode"], "pattern")
        self.assertEqual(sc.normalize({"signal_mode": "TREND "})["signal_mode"], "trend")
        self.assertEqual(sc.normalize({"signal_mode": "nonsense"})["signal_mode"], "pattern")

    def test_normalize_derives_trend_wide_preset(self):
        self.assertEqual(sc.normalize({"signal_mode": "trend"}).get("preset"), "trend_wide")

    def test_atr_mult_kept_as_float(self):
        """1.5 不能被截断成 1（会让 f 差一倍）。"""
        v = sc.normalize({"atr_mult": 1.5})["atr_mult"]
        self.assertIsInstance(v, float)
        self.assertEqual(v, 1.5)

    def test_atr_mult_invalid_to_zero(self):
        self.assertEqual(sc.normalize({"atr_mult": "abc"})["atr_mult"], 0.0)
        self.assertEqual(sc.normalize({"atr_mult": -3})["atr_mult"], 0.0)

    def test_trend_wide_is_blocking(self):
        """trend_wide 属阻断类：真金前必须显式确认。"""
        c = {"preset": "trend_wide", "atr_mult": 1.5, "signal_mode": "trend"}
        self.assertTrue(sc.blocking_reasons(c))
        self.assertTrue(sc.needs_confirmation(c, live=True))
        self.assertFalse(sc.needs_confirmation(c, live=False))

    def test_atr_mult_alone_is_blocking(self):
        c = {"preset": "legacy", "entry_mode": "close", "atr_mult": 2.0,
             "signal_mode": "pattern", "trend_period": 50}
        self.assertTrue(sc.blocking_reasons(c))

    def test_legacy_safe_config_not_blocking(self):
        c = {"preset": "legacy", "entry_mode": "close", "atr_mult": 0,
             "signal_mode": "pattern", "trend_period": 50}
        self.assertFalse(sc.blocking_reasons(c))
        self.assertEqual(sc.warning_lines(c), [])

    def test_warning_mentions_eighth_round(self):
        c = {"preset": "trend_wide", "atr_mult": 1.5, "signal_mode": "trend"}
        txt = "\n".join(sc.warning_lines(c))
        self.assertIn("PRICE_ACTION_SYNTHESIS", txt)
        self.assertIn("鞅", txt)

    def test_summary_shows_atr_and_mode(self):
        c = sc.resolve("trend_wide", use_env=False)
        s = sc.summary(c)
        self.assertIn("ATR×1.5", s)


# ---------------------------------------------------------------------------
# 14. 环境变量 PA_ATR_MULT / CLI --atr-mult / --signal-mode
# ---------------------------------------------------------------------------
class TestAtrEnvAndCli(unittest.TestCase):
    def test_env_atr_mult_parsed_as_float(self):
        old = os.environ.get("PA_ATR_MULT")
        os.environ["PA_ATR_MULT"] = "1.5"
        try:
            cfg = sc.resolve("legacy", use_env=True)
            self.assertEqual(cfg["atr_mult"], 1.5)
            self.assertIsInstance(cfg["atr_mult"], float)
        finally:
            if old is None:
                os.environ.pop("PA_ATR_MULT", None)
            else:
                os.environ["PA_ATR_MULT"] = old

    def test_env_atr_mult_invalid_ignored(self):
        old = os.environ.get("PA_ATR_MULT")
        os.environ["PA_ATR_MULT"] = "not-a-number"
        try:
            cfg = sc.resolve("legacy", use_env=True)
            self.assertEqual(cfg["atr_mult"], 0)   # 回退预设值
        finally:
            if old is None:
                os.environ.pop("PA_ATR_MULT", None)
            else:
                os.environ["PA_ATR_MULT"] = old

    def test_cli_parses_atr_mult_and_signal_mode(self):
        import main
        args = main.parse_args(["--atr-mult", "2.5", "--signal-mode", "trend"])
        self.assertEqual(args.atr_mult, 2.5)
        self.assertEqual(args.signal_mode, "trend")

    def test_preset_trend_wide_fills_unspecified(self):
        import main
        args = main.apply_preset(main.parse_args(["--preset", "trend_wide"]))
        self.assertEqual(args.signal_mode, "trend")
        self.assertEqual(args.atr_mult, 1.5)
        self.assertEqual(args.trend, 20)
        self.assertEqual(args.bar, "4H")

    def test_explicit_atr_beats_preset(self):
        import main
        args = main.apply_preset(main.parse_args(["--preset", "trend_wide", "--atr-mult", "3"]))
        self.assertEqual(args.atr_mult, 3.0)   # 显式值胜出

    def test_env_overrides_reach_cli(self):
        """PA_* 环境变量必须能在 CLI 生效（文档承诺可覆盖，不能只停留在配置层）。"""
        import main
        from _env_guard import set_env
        with set_env(PA_ATR_MULT="2.5", PA_SIGNAL_MODE="trend"):
            args = main.apply_preset(main.parse_args([]))
            self.assertEqual(args.atr_mult, 2.5)
            self.assertEqual(args.signal_mode, "trend")

    def test_explicit_cli_beats_env(self):
        """命令行显式值 > PA_* 环境变量。"""
        import main
        from _env_guard import set_env
        with set_env(PA_ATR_MULT="2.5"):
            args = main.apply_preset(main.parse_args(["--atr-mult", "4"]))
            self.assertEqual(args.atr_mult, 4.0)

    def test_env_beats_preset(self):
        """PA_* 环境变量 > 预设值（预设只填空）。"""
        import main
        from _env_guard import set_env
        with set_env(PA_ATR_MULT="2"):
            args = main.apply_preset(main.parse_args(["--preset", "trend_wide"]))
            self.assertEqual(args.atr_mult, 2.0)
            self.assertEqual(args.signal_mode, "trend")   # 预设仍填充其余项

    def test_env_preset_reaches_cli(self):
        """PA_PRESET 也必须能在 CLI 生效（--preset 未给时）。"""
        import main
        from _env_guard import set_env
        with set_env(PA_PRESET="trend_wide"):
            args = main.apply_preset(main.parse_args([]))
            self.assertEqual(args.signal_mode, "trend")
            self.assertEqual(args.atr_mult, 1.5)
            self.assertEqual(args.preset, "trend_wide")

    def test_env_preset_alias_optimal(self):
        """PA_PRESET=optimal 应被规范化为 candidate。"""
        import main
        from _env_guard import set_env
        with set_env(PA_PRESET="optimal"):
            args = main.apply_preset(main.parse_args([]))
            self.assertEqual(args.preset, "candidate")
            self.assertEqual(args.entry_mode, "breakout")

    def test_no_env_keeps_bare_defaults(self):
        """不设任何 PA_*、不传 --preset 时，仍与改造前逐字节一致。"""
        import main
        from _env_guard import set_env
        with set_env(PA_PRESET=None, PA_ATR_MULT=None, PA_SIGNAL_MODE=None,
                     PA_ENTRY_MODE=None, PA_MIN_GAP=None):
            args = main.apply_preset(main.parse_args([]))
            self.assertIsNone(args.preset)
            self.assertEqual(args.atr_mult, 0)
            self.assertEqual(args.signal_mode, "pattern")
            self.assertEqual(args.entry_mode, "close")
            self.assertEqual(args.min_gap, 0)

    def test_notes_report_atr_fallback_when_insufficient(self):
        """K 线不足 ATR 周期时，标注须诚实说明"实际回退结构止损"。"""
        import main
        cs = [_candle(100.0, 101.0, 99.0, 100.0, ts=i) for i in range(6)]
        args = main.apply_preset(main.parse_args(["--atr-mult", "1.5"]))
        notes = "、".join(main._notes_for(cs, args))
        self.assertIn("回退结构止损", notes)

    def test_notes_claim_atr_only_when_available(self):
        """ATR 真能算出来时才宣称用了 ATR 止损。"""
        import main
        cs = [_candle(100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, ts=i) for i in range(40)]
        args = main.apply_preset(main.parse_args(["--atr-mult", "1.5"]))
        notes = "、".join(main._notes_for(cs, args))
        self.assertIn("止损=ATR×1.5", notes)
        self.assertNotIn("回退", notes)

    def test_signal_mode_choices_restricted(self):
        import main
        with self.assertRaises(SystemExit):
            main.parse_args(["--signal-mode", "bogus"])

    def test_trend_mode_no_signals_on_short_data(self):
        """纯趋势模式在数据不足时应静默返回 0 个信号，而不是报错或乱猜。"""
        import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main.main(["--mock", "--scan", "5", "--signal-mode", "trend", "--trend", "20"])
        self.assertEqual(rc, 0)
        self.assertIn("共检测到 0 个信号", buf.getvalue())
