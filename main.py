"""价格行为学交易信号生成器 —— 主入口。

从 OKX 公开 API 拉取 K 线，检测 Pinbar / 吞没形态，生成 TradingView Webhook
兼容的 JSON 信号并打印到控制台（暂未接入真实 Webhook）。

用法示例:
    python main.py                                    # 默认 BTC-USDT-SWAP 5m
    python main.py --inst ETH-USDT-SWAP --bar 15m     # 指定交易对与周期
    python main.py --proxy http://127.0.0.1:7890      # OKX 被墙时走代理
    python main.py --host https://your-mirror/api/v5  # 自定义数据端点
    python main.py --pretty                           # 易读格式输出
    python main.py --mock                             # 离线演示
    python main.py --trend 50                         # 启用 50 周期均线趋势过滤
    python main.py --volume                           # 启用 量能确认（放量加权/缩量降级 + 弱量低盈亏比过滤）
"""
from __future__ import annotations

import argparse
import sys

from okx_client import OKXClient
from patterns import detect_engulfing, detect_pinbar
from signals import pattern_to_signal, print_signal_json, print_signal_pretty
from trend import trend_allows, trend_at
from volume import should_filter, volume_ratio_at


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX 价格行为学交易信号生成器")
    parser.add_argument(
        "--inst", "--ticker", dest="inst_id", default="BTC-USDT-SWAP",
        help="OKX 交易对 (默认 BTC-USDT-SWAP)",
    )
    parser.add_argument(
        "--bar", default="5m",
        help="K 线周期 (默认 5m，支持 1m/3m/5m/15m/30m/1H/4H/1D)",
    )
    parser.add_argument(
        "--limit", type=int, default=300,
        help="拉取 K 线条数 (默认 300，最多 3000，超过 300 自动分页)",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="以易读格式打印信号 JSON",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="使用内置模拟 K 线数据（离线演示/测试用，不请求网络）",
    )
    parser.add_argument(
        "--proxy", default=None,
        help="HTTP 代理地址，如 http://127.0.0.1:7890（OKX 被墙时使用）",
    )
    parser.add_argument(
        "--host", default=None,
        help="自定义数据端点前缀，如 https://your-mirror.example.com/api/v5",
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0,
        help="单次请求超时秒数 (默认 10)",
    )
    parser.add_argument(
        "--trend", type=int, default=0, metavar="PERIOD",
        help="启用均线趋势过滤，PERIOD 为均线周期（如 50）。"
             "上升趋势只做多、下降趋势只做空，过滤逆势信号；"
             "默认 0=不启用趋势过滤",
    )
    parser.add_argument(
        "--ma-type", choices=("sma", "ema"), default="sma",
        help="趋势均线类型 (默认 sma，可选 ema)，配合 --trend 使用",
    )
    parser.add_argument(
        "--scan", type=int, default=0, metavar="N",
        help="扫描最近 N 根 K 线的历史信号（用于验证数据链路与形态检测，"
             "默认 0=仅检测最新一根，N 最多为拉取的 K 线数）",
    )
    parser.add_argument(
        "--backtest", type=int, default=0, metavar="N",
        help="对最近 N 根 K 线上的信号做回测：跟踪信号之后的价格走势，"
             "统计 止盈(WIN)/止损(LOSS)/持仓中(OPEN) 与胜率",
    )
    parser.add_argument(
        "--volume", action="store_true",
        help="启用 量能确认：为每个形态附加 volume_ratio / volume_signal / "
             "volume_confirm；放量信号加权、缩量信号减权，并过滤 "
             "'缩量且盈亏比<2' 的信号。默认关闭（量能可选）",
    )
    return parser.parse_args(argv)


