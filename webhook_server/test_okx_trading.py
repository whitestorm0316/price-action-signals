"""OKX 交易客户端与 Webhook 服务的单元测试（不连接真实网络）。

通过注入 mock HTTP 传输层，验证请求构造、签名头、模拟盘标识、响应解析。
"""
from __future__ import annotations

import json
import unittest

import httpx
from fastapi.testclient import TestClient

from webhook_server import app as app_mod
from webhook_server.okx_trading import OKXTradingClient


class MockTransport(httpx.MockTransport):
    """拦截请求并回放预置响应，同时记录最后一次请求，便于断言。"""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.last_request: httpx.Request | None = None
        self.status_code = status_code
        self.payload = payload or {
            "code": "0",
            "msg": "",
            "data": [{"clOrdId": "tv-test", "ordId": "123456789", "sCode": "0", "sMsg": ""}],
        }
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return httpx.Response(
            self.status_code,
            json=self.payload,
            request=request,
        )


def _make_client(transport: httpx.MockTransport) -> OKXTradingClient:
    c = OKXTradingClient(api_key="TESTKEY", secret="TESTSECRET", passphrase="TESTPASS", simulated=True)
    c._client = httpx.Client(transport=transport, timeout=5)
    return c


class TestOKXTradingClient(unittest.TestCase):
    def test_limit_order_request_construction(self):
        transport = MockTransport()
        c = _make_client(transport)
        order = c.place_limit_order(
            inst_id="BTC-USDT-SWAP", side="buy", sz="0.01", px="100.5",
            take_profit=120.0, stop_loss=90.0, cl_ord_id="tv-abc",
        )

        req = transport.last_request
        # 路径与方法
        self.assertEqual(req.url.path, "/api/v5/trade/order")
        self.assertEqual(req.method, "POST")

        # 模拟盘标识 + 签名头必须存在
        self.assertEqual(req.headers["x-simulated-trading"], "1")
        for h in ("OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-TIMESTAMP", "OK-ACCESS-PASSPHRASE"):
            self.assertIn(h, req.headers)

        # body 字段
        body = json.loads(req.content)
        self.assertEqual(body["instId"], "BTC-USDT-SWAP")
        self.assertEqual(body["tdMode"], "cross")
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["ordType"], "limit")
        self.assertEqual(body["sz"], "0.01")
        self.assertEqual(body["px"], "100.5")
        # 止盈止损附带
        algo = body["attachAlgoOrds"][0]
        self.assertEqual(algo["tpTriggerPx"], "120.0")
        self.assertEqual(algo["tpOrdPx"], "-1")
        self.assertEqual(algo["slTriggerPx"], "90.0")
        self.assertEqual(algo["slOrdPx"], "-1")

        # 成功响应解析出 ordId
        self.assertEqual(order["ordId"], "123456789")

    def test_simulated_flag_off(self):
        # 当 simulated=False 时不应带 x-simulated-trading 头
        transport = MockTransport()
        c = OKXTradingClient(api_key="K", secret="S", passphrase="P", simulated=False)
        c._client = httpx.Client(transport=transport, timeout=5)
        c.place_limit_order(inst_id="BTC-USDT-SWAP", side="sell", sz="0.01", px="100")
        self.assertNotIn("x-simulated-trading", transport.last_request.headers)

    def test_order_rejected_raises(self):
        transport = MockTransport(
            payload={"code": "0", "msg": "", "data": [{"sCode": "51008", "sMsg": "订单被拒"}]}
        )
        c = _make_client(transport)
        from webhook_server.okx_trading import OKXTradeError
        with self.assertRaises(OKXTradeError) as ctx:
            c.place_limit_order(inst_id="BTC-USDT-SWAP", side="buy", sz="0.01", px="100")
        self.assertIn("51008", str(ctx.exception))

    def test_api_error_code_raises(self):
        transport = MockTransport(payload={"code": "50111", "msg": "限频"})
        c = _make_client(transport)
        from webhook_server.okx_trading import OKXTradeError
        with self.assertRaises(OKXTradeError):
            c.place_limit_order(inst_id="BTC-USDT-SWAP", side="buy", sz="0.01", px="100")

    def test_all_operations_failed_surfaces_detail(self):
        """顶层 code=1 失败时，能提取 data[0] 里更具体的 sCode/sMsg（如 51000 posSide error）。"""
        transport = MockTransport(payload={
            "code": "1",
            "msg": "All operations failed",
            "data": [{"clOrdId": "", "ordId": "", "sCode": "51000",
                      "sMsg": "Parameter posSide error", "ts": "1789713034581"}],
        })
        c = _make_client(transport)
        from webhook_server.okx_trading import OKXTradeError
        with self.assertRaises(OKXTradeError) as ctx:
            c.place_limit_order(inst_id="BTC-USDT-SWAP", side="buy", sz="0.01", px="100")
        self.assertIn("51000", str(ctx.exception))
        self.assertIn("posSide", str(ctx.exception))

    def test_pos_side_included_in_body(self):
        """传入 pos_side 时，下单请求体包含 posSide 字段。"""
        transport = MockTransport()
        c = _make_client(transport)
        c.place_limit_order(inst_id="BTC-USDT-SWAP", side="buy", sz="0.01", px="100",
                            pos_side="long")
        body = json.loads(transport.last_request.content)
        self.assertEqual(body["posSide"], "long")

    def test_http_401_permission_error_extracts_code(self):
        """HTTP 401 时能从响应体提取 OKX 错误码/信息（如 50120 权限不足）。"""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={
                "msg": "This API key doesn't have permission to use this function",
                "code": "50120",
            }, request=request)

        transport = httpx.MockTransport(handler)
        c = _make_client(transport)
        from webhook_server.okx_trading import OKXTradeError
        with self.assertRaises(OKXTradeError) as ctx:
            c.place_limit_order(inst_id="BTC-USDT-SWAP", side="buy", sz="0.01", px="100")
        self.assertIn("50120", str(ctx.exception))
        self.assertIn("permission", str(ctx.exception))


class TestWebhookApp(unittest.TestCase):
    def setUp(self):
        # 临时数据库，避免污染
        import tempfile
        import os
        self._tmp = tempfile.mkdtemp()
        app_mod._store = app_mod.OrderStore(os.path.join(self._tmp, "test.db"))
        # 注入一个成功响应的 mock 客户端
        transport = MockTransport()
        mock_client = _make_client(transport)
        app_mod._client = mock_client
        self.transport = transport
        self.client = TestClient(app_mod.app)

    def test_valid_buy_signal_submits(self):
        resp = self.client.post("/webhook", json={
            "action": "BUY", "ticker": "BTC-USDT-SWAP",
            "price": 100.0, "sl": 90.0, "tp": 120.0, "strategy": "bullish_pinbar",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["okx_ord_id"], "123456789")
        self.assertIn("record_id", data)

        # 已写入 SQLite
        rows = app_mod._store.list_recent(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "submitted")
        self.assertEqual(rows[0]["okx_ord_id"], "123456789")

    def test_invalid_price_relation_rejected(self):
        resp = self.client.post("/webhook", json={
            "action": "BUY", "ticker": "BTC-USDT-SWAP",
            "price": 100.0, "sl": 110.0, "tp": 120.0, "strategy": "x",
        })
        self.assertEqual(resp.status_code, 400)

    def test_wrong_ticker_rejected(self):
        resp = self.client.post("/webhook", json={
            "action": "SELL", "ticker": "ETH-USDT-SWAP",
            "price": 100.0, "sl": 110.0, "tp": 90.0, "strategy": "x",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("BTC-USDT-SWAP", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
