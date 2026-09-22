"""OKX V5 公开 API 客户端 —— 拉取 K 线数据。

仅使用公开接口（market/candles），无需 API Key。
文档参考: https://www.okx.com/docs-v5/en/#rest-api-market-data-get-candlesticks
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

# OKX V5 公开行情端点（按优先级排列，依次尝试，避免单点超时）
# 注意：OKX 主站在部分网络（尤其中国大陆）会被 DNS 污染导致解析失败，
# 此时可通过 OKXClient(host=...) 或 CLI 的 --host 指向可访问的镜像。
OKX_MIRROR_URLS = [
    "https://www.okx.com/api/v5/market/candles",
    "https://aws.okx.com/api/v5/market/candles",
    "https://www.okx.com.cn/api/v5/market/candles",
]

# 历史行情端点：保留上市以来的全部 K 线（单页上限 100 根）。
# 用于长周期回测（如一整年 15m ≈ 35,040 根，远超 /market/candles 的 1440 根保留量）
OKX_HISTORY_URLS = [
    "https://www.okx.com/api/v5/market/history-candles",
    "https://aws.okx.com/api/v5/market/history-candles",
]

# OKX 单次请求上限 300 根；需要更多时用 after 参数向前翻页
PAGE_SIZE = 300
# 历史端点单次请求上限 100 根
HISTORY_PAGE_SIZE = 100
# 自动分页的最大总条数（防止误传超大值导致请求过多）。
# 可用环境变量 OKX_MAX_CANDLES 覆盖（一年 15m≈35040、1m≈525600）
MAX_TOTAL = int(os.getenv("OKX_MAX_CANDLES", "60000"))
# 分页间的停顿秒数，避开公开行情 ~20次/2s 的限频
PAGE_DELAY = 0.25
# 说明：/market/candles 最近行情端点保留的条数有限（随周期不同，
# 5m 约 1440 根、1m 约 1440 根）；分页时若该端点取尽，
# 自动切换到 /market/history-candles 继续向前翻页。

# 默认请求头，模拟浏览器，降低被风控的概率
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


@dataclass(frozen=True)
class Candle:
    """单根 K 线。OKX candles 返回的字段顺序: ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm"""

    ts: int          # 开盘时间戳 (ms)
    open: float
    high: float
    low: float
    close: float
    volume: float    # 成交量 (张/币种数量)

    @property
    def body_top(self) -> float:
        """实体上沿 = max(open, close)"""
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        """实体下沿 = min(open, close)"""
        return min(self.open, self.close)

    @property
    def body(self) -> float:
        """实体长度"""
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        """上影线长度"""
        return self.high - self.body_top

    @property
    def lower_wick(self) -> float:
        """下影线长度"""
        return self.body_bottom - self.low

    @property
    def is_bullish(self) -> bool:
        """阳线: 收盘 >= 开盘"""
        return self.close >= self.open

    @property
    def is_bearish(self) -> bool:
        """阴线: 收盘 < 开盘"""
        return self.close < self.open


class OKXClient:
    """OKX 公开行情客户端，负责拉取并解析 K 线。"""

    def __init__(
        self,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_delay: float = 0.5,
        proxy: Optional[str] = None,
        host: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> None:
        """初始化客户端。

        Args:
            timeout: 单次请求超时（秒）
            max_retries: 每个端点的最大重试次数
            retry_delay: 重试基础间隔（秒）
            proxy: HTTP 代理地址，如 "http://127.0.0.1:7890"。
                不传则默认 trust_env 读系统环境变量代理（HTTP_PROXY 等）。
            host: 自定义数据端点（须为完整 URL 前缀，如
                "https://your-mirror.example.com/api/v5"）。
                传入后仅使用该端点，跳过默认镜像列表。
            cache_dir: 可选磁盘缓存目录。启用后 get_candles(use_cache=True)
                的大批量拉取会与本地历史增量合并持久化，重复回测秒出。
        """
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        # 若指定 host，则只用它；否则遍历默认镜像列表
        self._urls = (
            [host.rstrip("/") + "/market/candles"] if host else list(OKX_MIRROR_URLS)
        )
        self._history_urls = (
            [host.rstrip("/") + "/market/history-candles"]
            if host
            else list(OKX_HISTORY_URLS)
        )
        # 可选磁盘缓存（仅在显式传 cache_dir 时启用）
        self._cache_dir = cache_dir
        self._client = httpx.Client(
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy,
            trust_env=True,  # 未显式给 proxy 时读系统 HTTP(S)_PROXY
        )

    def get_candles(
        self,
        inst_id: str,
        bar: str = "5m",
        limit: int = 300,
        use_cache: bool = False,
    ) -> list[Candle]:
        """拉取 K 线，返回按时间升序排列的列表（最旧在前，最新在后）。

        limit 超过 300 时自动分页（after=最早一根的 ts 向前翻），
        最多 MAX_TOTAL 根（可用环境变量 OKX_MAX_CANDLES 覆盖，默认 60000）。

        分页策略：先走 /market/candles（最近行情，新鲜），
        其保留量取尽后自动切换 /market/history-candles 继续向前翻页，
        因此可拉取任意久远的历史（如一整年的 15m K 线 ≈ 35,040 根）。

        Args:
            inst_id: 交易对，如 "BTC-USDT-SWAP"
            bar: K 线周期，如 "5m"（支持 1m/3m/5m/15m/30m/1H/4H/1D 等）
            limit: 返回条数，>300 自动分页，上限 MAX_TOTAL
            use_cache: 启用磁盘缓存（需构造时传 cache_dir）。
                缓存历史条数足够时直接命中返回（回测重复运行秒出），
                不足时先拉取再与缓存合并落盘。

        Returns:
            升序排列的 Candle 列表。
        """
        total = min(max(limit, 2), MAX_TOTAL)

        # 磁盘缓存命中：历史条数足够时直接返回（重复回测免二次拉取）
        cached = (
            self._cache_load(inst_id, bar)
            if (use_cache and self._cache_dir)
            else None
        )
        if cached is not None and len(cached) >= total:
            return self._rows_to_candles(cached[-total:])

        # OKX 返回降序（最新在前）；ts 去重集合应对 live/history 翻页交界重叠
        rows: list[list] = []
        seen: set[int] = set()
        after: Optional[int] = None  # 翻页游标：取该 ts 之前(更早)的数据
        use_history = False          # False=最近行情端点 → True=历史端点

        while len(rows) < total:
            page_n = min(
                total - len(rows),
                HISTORY_PAGE_SIZE if use_history else PAGE_SIZE,
            )
            params = {
                "instId": inst_id,
                "bar": bar,
                "limit": str(page_n),
            }
            if after is not None:
                params["after"] = str(after)

            urls = self._history_urls if use_history else self._urls
            data = self._request_with_retry(params, urls=urls)
            if not data:
                if use_history:
                    break  # 历史端点也无更多数据（已到上市起点）
                # 最近行情保留量取尽 → 切历史端点继续向前翻
                use_history = True
                time.sleep(PAGE_DELAY)
                continue

            for row in data:
                ts = int(row[0])
                if ts not in seen:
                    seen.add(ts)
                    rows.append(row)

            # 本页最旧一根的 ts 作为下一页游标
            after = int(data[-1][0])
            if len(data) < page_n:
                if use_history:
                    break  # 历史端点已翻到头
                use_history = True
                time.sleep(PAGE_DELAY)
                continue
            if len(rows) < total:
                time.sleep(PAGE_DELAY)  # 翻页限频停顿

        # OKX 返回降序，翻转成升序（"前一根" = 时间上更早的一根）
        rows.reverse()

        # 磁盘缓存：与本地历史合并落盘（写失败不影响返回）
        if self._cache_dir is not None:
            rows = self._cache_merge_save(inst_id, bar, rows)

        rows = rows[-total:]
        return self._rows_to_candles(rows)

    @staticmethod
    def _rows_to_candles(rows: list[list]) -> list[Candle]:
        """把 OKX 原始行 [ts,o,h,l,c,vol,...] 转成升序 Candle 列表。"""
        return [
            Candle(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 磁盘缓存（可选，仅在构造时传入 cache_dir 时生效）
    # ------------------------------------------------------------------
    def _cache_path(self, inst_id: str, bar: str) -> Path:
        return Path(self._cache_dir) / f"candles_{inst_id}_{bar}.json"

    def _cache_load(self, inst_id: str, bar: str) -> Optional[list[list]]:
        """读取缓存（升序 rows）；文件不存在/损坏返回 None。"""
        try:
            rows = json.loads(self._cache_path(inst_id, bar).read_text(encoding="utf-8"))
            if isinstance(rows, list) and rows:
                return rows
        except Exception:  # noqa: BLE001
            pass
        return None

    def _cache_merge_save(self, inst_id: str, bar: str, new_asc: list[list]) -> list[list]:
        """新拉取的升序 rows 与缓存按 ts 去重合并，落盘并返回合并结果。"""
        merged: dict[int, list] = {int(r[0]): r for r in (self._cache_load(inst_id, bar) or [])}
        merged.update({int(r[0]): r for r in new_asc})
        combined = [merged[ts] for ts in sorted(merged)]
        try:
            path = self._cache_path(inst_id, bar)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(combined), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return combined

    def _request_with_retry(
        self, params: dict, urls: Optional[list[str]] = None
    ) -> list[list]:
        """带重试与镜像切换的请求，返回 OKX data 数组。

        依次尝试各个端点，对每个端点最多重试 max_retries 次。
        全部失败时抛出自带诊断信息的 RuntimeError。

        Args:
            params: 请求参数
            urls: 本轮使用的候选端点列表；默认为最近行情端点
        """
        last_err: Optional[Exception] = None
        attempted: list[str] = []

        for url in (urls or self._urls):
            attempted.append(url)
            for attempt in range(self._max_retries):
                resp = None
                try:
                    resp = self._client.get(url, params=params)
                    resp.raise_for_status()
                    payload = resp.json()

                    code = payload.get("code")
                    if code != "0":
                        raise RuntimeError(
                            f"OKX API 返回错误码 {code}: {payload.get('msg')}"
                        )
                    return payload["data"]

                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    # 限频(429)时适当拉长等待
                    delay = self._retry_delay * (2**attempt)
                    if resp is not None and resp.status_code == 429:
                        delay = max(delay, 1.0)
                    time.sleep(delay)
            # 当前端点全部重试失败，切换到下一个
            time.sleep(self._retry_delay)

        raise RuntimeError(_diagnose(last_err, attempted))

    def close(self) -> None:
        self._client.close()


def _diagnose(err: Optional[Exception], attempted: list[str]) -> str:
    """把原始异常转成对用户友好的诊断信息。"""
    msg = str(err) or type(err).__name__

    is_dns = any(
        k in msg.lower()
        for k in ("nodename", "name or service not known", "getaddrinfo", "not known")
    )
    is_timeout = "timeout" in msg.lower() or "timed out" in msg.lower()
    is_conn = "connection refused" in msg.lower() or "connection" in msg.lower()

    hint = (
        "无法解析 OKX 域名(DNS)。OKX 主站在部分网络(尤其中国大陆)会被污染。"
        "建议：① 使用 --proxy 指定可用的代理；② 或使用 --host 指向可访问的镜像；"
        "③ 或确认 DNS 设置后重试；④ 离线验证可用 --mock。"
        if is_dns
        else (
            "请求超时，OKX 服务器响应缓慢或被网络阻断。"
            "可尝试 --proxy 走代理，或增大超时后重试。"
            if is_timeout
            else (
                "网络连接失败，请检查网络/代理是否可用。"
                if is_conn
                else "OKX API 请求失败。"
            )
        )
    )

    return (
        f"请求 OKX K 线失败。已尝试端点: {', '.join(attempted)}；"
        f"原始错误: {msg}。\n{ hint }"
    )
