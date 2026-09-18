"""OKX V5 交易客户端 —— 模拟盘限价下单 + 附带止盈止损。

仅用于 OKX 模拟盘（Demo Trading），请求头带 x-simulated-trading: 1，
绝不触碰真实账户。API Key / Secret / Passphrase 从环境变量读取，不硬编码。

接口规格（来源: OKX V5 官方文档）:
  下单(限价单, 可附带止盈止损):  POST /api/v5/trade/order
    必需参数:
      instId   String  交易对, 如 BTC-USDT-SWAP
      tdMode   String  交易模式: cross(全仓) / isolated(逐仓)
      side     String  买卖方向: buy / sell
      ordType  String  订单类型: limit(限价单)
      sz       String  委托数量(张)
    条件价格:
      px       String  限价单价格
    止盈止损(attachAlgoOrds, 数组, 可省略):
      attachAlgoOrds[].tpTriggerPx  止盈触发价
      attachAlgoOrds[].tpOrdPx      止盈委托价 (-1 = 按市价)
      attachAlgoOrds[].slTriggerPx  止损触发价
      attachAlgoOrds[].slOrdPx      止损委托价 (-1 = 按市价)
      attachAlgoOrds[].tpTriggerPxType / slTriggerPxType  默认 last
  响应: { "code": "0", "msg": "", "data": [{"clOrdId","ordId","sCode","sMsg"}] }

签名方式(每个私有请求都要):
  timestamp = ISO8601 UTC, 如 2026-09-17T03:04:05.000Z
  OK-ACCESS-SIGN = Base64( HMAC_SHA256(timestamp + method + requestPath + body, secretKey) )
  requestPath 含 query 串(GET 才有), body 为 POST 的 JSON 字符串

模拟盘: 请求头加 x-simulated-trading: 1
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

# 模拟盘使用与实盘相同的主机，仅靠 x-simulated-trading: 1 头区分（OKX 官方约定）
OKX_BASE = os.getenv("OKX_BASE", "https://www.okx.com")
ORDER_PATH = "/api/v5/trade/order"      # 下单（限价 + 可附带止盈止损）


class OKXConfigError(RuntimeError):
    """API 凭证配置缺失或非法。"""


class OKXTradeError(RuntimeError):
    """OKX 下单失败。"""


def _env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise OKXConfigError(
            f"缺少环境变量 {key}。请先 export {key}=... (模拟盘 API Key 的对应项)"
        )
    return v


class OKXTradingClient:
    """OKX 模拟盘交易客户端（签名 + 下单）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        simulated: bool = True,
        timeout: float = 10.0,
        proxy: Optional[str] = None,
    ) -> None:
        # 优先用传入参数，否则读环境变量
        self._api_key = api_key or _env("OKX_API_KEY")
        self._secret = secret or _env("OKX_API_SECRET")
        self._passphrase = passphrase or _env("OKX_PASSPHRASE")
        self._simulated = simulated
        self._timeout = timeout
        # 支持走代理（OKX 主站被墙时必需），与行情客户端口径一致
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=True, proxy=proxy or None,
        )

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------
    @staticmethod
    def _iso_timestamp() -> str:
        """OKX 需要 ISO8601 UTC 毫秒时间戳，如 2026-09-17T03:04:05.000Z。"""
        return (
            datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z"
        )

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = timestamp + method + request_path + body
        mac = hmac.new(
            self._secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        ts = self._iso_timestamp()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
        }
        if self._simulated:
            # 模拟盘关键标识，保证只在演示环境成交
            headers["x-simulated-trading"] = "1"
        return headers

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------
    def place_limit_order(
        self,
        inst_id: str,
        side: str,
        sz: str,
        px: str,
        td_mode: str = "cross",
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        cl_ord_id: Optional[str] = None,
        pos_side: Optional[str] = None,
    ) -> dict[str, Any]:
        """模拟盘下限价单，可选附带止盈止损（attachAlgoOrds）。

        Args:
            inst_id: 交易对，如 BTC-USDT-SWAP
            side: buy / sell
            sz: 委托数量（张）
            px: 限价单价格
            td_mode: 交易模式 cross / isolated
            take_profit: 止盈价（tp），None 则不挂止盈
            stop_loss: 止损价（sl），None 则不挂止损
            cl_ord_id: 客户端订单 ID（可选）
            pos_side: 持仓方向，long / short。U本位永续合约(BTC-USDT-SWAP)
                下单必需；不开仓方向过滤时可省略

        Returns:
            OKX 响应 data[0] 字典，含 ordId / clOrdId / sCode / sMsg

        Raises:
            OKXTradeError: 网络或交易所返回失败
        """
        body_obj: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "limit",
            "sz": str(sz),
            "px": str(px),
        }
        if pos_side:
            body_obj["posSide"] = pos_side
        if cl_ord_id:
            body_obj["clOrdId"] = cl_ord_id
        if take_profit is not None or stop_loss is not None:
            algo = {}
            if take_profit is not None:
                algo["tpTriggerPx"] = str(take_profit)
                algo["tpOrdPx"] = "-1"  # 触发后按市价平仓
            if stop_loss is not None:
                algo["slTriggerPx"] = str(stop_loss)
                algo["slOrdPx"] = "-1"  # 触发后按市价平仓
            body_obj["attachAlgoOrds"] = [algo]

        body_str = json.dumps(body_obj)
        resp = self._client.post(
            f"{OKX_BASE}{ORDER_PATH}",
            headers=self._headers("POST", ORDER_PATH, body_str),
            content=body_str,
        )
        payload = self._parse_response(resp, ORDER_PATH, body_obj)
        return payload

    # ------------------------------------------------------------------
    # 账户查询（余额 / 仓位 / 订单历史）
    # ------------------------------------------------------------------
    def _request_private_get(self, path: str, params: dict | None = None) -> list[dict]:
        """发起带签名的私有 GET 请求。

        GET 请求签名时 requestPath 必须包含 query 串（OKX 约定），body 为空串。
        """
        query = ""
        if params:
            # 过滤空值，构建有序 query 串
            kv = [f"{k}={v}" for k, v in params.items() if v is not None and v != ""]
            if kv:
                query = "?" + "&".join(kv)
        request_path = path + query
        resp = self._client.get(
            f"{OKX_BASE}{request_path}",
            headers=self._headers("GET", request_path, ""),
        )
        # 复用 _parse_response：对 GET 只返回 data 数组
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OKXTradeError(f"HTTP {exc.response.status_code} 请求 {path} 失败") from exc
        payload = resp.json()
        if payload.get("code") != "0":
            detail = ""
            data = payload.get("data") or []
            if data and isinstance(data[0], dict):
                d0 = data[0]
                if d0.get("sCode") or d0.get("sMsg"):
                    detail = f" [{d0.get('sCode')}] {d0.get('sMsg')}"
            raise OKXTradeError(
                f"OKX 返回错误码 {payload.get('code')}: {payload.get('msg')}{detail}"
            )
        return payload.get("data") or []

    def get_balance(self) -> list[dict]:
        """获取账户余额。返回 data 数组，每项含 details[].ccy/availEq 等。"""
        return self._request_private_get("/api/v5/account/balance")

    def get_positions(self, inst_id: str | None = None) -> list[dict]:
        """获取当前持仓（永续/交割合约）。

        Args:
            inst_id: 交易对，如 BTC-USDT-SWAP；None 返回所有持仓

        Returns:
            data 数组，每项含 posSide / pos / avgPx / upl / markPx / liqPx 等
        """
        return self._request_private_get("/api/v5/account/positions", {"instId": inst_id})

    def get_order_history(
        self,
        inst_id: str,
        ord_type: str = "limit",
        state: str = "filled",
        limit: int = 20,
    ) -> list[dict]:
        """获取已成交订单历史。

        Args:
            inst_id: 交易对
            ord_type: 订单类型，limit 限价单
            state: filled 已成交 / canceled 已撤销 / live 挂单中
            limit: 返回条数，最多 100

        Returns:
            data 数组，每项含 ordId / side / px / sz / fillPx / fee / state 等
        """
        return self._request_private_get(
            "/api/v5/trade/orders-history-archive",
            {
                "instId": inst_id,
                "instType": "SWAP",   # BTC-USDT-SWAP 为 U本位永续合约，必需
                "ordType": ord_type,
                "state": state,
                "limit": limit,
            },
        )

    def get_pending_orders(self, inst_id: str, limit: int = 20) -> list[dict]:
        """获取当前挂单（未成交）。

        Args:
            inst_id: 交易对

        Returns:
            data 数组，每项含 ordId / side / px / sz / state 等
        """
        return self._request_private_get(
            "/api/v5/trade/orders-pending", {"instId": inst_id, "limit": limit}
        )

    # ------------------------------------------------------------------
    # 公共工具
    # ------------------------------------------------------------------
    def _parse_response(self, resp: httpx.Response, path: str, body_obj: dict) -> dict[str, Any]:
        """解析 OKX 响应，code 非 0 或 sCode 非 0 时抛错。"""
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 尽量提取 OKX 响应体里的错误码/信息，方便定位（如 50120 权限不足）
            detail = ""
            try:
                err = exc.response.json()
                if err.get("code") or err.get("msg"):
                    detail = f" [{err.get('code')}] {err.get('msg')}".strip()
            except Exception:  # noqa: BLE001
                detail = ""
            raise OKXTradeError(
                f"HTTP {exc.response.status_code} 请求 {path} 失败{detail}"
            ) from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise OKXTradeError(f"OKX 返回非 JSON: {resp.text[:300]}") from exc

        if payload.get("code") != "0":
            # 顶层失败时，优先取 data[0] 里更具体的 sCode/sMsg（如 51000 posSide error）
            data = payload.get("data") or []
            detail = ""
            if data and isinstance(data[0], dict):
                d0 = data[0]
                if d0.get("sCode") or d0.get("sMsg"):
                    detail = f" [{d0.get('sCode')}] {d0.get('sMsg')}"
            raise OKXTradeError(
                f"OKX 返回错误码 {payload.get('code')}: {payload.get('msg')}{detail}"
            )

        data = payload.get("data") or []
        if not data:
            raise OKXTradeError(f"OKX 未返回订单数据: {payload}")

        order = data[0]
        # 单笔下单 sCode 非 0 表示该笔被拒
        if str(order.get("sCode", "0")) != "0":
            raise OKXTradeError(
                f"订单被拒 sCode={order.get('sCode')} sMsg={order.get('sMsg')} "
                f"(请求体: {body_obj})"
            )
        return order

    def close(self) -> None:
        self._client.close()
