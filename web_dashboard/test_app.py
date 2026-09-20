"""Web 仪表盘后端接口单元测试（离线，mock 掉 K 线拉取与信号检测）。"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from web_dashboard import app as app_mod


class TestDashboardAPI(unittest.TestCase):
    def setUp(self):
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()

    def test_klines_format(self):
        fake = [
            {"time": 1700000000, "open": 100, "high": 102, "low": 99, "close": 101},
            {"time": 1700000300, "open": 101, "high": 103, "low": 100, "close": 102},
        ]
        with mock.patch.object(app_mod, "fetch_klines", return_value=fake):
            resp = self.client.get("/api/klines?ticker=BTC-USDT-SWAP&interval=5m&limit=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data), 2)
        # lightweight-charts 要求的字段
        for bar in data:
            for k in ("time", "open", "high", "low", "close"):
                self.assertIn(k, bar)

    def test_klines_unsupported_ticker(self):
        resp = self.client.get("/api/klines?ticker=SOL-USDT-SWAP")
        self.assertEqual(resp.status_code, 400)

    def test_signals_format(self):
        fake = [{
            "action": "BUY", "price": 100.5, "sl": 99.0, "tp": 103.5,
            "strategy": "bullish_pinbar", "ratio": 2.0, "timestamp": 1700000000,
            "result": "WIN", "result_ts": 1700000300,
        }]
        with mock.patch.object(app_mod, "detect_signals", return_value=fake):
            resp = self.client.get("/api/signals?ticker=BTC-USDT-SWAP")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data), 1)
        for k in ("action", "price", "sl", "tp", "strategy", "ratio", "timestamp", "result"):
            self.assertIn(k, data[0])

    def test_evaluate_signal_win_loss_open(self):
        """evaluate_signal 复用阶段一回测逻辑，能判定 WIN/LOSS/OPEN。"""
        from okx_client import Candle
        from types import SimpleNamespace

        def mk(ts, o, h, l, c):
            return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=1.0)

        # 直接构造已知的 BUY 信号：entry=100, sl=90, tp=120（可控数值）
        pattern = SimpleNamespace(action="BUY", stop=90.0, take_profit=120.0)
        sig_candle = mk(1000, 100, 100, 100, 100)

        # WIN：之后一根 high 到达 tp
        candles_win = [sig_candle, mk(2000, 100, 121, 99, 120)]
        self.assertEqual(app_mod.evaluate_signal(0, pattern, candles_win)[0], "WIN")

        # LOSS：之后一根 low 跌破 sl
        candles_loss = [sig_candle, mk(2000, 100, 101, 89, 90)]
        self.assertEqual(app_mod.evaluate_signal(0, pattern, candles_loss)[0], "LOSS")

        # OPEN：之后一根未触及 sl 也未到 tp
        candles_open = [sig_candle, mk(2000, 100, 110, 95, 105)]
        self.assertEqual(app_mod.evaluate_signal(0, pattern, candles_open)[0], "OPEN")

        # 同根既触 sl 又触 tp：保守判 LOSS
        candles_both = [sig_candle, mk(2000, 100, 121, 89, 100)]
        self.assertEqual(app_mod.evaluate_signal(0, pattern, candles_both)[0], "LOSS")

    def test_order_mock_success(self):
        """凭证缺失（get_trade_client 返回 None）时自动降级 mock，返回 mock=true。"""
        with mock.patch.object(app_mod, "get_trade_client", return_value=(None, "缺少 OKX_API_KEY")):
            resp = self.client.post("/api/order", json={
                "action": "BUY", "ticker": "BTC-USDT-SWAP",
                "price": 100.5, "sl": 99.0, "tp": 103.5, "size": 0.01,
            })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["mock"])
        self.assertEqual(data["mock_reason"], "缺少 OKX_API_KEY")
        self.assertIn("order_id", data)

    def test_order_real_simulated_submission(self):
        """配置凭证后 /api/order 走 OKXTradingClient 真实提交（mock 传输层验证请求体）。"""
        import httpx as _httpx
        from webhook_server.okx_trading import OKXTradingClient

        captured = {}

        def handler(request: _httpx.Request) -> _httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return _httpx.Response(200, json={
                "code": "0", "msg": "",
                "data": [{"ordId": "123456789", "sCode": "0", "sMsg": ""}],
            }, request=request)

        real = OKXTradingClient(api_key="K", secret="S", passphrase="P", simulated=True)
        real._client = _httpx.Client(transport=_httpx.MockTransport(handler), timeout=5)
        app_mod._trade_client = real
        app_mod._trade_client_error = None
        try:
            resp = self.client.post("/api/order", json={
                "action": "BUY", "ticker": "BTC-USDT-SWAP",
                "price": 100.5, "sl": 99.0, "tp": 103.5,
            })
        finally:
            app_mod._trade_client = None

        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.get_json()
        self.assertFalse(data["mock"])                 # 真实提交而非 mock
        self.assertEqual(data["order_id"], "123456789")
        # 请求发往 OKX 下单端点且带模拟盘头（httpx 头名会转小写，用大小写不敏感比较）
        self.assertIn("/api/v5/trade/order", captured["url"])
        headers_lc = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers_lc["x-simulated-trading"], "1")
        for h in ("ok-access-key", "ok-access-sign", "ok-access-timestamp", "ok-access-passphrase"):
            self.assertIn(h, headers_lc)
        # 请求体：限价单 + posSide 持仓方向 + attachAlgoOrds 附带止盈止损
        body = captured["body"]
        self.assertEqual(body["instId"], "BTC-USDT-SWAP")
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["ordType"], "limit")
        self.assertEqual(body["sz"], "0.01")
        self.assertEqual(body["px"], "100.5")
        self.assertEqual(body["posSide"], "long")   # BUY -> long
        self.assertEqual(body["attachAlgoOrds"][0]["tpTriggerPx"], "103.5")
        self.assertEqual(body["attachAlgoOrds"][0]["slTriggerPx"], "99.0")

    def test_order_okx_rejection_returns_502(self):
        """OKX 拒单（sCode!=0）时接口返回 502 与错误信息，而非伪装成功。"""
        import httpx as _httpx
        from webhook_server.okx_trading import OKXTradingClient

        def handler(request: _httpx.Request) -> _httpx.Response:
            return _httpx.Response(200, json={
                "code": "0", "msg": "",
                "data": [{"sCode": "51008", "sMsg": "Order price is not in range"}],
            }, request=request)

        real = OKXTradingClient(api_key="K", secret="S", passphrase="P", simulated=True)
        real._client = _httpx.Client(transport=_httpx.MockTransport(handler), timeout=5)
        app_mod._trade_client = real
        app_mod._trade_client_error = None
        try:
            resp = self.client.post("/api/order", json={
                "action": "BUY", "ticker": "BTC-USDT-SWAP",
                "price": 100.5, "sl": 99.0, "tp": 103.5,
            })
        finally:
            app_mod._trade_client = None

        self.assertEqual(resp.status_code, 502)
        self.assertIn("51008", resp.get_json()["error"])

    def test_order_price_relation_validated(self):
        """价格关系校验：BUY 要求 sl < price < tp。"""
        resp = self.client.post("/api/order", json={
            "action": "BUY", "ticker": "BTC-USDT-SWAP",
            "price": 100.0, "sl": 110.0, "tp": 120.0,
        })
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/order", json={
            "action": "SELL", "ticker": "BTC-USDT-SWAP",
            "price": 100.0, "sl": 90.0, "tp": 110.0,
        })
        self.assertEqual(resp.status_code, 400)

    def test_order_invalid_action(self):
        resp = self.client.post("/api/order", json={
            "action": "HOLD", "ticker": "BTC-USDT-SWAP", "size": 0.01,
        })
        self.assertEqual(resp.status_code, 400)

    def test_order_invalid_ticker(self):
        resp = self.client.post("/api/order", json={
            "action": "SELL", "ticker": "ETH-USDT-SWAP", "size": 0.01,
        })
        self.assertEqual(resp.status_code, 400)

    def test_order_invalid_size(self):
        resp = self.client.post("/api/order", json={
            "action": "BUY", "ticker": "BTC-USDT-SWAP", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)

    def test_index_page_served(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("lightweight-charts", resp.get_data(as_text=True))

    def test_signals_trend_filter(self):
        """开启趋势过滤后，只保留顺势信号（整体上升趋势只留 BUY）。"""
        from okx_client import Candle

        def mk(ts, o, h, l, c):
            return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=1.0)

        # 构造 12 根整体上升的 K 线；插入一根看跌Pinbar(逆势SELL) 与一根看涨Pinbar(顺势BUY)
        candles = [
            mk(1000 + i * 300000, 100 + i, 101 + i, 99 + i, 100 + i + 0.5)
            for i in range(6)
        ]
        # 看跌 Pinbar：上影线长、收盘靠底（逆势，趋势过滤应删除）
        candles.append(mk(1000 + 6 * 300000, 108, 120, 106, 107.5))   # bearish_pinbar
        candles.append(mk(1000 + 7 * 300000, 109, 110, 108, 109.5))
        # 看涨 Pinbar：下影线长、收盘靠顶（顺势，应保留）
        candles.append(mk(1000 + 8 * 300000, 110, 111, 98, 110.5))    # bullish_pinbar
        candles.append(mk(1000 + 9 * 300000, 111, 112, 110, 111.5))

        with mock.patch.object(app_mod.OKXClient, "get_candles", return_value=candles):
            no_trend = app_mod.detect_signals("BTC-USDT-SWAP", "5m", 100, trend_period=0)
            with_trend = app_mod.detect_signals("BTC-USDT-SWAP", "5m", 100, trend_period=3)

        actions_no = {s["action"] for s in no_trend}
        actions_with = {s["action"] for s in with_trend}
        # 无过滤时存在 BUY 和 SELL
        self.assertIn("BUY", actions_no)
        self.assertIn("SELL", actions_no)
        # 上升趋势过滤后不应再出现逆势的 SELL
        self.assertNotIn("SELL", actions_with)
        self.assertIn("BUY", actions_with)

    def test_klines_include_volume(self):
        """K 线接口应包含成交量，供图表绘制量柱。"""
        fake = [
            {"time": 1700000000, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 123.5},
            {"time": 1700000300, "open": 101, "high": 103, "low": 100, "close": 102, "volume": 88.0},
        ]
        with mock.patch.object(app_mod, "fetch_klines", return_value=fake):
            resp = self.client.get("/api/klines?ticker=BTC-USDT-SWAP&interval=5m&limit=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual([b["volume"] for b in data], [123.5, 88.0])

    def test_detect_signals_volume_disabled_by_default(self):
        """默认不启用量能：不附加量能字段、也不过滤缩量信号。"""
        from okx_client import Candle

        def mk(ts, o, h, l, c, v):
            return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)

        # 看涨 Pinbar（下影线长、收盘靠顶），量能极小（缩量）
        candles = [mk(1000 + i * 300000, 100, 101, 99, 100.5, 1.0) for i in range(25)]
        candles[-1] = mk(1000 + 24 * 300000, 100, 101, 90, 100.5, 0.01)

        with mock.patch.object(app_mod.OKXClient, "get_candles", return_value=candles):
            signals = app_mod.detect_signals("BTC-USDT-SWAP", "5m", 100, scan_window=1)
        self.assertEqual(len(signals), 1)          # 缩量信号默认保留
        self.assertNotIn("volume_ratio", signals[0])

    def test_detect_signals_volume_enabled(self):
        """启用量能后：附加量能字段；缩量+低盈亏比信号被过滤。"""
        from okx_client import Candle
        from types import SimpleNamespace
        from volume import should_filter

        def mk(ts, o, h, l, c, v):
            return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)

        # 看涨 Pinbar，缩量（0.01 远小于均量 1）
        candles = [mk(1000 + i * 300000, 100, 101, 99, 100.5, 1.0) for i in range(25)]
        candles[-1] = mk(1000 + 24 * 300000, 100, 101, 90, 100.5, 0.01)

        with mock.patch.object(app_mod.OKXClient, "get_candles", return_value=candles):
            signals = app_mod.detect_signals("BTC-USDT-SWAP", "5m", 100,
                                             scan_window=1, volume_enabled=True)
        # 固定盈亏比 2:1 下 rr<2 恒不成立，信号保留但带量能信息
        self.assertEqual(len(signals), 1)
        self.assertLess(signals[0]["volume_ratio"], 0.8)
        self.assertEqual(signals[0]["volume_signal"], "weak")
        self.assertEqual(signals[0]["volume_confirm"], "缩量Pinbar")
        self.assertTrue(should_filter(signals[0]["volume_ratio"], 1.5))

        # 低盈亏比形态在启用量能时会被过滤（用 SimpleNamespace 验证 should_filter 分支）
        self.assertTrue(should_filter(0.5, 1.5))
        self.assertFalse(should_filter(0.5, 2.0))

    def test_api_signals_volume_param(self):
        """/api/signals 的 volume=1 应透传 volume_enabled，非法值返回 400。"""
        captured = {}
        def fake_detect(ticker, interval, limit, **kwargs):
            captured.update(kwargs)
            return []
        with mock.patch.object(app_mod, "detect_signals", side_effect=fake_detect):
            self.client.get("/api/signals?ticker=BTC-USDT-SWAP&volume=1")
        self.assertTrue(captured.get("volume_enabled"))
        captured.clear()
        with mock.patch.object(app_mod, "detect_signals", side_effect=fake_detect):
            self.client.get("/api/signals?ticker=BTC-USDT-SWAP&volume=0")
        self.assertNotIn("volume_enabled", captured)
        resp = self.client.get("/api/signals?ticker=BTC-USDT-SWAP&volume=yes")
        self.assertEqual(resp.status_code, 400)

    def test_api_config_get(self):
        """GET /api/config 返回脱敏配置与凭证状态，不泄露明文。"""
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for k in ("configured", "proxy", "simulated", "api_key_masked",
                  "secret_masked", "passphrase_masked"):
            self.assertIn(k, data)
        # 不应返回明文凭证
        self.assertNotIn("OKX_API_KEY", data)
        self.assertNotIn("OKX_API_SECRET", data)

    def test_api_config_save_and_reset(self):
        """POST /api/config 保存配置并热更新（不真正写盘，避免污染 .env）。"""
        import os
        old_proxy = os.environ.get("OKX_PROXY")
        with mock.patch.object(app_mod, "_save_env_file") as mock_save:
            resp = self.client.post("/api/config", json={"OKX_PROXY": "http://127.0.0.1:1234"})
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.get_json()
            self.assertEqual(data["status"], "ok")
            self.assertIn("OKX_PROXY", data["saved"])
            # 确认写盘函数被调用且传入了新值
            self.assertTrue(mock_save.called)
            self.assertEqual(mock_save.call_args[0][0]["OKX_PROXY"], "http://127.0.0.1:1234")
        # 复位环境变量，避免污染其他用例
        app_mod.reset_trade_client()
        if old_proxy is None:
            os.environ.pop("OKX_PROXY", None)
        else:
            os.environ["OKX_PROXY"] = old_proxy

    def test_api_signals_trend_param(self):
        """/api/signals 的 trend/ma_type/scan 参数应传递给 detect_signals。"""
        captured = {}
        def fake_detect(ticker, interval, limit, trend_period=None,
                        ma_type=None, scan_window=None, **_):
            captured["trend"] = trend_period
            captured["ma"] = ma_type
            captured["scan"] = scan_window
            return []
        with mock.patch.object(app_mod, "detect_signals", side_effect=fake_detect):
            self.client.get("/api/signals?ticker=BTC-USDT-SWAP&trend=50&ma_type=ema&scan=80")
        self.assertEqual(captured["trend"], 50)
        self.assertEqual(captured["ma"], "ema")
        self.assertEqual(captured["scan"], 80)


if __name__ == "__main__":
    unittest.main()