def _mock_candles(bar: str = "5m") -> list:
    """构造一段含 Pinbar + 吞没的模拟 K 线，用于离线验证完整链路。"""
    from okx_client import Candle

    rows = [
        # (open, high, low, close)
        (100.0, 100.5, 99.6, 100.2),   # 普通阳线
        (100.2, 100.8, 99.8, 100.4),   # 普通阳线
        (100.4, 100.9, 99.9, 100.1),   # 普通小阴线
        # 看跌吞没: 前阳(100.1~100.7) 被当前大阴实体覆盖
        (100.1, 100.7, 99.9, 100.6),
        (100.6, 101.0, 98.8, 98.9),    # 大阴线实体[98.9,100.6] 吞没前一根实体
        # 看涨 Pinbar: 下影线长、收盘接近高点
        (98.9, 100.2, 96.8, 99.9),
    ]
    base_ts = 1_700_000_000_000
    candles = []
    for i, (o, h, l, cl) in enumerate(rows):
        candles.append(Candle(ts=base_ts + i * 300_000, open=o, high=h, low=l, close=cl, volume=100.0))
    return candles


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.mock:
        candles = _mock_candles()
    else:
        client = OKXClient(
            timeout=args.timeout,
            proxy=args.proxy,
            host=args.host,
        )
        try:
            candles = client.get_candles(args.inst_id, bar=args.bar, limit=args.limit)
        except RuntimeError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            print("提示: 可尝试 --mock 进行离线演示，或参考上方诊断信息调整网络配置。",
                  file=sys.stderr)
            return 1
        finally:
            client.close()

    if len(candles) < 2:
        print("K 线数据不足，无法检测形态", file=sys.stderr)
        return 1

    if args.scan:
        return _run_scan(candles, args)
    if args.backtest:
        return _run_backtest(candles, args)
    return _run_latest(candles, args)


def _detect_at(candles: list, idx: int, args: argparse.Namespace) -> list:
    """在 candles[idx] 处检测 Pinbar/吞没，应用趋势过滤与量能过滤，返回保留的信号列表。"""
    cur = candles[idx]
    prev = candles[idx - 1]
    # 量能可选：仅在 --volume 时计算量能比率，并把它作为形态的确认/降级依据
    ratio = volume_ratio_at(candles, idx) if args.volume else None
    found = []
    for pattern in (
        detect_pinbar(cur, volume_ratio=ratio),
        detect_engulfing(prev, cur, volume_ratio=ratio),
    ):
        if pattern is None:
            continue
        if args.trend:
            trend = trend_at(candles, idx, args.trend, args.ma_type)
            if not trend_allows(pattern, trend):
                continue  # 逆势，过滤掉
        if args.volume and should_filter(ratio, pattern.risk_reward):
            continue  # 缩量且盈亏比<2，不值得冒险，过滤掉
        found.append(pattern)
    return found


def _run_latest(candles: list, args: argparse.Namespace) -> int:
    """仅检测最新一根 K 线（实时触发用）。"""
    current = candles[-1]
    found = _detect_at(candles, len(candles) - 1, args)

    if not found:
        print(
            f"未检测到价格行为信号（已拉取 {len(candles)} 根 {args.bar} K 线，"
            f"最新一根 [{current.ts}] O={current.open:.6g} H={current.high:.6g} "
            f"L={current.low:.6g} C={current.close:.6g}）。"
        )
        if args.trend:
            print(f"已启用 {args.trend} 周期 {args.ma_type.upper()} 趋势过滤。")
        if args.volume:
            print("已启用 量能确认：放量加权 / 缩量降级，缩量且盈亏比<2 的信号被过滤。")
        print("提示: 可用 --scan N 扫描最近 N 根 K 线的历史信号，验证数据链路与形态检测。")
        return 0

    for pattern in found:
        sig = pattern_to_signal(pattern, args.inst_id)
        if args.pretty:
            print_signal_pretty(sig)
        else:
            print_signal_json(sig)
    return 0


