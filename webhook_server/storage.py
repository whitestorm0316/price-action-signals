"""本地订单记录存储（SQLite）。

保存每次 Webhook 触发与下单结果，方便后续查看/统计。
用标准库 sqlite3，无需额外依赖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager, closing
from typing import Any, Iterator, Optional


class OrderStore:
    """把订单记录写入本地 SQLite 文件。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # sqlite3 连接不能跨线程共享，用锁 + 每次操作开新连接
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """带锁、自动提交、确保关闭的数据库会话。"""
        with self._lock:
            with closing(self._connect()) as conn:
                with conn:
                    yield conn

    def _init_db(self) -> None:
        with self._session() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at    TEXT NOT NULL,
                    action        TEXT NOT NULL,
                    ticker        TEXT NOT NULL,
                    price         TEXT NOT NULL,
                    sl            TEXT,
                    tp            TEXT,
                    strategy      TEXT,
                    okx_ord_id    TEXT,
                    okx_cl_ord_id TEXT,
                    okx_s_code    TEXT,
                    okx_s_msg     TEXT,
                    status        TEXT NOT NULL,   -- submitted / error
                    error         TEXT,
                    raw_request   TEXT,
                    raw_response  TEXT
                )
                """
            )

    def record_order(
        self,
        *,
        action: str,
        ticker: str,
        price: str,
        sl: Optional[str],
        tp: Optional[str],
        strategy: Optional[str],
        okx_ord_id: Optional[str] = None,
        okx_cl_ord_id: Optional[str] = None,
        okx_s_code: Optional[str] = None,
        okx_s_msg: Optional[str] = None,
        status: str = "submitted",
        error: Optional[str] = None,
        raw_request: Optional[dict] = None,
        raw_response: Optional[dict] = None,
    ) -> int:
        """写入一条订单记录，返回自增 id。"""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._session() as conn:
            cur = conn.execute(
                """
                INSERT INTO orders (
                    created_at, action, ticker, price, sl, tp, strategy,
                    okx_ord_id, okx_cl_ord_id, okx_s_code, okx_s_msg,
                    status, error, raw_request, raw_response
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now, action, ticker, price, sl, tp, strategy,
                    okx_ord_id, okx_cl_ord_id, okx_s_code, okx_s_msg,
                    status, error,
                    json.dumps(raw_request, ensure_ascii=False) if raw_request else None,
                    json.dumps(raw_response, ensure_ascii=False) if raw_response else None,
                ),
            )
            return int(cur.lastrowid)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """按时间倒序返回最近 limit 条记录（查看用）。"""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
