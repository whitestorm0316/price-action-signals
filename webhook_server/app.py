"""TradingView Webhook 接收服务 —— 自动在 OKX 模拟盘下单。

接收 TradingView Alert Webhook 发来的固定 JSON:
    {action, ticker, price, sl, tp, strategy}
并在 OKX 模拟盘(Demo)下限价单(0.01 张 BTC-USDT-SWAP)并附带止盈止损。

运行:
    export OKX_API_KEY=xxx
    export OKX_API_SECRET=xxx
    export OKX_PASSPHRASE=xxx
    uvicorn webhook_server.app:app --host 0.0.0.0 --port 9000

安全: 绝不使用真实账户 —— 客户端固定带 x-simulated-trading: 1。
"""
from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .okx_trading import OKXConfigError, OKXTradeError, OKXTradingClient
from .storage import OrderStore

app = FastAPI(title="OKX 模拟盘 Webhook 信号执行器")

# 默认交易对与张数（用户要求：先只支持 BTC-USDT-SWAP，固定 0.01 张）
DEFAULT_INST_ID = "BTC-USDT-SWAP"
DEFAULT_SZ = "0.01"
# 交易模式：cross(全仓) —— 模拟盘单货币保证金的合约，用 cross 最通用
TD_MODE = os.getenv("OKX_TD_MODE", "cross")

# 记录存到 webhook_server/orders.db
DB_PATH = os.getenv(
    "OKX_ORDERS_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db"),
)

_store = OrderStore(DB_PATH)
_client: OKXTradingClient | None = None


def get_client() -> OKXTradingClient:
    """惰性创建 OKX 客户端（读取环境变量凭证）。"""
    global _client
    if _client is None:
        _client = OKXTradingClient(simulated=True)
    return _client


class SignalPayload(BaseModel):
    """TradingView Webhook 消息体（字段全部必填）。"""

    action: str = Field(..., pattern="^(BUY|SELL)$", description="BUY 或 SELL")
    ticker: str = Field(..., min_length=1)
    price: float = Field(..., gt=0)
    sl: float = Field(..., gt=0)
    tp: float = Field(..., gt=0)
    strategy: str = Field("", description="形态名称，可空")


def _validate_signal(p: SignalPayload) -> None:
    """业务校验，避免非法值进入交易所。"""
    if p.ticker != DEFAULT_INST_ID:
        raise HTTPException(
            status_code=400,
            detail=f"暂只支持 {DEFAULT_INST_ID}，收到 {p.ticker}",
        )
    if p.action == "BUY":
        if not (p.sl < p.price < p.tp):
            raise HTTPException(
                status_code=400,
                detail="BUY 信号应满足 sl < price < tp",
            )
    else:  # SELL
        if not (p.tp < p.price < p.sl):
            raise HTTPException(
                status_code=400,
                detail="SELL 信号应满足 tp < price < sl",
            )


@app.post("/webhook")
async def webhook(payload: SignalPayload, request: Request) -> dict:
    """接收 TradingView Webhook 并在 OKX 模拟盘下单。"""
    _validate_signal(payload)

    cl_ord_id = f"tv-{uuid.uuid4().hex[:20]}"
    raw_request = {
        "instId": payload.ticker,
        "tdMode": TD_MODE,
        "side": payload.action.lower(),
        "ordType": "limit",
        "sz": DEFAULT_SZ,
        "px": str(payload.price),
        "attachAlgoOrds": [
            {
                "tpTriggerPx": str(payload.tp),
                "tpOrdPx": "-1",
                "slTriggerPx": str(payload.sl),
                "slOrdPx": "-1",
            }
        ],
    }

    try:
        client = get_client()
        order = client.place_limit_order(
            inst_id=payload.ticker,
            side=payload.action.lower(),
            sz=DEFAULT_SZ,
            px=str(payload.price),
            td_mode=TD_MODE,
            take_profit=payload.tp,
            stop_loss=payload.sl,
            cl_ord_id=cl_ord_id,
        )
    except (OKXConfigError, OKXTradeError) as exc:
        # 记录失败，便于排查
        row_id = _store.record_order(
            action=payload.action,
            ticker=payload.ticker,
            price=str(payload.price),
            sl=str(payload.sl),
            tp=str(payload.tp),
            strategy=payload.strategy,
            status="error",
            error=str(exc),
            raw_request=raw_request,
        )
        raise HTTPException(status_code=502, detail=f"OKX 下单失败: {exc} (record #{row_id})")

    row_id = _store.record_order(
        action=payload.action,
        ticker=payload.ticker,
        price=str(payload.price),
        sl=str(payload.sl),
        tp=str(payload.tp),
        strategy=payload.strategy,
        okx_ord_id=order.get("ordId"),
        okx_cl_ord_id=order.get("clOrdId"),
        okx_s_code=order.get("sCode"),
        okx_s_msg=order.get("sMsg"),
        status="submitted",
        raw_request=raw_request,
        raw_response=order,
    )

    return {
        "status": "ok",
        "record_id": row_id,
        "okx_ord_id": order.get("ordId"),
        "message": f"已在 OKX 模拟盘提交 {payload.action} 限价单 {DEFAULT_SZ} 张 {payload.ticker}",
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "up", "simulated": True, "inst_id": DEFAULT_INST_ID, "sz": DEFAULT_SZ}


@app.get("/orders")
async def orders(limit: int = 20) -> dict:
    """查看最近的下单记录（本地 SQLite）。"""
    return {"orders": _store.list_recent(limit=limit)}