def _run_scan(candles: list, args: argparse.Namespace) -> int:
    """扫描最近 N 根 K 线，报告每根检测到的历史信号（验证用）。"""
    n = min(args.scan, len(candles))
    total = 0
    # 从最新往旧扫描；每根作为"当前"，其前一根作为吞没比较对象
    for idx in range(len(candles) - 1, len(candles) - n - 1, -1):
        found = _detect_at(candles, idx, args)
        if not found:
            continue
        cur = candles[idx]
        total += len(found)
        ts_line = f"[{cur.ts}]"
        for p in found:
            print(f"{ts_line} {p.name:<20} action={p.action:<4} "
                  f"entry={p.entry:.6g} sl={p.stop:.6g} tp={p.take_profit:.6g} rr={p.risk_reward}")
    filter_note = f"（含 {args.trend}周期{args.ma_type.upper()} 趋势过滤）" if args.trend else ""
    print(f"—— 扫描最近 {n} 根 {args.bar} K 线，共检测到 {total} 个信号{filter_note} ——")
    return 0


def _evaluate(idx: int, pattern, candles: list) -> tuple[str, object]:
    """回测单个信号：用信号之后的价格走势判断结果。

    从信号所在 K 线的下一根开始，逐根判断是否先触及止损或止盈。

    Returns:
        (result, ts)：
          result ∈ {"WIN", "LOSS", "OPEN"}
          WIN  = 先到达目标价（成功）
          LOSS = 先触发止损（失败）
          OPEN = 到数据末尾仍未触及（仍在持仓）
        ts 为触发/当前最新 K 线的 ts（OPEN 时返回最新 ts）
    """
    sl, tp = pattern.stop, pattern.take_profit
    is_buy = pattern.action == "BUY"

    for j in range(idx + 1, len(candles)):
        c = candles[j]
        if is_buy:
            hit_sl = c.low <= sl
            hit_tp = c.high >= tp
        else:  # SELL：sl 在价格上方，tp 在下方
            hit_sl = c.high >= sl
            hit_tp = c.low <= tp

        if hit_sl and hit_tp:
            # 同根既触及止损又触及止盈：无法确定先后，保守按止损计
            return "LOSS", c.ts
        if hit_sl:
            return "LOSS", c.ts
        if hit_tp:
            return "WIN", c.ts

    return "OPEN", candles[-1].ts if candles else None


def _run_backtest(candles: list, args: argparse.Namespace) -> int:
    """对最近 N 根 K 线检出的信号做回测，统计成功/失败/持仓中。"""
    n = min(args.backtest, len(candles) - 1)
    stats = {"WIN": 0, "LOSS": 0, "OPEN": 0}
    detail: list[tuple[object, str, object]] = []  # (pattern, result, ts)

    for idx in range(len(candles) - 1, len(candles) - n - 1, -1):
        for pattern in _detect_at(candles, idx, args):
            result, ts = _evaluate(idx, pattern, candles)
            stats[result] += 1
            detail.append((pattern, result, ts))

    # 按信号时间升序打印
    detail.reverse()
    for pattern, result, ts in detail:
        label = {"WIN": "✅ 止盈成功", "LOSS": "❌ 止损触发", "OPEN": "⏳ 持仓中"}[result]
        print(
            f"[{ts}] {pattern.name:<20} action={pattern.action:<4} "
            f"entry={pattern.entry:.6g} sl={pattern.stop:.6g} tp={pattern.take_profit:.6g} "
            f"rr={pattern.risk_reward}  ->  {label}"
        )

    total = stats["WIN"] + stats["LOSS"] + stats["OPEN"]
    closed = stats["WIN"] + stats["LOSS"]
    win_rate = (stats["WIN"] / closed * 100) if closed else 0.0
    filter_note = f"（含 {args.trend}周期{args.ma_type.upper()} 趋势过滤）" if args.trend else ""
    print(
        f"—— 回测最近 {n} 根 {args.bar} K 线{filter_note}：共 {total} 个信号，"
        f"止盈 {stats['WIN']} / 止损 {stats['LOSS']} / 持仓中 {stats['OPEN']}，"
        f"已平仓胜率 {win_rate:.1f}% ——"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
