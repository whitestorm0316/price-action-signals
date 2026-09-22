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
    python main.py --entry-mode breakout              # 突破入场（等价格越过形态极值）
    python main.py --atr-mult 1.5                     # 止损改用 1.5×ATR（放宽止损以降低 f 成本）
    python main.py --min-gap 40                       # 同形态最小间隔 40 根（降频）
    python main.py --preset candidate                 # 套用复盘实测最好的候选（⚠️ 统计不显著、不可交易）
    python main.py --preset trend_wide                # 纯趋势 + 1.5×ATR 宽止损（⚠️ 同样未验证）
"""
from __future__ import annotations

import argparse
import os
import sys

from atr import DEFAULT_PERIOD as ATR_PERIOD, atr_at
from okx_client import OKXClient
from logs_utils import get_logger
from patterns import build_trend_signal, detect_engulfing, detect_pinbar
from signals import pattern_to_signal, print_signal_json, print_signal_pretty
from strategy_config import (
    ENTRY_MODES,
    blocking_reasons,
    env_overrides,
    filter_min_gap,
    pattern_kind,
    preset_name,
    resolve as resolve_config,
    warning_lines_for,
)
from trend import trend_allows, trend_at, trend_slope_at
from volume import should_filter, volume_ratio_at


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX 价格行为学交易信号生成器")
    # 可预设项默认值均为 None：表示"未显式指定"，由 --preset / 内置默认值填充。
    # 这样裸命令（不带任何参数）的行为与以往**逐字节一致**。
    parser.add_argument(
        "--inst", "--ticker", dest="inst_id", default=None,
        help="OKX 交易对 (默认 BTC-USDT-SWAP)",
    )
    parser.add_argument(
        "--bar", default=None,
        help="K 线周期 (默认 5m，支持 1m/3m/5m/15m/30m/1H/4H/1D)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="拉取 K 线条数 (默认 300，超过 300 自动分页；回测一整年可用 "
             "如 15m 一年=35040、1H 一年=8760，默认上限 60000，"
             "可用环境变量 OKX_MAX_CANDLES 调整)",
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
        help="HTTP 代理地址，如 http://127.0.0.1:7890（OKX 被墙时使用）；"
             "未传时自动读取环境变量 OKX_PROXY",
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
        "--trend", type=int, default=None, metavar="PERIOD",
        help="启用均线趋势过滤，PERIOD 为均线周期（如 50）。"
             "上升趋势只做多、下降趋势只做空，过滤逆势信号；"
             "默认不启用趋势过滤",
    )
    parser.add_argument(
        "--ma-type", choices=("sma", "ema"), default=None,
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
        "--volume", action="store_true", default=None,
        help="启用 量能确认：为每个形态附加 volume_ratio / volume_signal / "
             "volume_confirm；放量信号加权、缩量信号减权，并过滤 "
             "'缩量且盈亏比<2' 的信号。默认关闭（量能可选）",
    )
    parser.add_argument(
        "--rr", type=float, default=None, metavar="RR",
        help="盈亏比 (Reward/Risk)：目标价 = 入场 ± 止损距离 × 盈亏比，"
             "默认 2（即 2:1）。例 --rr 3 收窄为3比1，--rr 1.5 更容易止盈但单笔收益小",
    )
    parser.add_argument(
        "--entry-mode", choices=ENTRY_MODES, default=None,
        help="入场方式 (默认 close)。close=形态收盘价入场（原有行为）；"
             "breakout=等价格越过形态极值才进场（复盘显示可过滤假信号，"
             "但实盘需要触发单而非限价单）",
    )
    parser.add_argument(
        "--signal-mode", choices=("pattern", "trend"), default=None,
        help="信号来源模式 (默认 pattern，行为不变)。"
             "pattern=检测 Pinbar/吞没形态触发（原有行为）；"
             "trend=**纯趋势模式**：不发形态，只看均线斜率决定方向，"
             "按 K×ATR 定价止损——用于真实表达「纯趋势 + 宽止损」这一组合"
             "（第八轮证明形态不含方向信息，故 trend_wide 预设需此模式才自洽）。"
             "⚠️ 纯趋势同样未通过独立验证。",
    )
    parser.add_argument(
        "--atr-mult", type=float, default=None, metavar="K",
        help="止损改为 K × ATR(14) 的口径（默认不传 = 结构止损，行为不变）。"
             "第八轮证明：放宽止损能显著降低 f（往返费率占 R 的比例），"
             "从而改善净期望——但这是**成本效应，不是方向信息**。"
             "实测 1.5 在 4H 上把 BTC 的 f 由 0.065R 降到 0.035R。"
             "两种口径取「离入场更远」的一侧，避免把风险放大到形态之外。",
    )
    parser.add_argument(
        "--min-gap", type=int, default=None, metavar="N",
        help="同形态（pinbar / 吞没 分类计算）最小间隔 N 根 K 线，用于降频。"
             "形态会聚集出现，后续信号多为追单；默认 0=不过滤。"
             "复盘实测最优约 40（ETH 4H 基准 +0.161R / t=1.84）",
    )
    parser.add_argument(
        "--preset", choices=("legacy", "candidate", "trend_wide", "optimal"), default=None,
        help="套用参数预设。legacy=原有默认行为；"
             "candidate=复盘中实测最好的形态候选（ETH 4H + 突破入场 + 结构止损 "
             "+ 关闭趋势过滤 + 间隔40）——⚠️ **统计不显著（t=1.84、CI 含 0）**、"
             "第八轮证明其 **α≈0（不含方向信息）**、在 BTC 上显著为负，**不可交易**；"
             "trend_wide=纯趋势 + 1.5×ATR 宽止损（**不用任何形态**）——唯一还有正 t "
             "的配置，但正 t 来自市场漂移而非择时，ETH 上为负，**同样未验证**。"
             "``optimal`` 是 candidate 的历史别名（等价）。"
             "未显式指定的参数才会被预设填充，命令行显式值优先",
    )
    return parser.parse_args(argv)


#: 预设字段 → main.py 参数名 的映射（用于把 --preset 展开成 CLI 参数）
_PRESET_FIELD_TO_ARG = {
    "ticker": "inst_id",
    "interval": "bar",
    "limit": "limit",
    "trend_period": "trend",
    "ma_type": "ma_type",
    "volume_filter": "volume",
    "entry_mode": "entry_mode",
    "signal_mode": "signal_mode",
    "atr_mult": "atr_mult",
    "min_gap": "min_gap",
    "rr": "rr",
}

#: 裸命令（不带 --preset）时各参数的内置默认值 —— 与改造前完全一致
_CLI_BUILTIN_DEFAULTS = {
    "inst_id": "BTC-USDT-SWAP",
    "bar": "5m",
    "limit": 300,
    "trend": 0,
    "ma_type": "sma",
    "volume": False,
    "entry_mode": "close",
    "signal_mode": "pattern",
    "atr_mult": 0,
    "min_gap": 0,
    "rr": 2.0,
}


def apply_preset(args: argparse.Namespace) -> argparse.Namespace:
    """把 ``--preset`` / ``PA_*`` 环境变量展开成参数值。

    优先级（低 → 高）：内置默认值 → 预设值 → ``PA_*`` 环境变量 → 命令行显式值。
    因此 ``--preset candidate --bar 1H`` 会得到 1H（用户显式值胜出），
    其余参数取候选组合；``PA_ATR_MULT=1.5`` 也会盖掉预设里的 ``atr_mult``。

    未设置任何 ``PA_*``、也未传 ``--preset`` 时，结果与改造前**逐字节一致**。
    """
    # ① 先记录命令行显式指定的项（此时未指定者仍为 None）
    explicit = {
        name for name in _CLI_BUILTIN_DEFAULTS
        if getattr(args, name, None) is not None
    }
    # ② PA_* 环境变量：填充未被命令行指定的项，且视为"已指定"（不再被预设覆盖）
    try:
        env_vals = env_overrides()
    except Exception:  # noqa: BLE001
        env_vals = {}
    for field, arg_name in _PRESET_FIELD_TO_ARG.items():
        if arg_name in explicit or field not in env_vals:
            continue
        setattr(args, arg_name, env_vals[field])
        explicit.add(arg_name)
    # ③ 内置默认值填空
    for arg_name, builtin in _CLI_BUILTIN_DEFAULTS.items():
        if getattr(args, arg_name, None) is None:
            setattr(args, arg_name, builtin)
    # ④ 预设名：--preset > PA_PRESET（兼容历史别名 optimal）
    if not args.preset:
        try:
            env_preset = os.getenv("PA_PRESET")
            if env_preset:
                args.preset = preset_name(env_preset)
        except Exception:  # noqa: BLE001
            pass
    if not args.preset:
        return args
    try:
        cfg = resolve_config(preset=args.preset, use_env=False)
    except Exception:  # noqa: BLE001
        return args
    for field, arg_name in _PRESET_FIELD_TO_ARG.items():
        # 已由命令行或环境变量指定时不覆盖
        if arg_name in explicit:
            continue
        if field in cfg:
            setattr(args, arg_name, cfg[field])
    return args


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
    args = apply_preset(parse_args(argv))
    log = get_logger("price-action")
    # 未验证配置（candidate 预设 / 突破入场）：先打印醒目告警
    _print_warnings(args)

    if args.mock:
        candles = _mock_candles()
    else:
        # CLI --proxy 优先；否则读 OKX_PROXY 环境变量（与仪表盘一致）
        effective_proxy = args.proxy or os.getenv("OKX_PROXY")
        client = OKXClient(
            timeout=args.timeout,
            proxy=effective_proxy,
            host=args.host,
            cache_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache"),
        )
        try:
            # 回测使用磁盘缓存（.cache 目录）：同一时段重复回测不再重新拉取
            use_cache = bool(args.backtest) and not args.mock
            candles = client.get_candles(
                args.inst_id, bar=args.bar, limit=args.limit, use_cache=use_cache,
            )
            log.info("K线获取 %s %s %d根(backtest=%s cache=%s)",
                     args.inst_id, args.bar, len(candles), bool(args.backtest), use_cache)
        except RuntimeError as exc:
            log.error("K线获取失败 %s %s: %s", args.inst_id, args.bar, exc)
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
        rc = _run_backtest(candles, args)
        log.info("回测完成 %s %s N=%d rc=%s", args.inst_id, args.bar, args.backtest, rc)
        return rc
    return _run_latest(candles, args)


def _detect_at(candles: list, idx: int, args: argparse.Namespace) -> list:
    """在 candles[idx] 处产生信号。

    两种模式（``--signal-mode``）：
      * ``pattern``（默认）—— 检测 Pinbar/吞没，可选趋势过滤与量能过滤。
        不传新参数时行为与改造前**逐字节一致**。
      * ``trend`` —— **纯趋势模式**：完全不看形态，只用均线斜率定方向，
        按 K×ATR 定价止损。用于真实表达第八轮的"纯趋势 + 宽止损"组合。
    """
    smode = getattr(args, "signal_mode", None) or "pattern"
    # 止损口径：--atr-mult（默认 0/None = 结构止损，行为不变）。
    # 仅在需要时计算 ATR，避免默认路径多算一步（保证默认行为逐字节不变）。
    am = getattr(args, "atr_mult", None) or 0

    if smode == "trend":
        # 纯趋势：不看形态。方向由 MA 斜率决定，止损按 ATR。
        period = getattr(args, "trend", 0) or 0
        if period <= 0:
            return []   # 无趋势周期则无从判定方向，不发信号（不猜）
        mult = float(am) if am else 1.5
        atr_v = atr_at(candles, idx)
        if not atr_v:
            return []
        direction = trend_slope_at(
            candles, idx, period, getattr(args, "ma_type", "sma") or "sma",
            atr=atr_v,
        )
        if direction is None:
            return []   # 横盘/无趋势：不发信号
        action = "BUY" if direction == "up" else "SELL"
        p = build_trend_signal(candles[idx], action, atr_v, atr_mult=mult, rr=args.rr)
        return [p] if p is not None else []

    cur = candles[idx]
    prev = candles[idx - 1]
    # 量能可选：仅在 --volume 时计算量能比率，并把它作为形态的确认/降级依据
    ratio = volume_ratio_at(candles, idx) if args.volume else None
    # 入场方式：--entry-mode（默认 close，行为不变）
    em = getattr(args, "entry_mode", None) or "close"
    atr_v = atr_at(candles, idx) if am else None
    found = []
    for pattern in (
        detect_pinbar(cur, volume_ratio=ratio, rr=args.rr,
                      atr=atr_v, atr_mult=am or None, entry_mode=em),
        detect_engulfing(prev, cur, volume_ratio=ratio, rr=args.rr,
                         atr=atr_v, atr_mult=am or None, entry_mode=em),
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


def _apply_min_gap(items: list, gap: int) -> list:
    """对 ``(idx, pattern)`` 列表应用同形态最小间隔过滤。

    ``gap <= 1`` 时原样返回（= 不改行为）。返回按 idx 升序。
    """
    try:
        g = int(gap) if gap else 0
    except (TypeError, ValueError):
        g = 0
    if g <= 1:
        return sorted(items, key=lambda it: it[0])
    return filter_min_gap(
        items, g,
        kind_of=lambda it: pattern_kind(it[1].name),
        order_of=lambda it: it[0],
    )


def _print_warnings(args: argparse.Namespace) -> None:
    """未验证配置：在输出前打印醒目告警（stderr，不污染 JSON 输出）。

    注意：CLI 的"原有行为"基线是**趋势过滤关闭**，因此单独不启用趋势过滤
    对本命令而言不算偏离，只按**阻断类**特征（candidate / trend_wide 预设、
    突破入场、ATR 止损）告警。
    """
    cfg = {
        "preset": args.preset or "legacy",
        "entry_mode": getattr(args, "entry_mode", None) or "close",
        "signal_mode": getattr(args, "signal_mode", None) or "pattern",
        "atr_mult": getattr(args, "atr_mult", None) or 0,
        "trend_period": 1,   # CLI 基线为关闭趋势过滤，故不把它当作偏离
    }
    for line in warning_lines_for(blocking_reasons(cfg), blocking=blocking_reasons(cfg)):
        print(line, file=sys.stderr)


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


def _notes_for(candles: list, args: argparse.Namespace) -> list[str]:
    """汇总本命令实际生效的参数标注（供扫描/回测尾部提示）。

    对 ATR 止损做**可用性核对**：K 线不足 ATR 周期时 ``atr_at`` 返回 None，
    止损会静默回退结构止损。此时若仍打印"止损=ATR×N"就是**标注与实际不符**，
    故只在真的算得出 ATR 时才宣称。
    """
    notes = []
    if getattr(args, "trend", None):
        notes.append(f"{args.trend}周期{args.ma_type.upper()} 趋势过滤")
    if (getattr(args, "entry_mode", None) or "close") != "close":
        notes.append(f"入场={args.entry_mode}")
    am = getattr(args, "atr_mult", None)
    if am:
        if atr_at(candles, len(candles) - 1) is not None:
            notes.append(f"止损=ATR×{am:g}")
        else:
            notes.append(f"止损=ATR×{am:g}（K线不足 {ATR_PERIOD} 根，实际回退结构止损）")
    if getattr(args, "signal_mode", None) == "trend":
        notes.append("来源=纯趋势(不用形态)")
    if getattr(args, "min_gap", 0) and int(args.min_gap) > 1:
        notes.append(f"同形态间隔≥{args.min_gap}根")
    return notes


def _run_scan(candles: list, args: argparse.Namespace) -> int:
    """扫描最近 N 根 K 线，报告每根检测到的历史信号（验证用）。"""
    n = min(args.scan, len(candles))
    # 先收集候选 (idx, pattern)，再做同形态最小间隔过滤（跨 K 线生效）
    candidates: list[tuple[int, object]] = []
    for idx in range(len(candles) - 1, len(candles) - n - 1, -1):
        for p in _detect_at(candles, idx, args):
            candidates.append((idx, p))
    kept = _apply_min_gap(candidates, getattr(args, "min_gap", 0) or 0)

    total = len(kept)
    for idx, p in kept:
        ts_line = f"[{candles[idx].ts}]"
        print(f"{ts_line} {p.name:<20} action={p.action:<4} "
              f"entry={p.entry:.6g} sl={p.stop:.6g} tp={p.take_profit:.6g} rr={p.risk_reward}")
    notes = _notes_for(candles, args)
    filter_note = f"（含 {'、'.join(notes)}）" if notes else ""
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

    # 先收集候选 (idx, pattern)，再做同形态最小间隔过滤（跨 K 线生效），最后判定结局
    candidates: list[tuple[int, object]] = []
    for idx in range(len(candles) - 1, len(candles) - n - 1, -1):
        for pattern in _detect_at(candles, idx, args):
            candidates.append((idx, pattern))
    for idx, pattern in _apply_min_gap(candidates, getattr(args, "min_gap", 0) or 0):
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
    notes = _notes_for(candles, args)
    filter_note = f"（含 {'、'.join(notes)}）" if notes else ""
    print(
        f"—— 回测最近 {n} 根 {args.bar} K 线{filter_note}：共 {total} 个信号，"
        f"止盈 {stats['WIN']} / 止损 {stats['LOSS']} / 持仓中 {stats['OPEN']}，"
        f"已平仓胜率 {win_rate:.1f}% ——"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
