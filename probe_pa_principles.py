"""价格行为学的第一性原理检验 —— 融会贯通的核心实验。

问题：为什么形态策略在本项目上全线失败？是"参数没调好"，还是**理论基础不成立**？

价格行为学（Price Action）的核心因果假设链是：
    ┌─ 价格在某个位置出现特定形态（Pinbar/吞没）
    │        ↓ 因为
    │  该位置存在"买卖失衡"（订单流压力）
    │        ↓ 所以
    └─ 后续价格会朝形态方向移动（延续）

本项目已否证最后一步（形态后价格没有方向性：MFE ≈ MAE ≈ 1.2R）。
但**从未检验中间那一步**：形态真的对应"失衡"吗？

价格行为学给出的"位置"条件（这才是它区别于裸形态的关键）：
  ① **供给/需求区**：形态出现在前期的极值附近（支撑阻力）
  ② **趋势方向**：形态顺应趋势才有效（"顺势而为"）
  ③ **位置质量**：形态出现在"关键位"才有效

本脚本做三组检验，把"位置"这个维度**单独拎出来**测：
  【A】位置分层：形态出现在不同位置（距摆动极值远近 / 趋势状态），
       后续期望是否**系统性不同**？→ 若不同，"位置"就是真维度；若相同，形态学的位置论也不成立。
  【B】反转 vs 延续：价格行为学说"极值处反转、趋势中延续"。
       实测这两种上下文里的期望差。
  【C】"失衡"的直接测量：用形态 K 线的**实体占比 / 影线比 / 成交量倍数**当作
       "压力强度"的代理，看强度是否与后续期望单调相关。
       → 若强者恒强不成立，则"形态强度"也不是有效维度。

**判定标准（开跑前定死）**：
  - 位置分层的各层期望差必须 **> 0.10R 且方向一致**，才算"位置是真维度"；
  - 强度分层的期望必须**单调**，才算法有效；
  - 所有比较必须同时给出**样本量**与 **t 值**，避免"某个小样本格子里有漂亮数字"。
"""
from __future__ import annotations

import json
import math
import os
import statistics as st
from dataclasses import dataclass
from typing import Optional

from okx_client import OKXClient, Candle

PROXY = os.getenv("OKX_PROXY") or "http://127.0.0.1:7897"
ROUND_TRIP = 0.0007          # 挂单入 + 吃单出（本项目口径）


def fetch(sym: str, bar: str, bars: int) -> list[Candle]:
    cli = OKXClient(proxy=PROXY, cache_dir=".cache")
    return cli.get_candles(sym, bar=bar, limit=bars, use_cache=True)


# --------------------------------------------------------------------------
# 形态检测（复刻 patterns.py 的口径，但返回更多"上下文特征"）
# --------------------------------------------------------------------------
@dataclass
class Signal:
    idx: int
    action: str            # BUY / SELL
    entry: float
    stop: float
    risk: float
    # 上下文特征（本实验新增）
    trend_state: str       # up / down / flat（按 MA 斜率 + 位置判定）
    dist_to_swing: float   # 距最近摆动极值的距离（以 R 为单位），越小=越靠近关键位
    body_ratio: float      # 实体占振幅比例（"压力强度"代理 1）
    wick_ratio: float      # 主导影线 / 实体（"压力强度"代理 2）
    vol_mult: float        # 成交量 / 20 根均量（"压力强度"代理 3）
    near_extreme: bool     # 是否在最近 100 根的上下 20% 区间内


def atr_series(cs: list[Candle], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(cs)
    trs = []
    for i, c in enumerate(cs):
        if i == 0:
            trs.append(c.high - c.low)
            continue
        p = cs[i - 1].close
        trs.append(max(c.high - c.low, abs(c.high - p), abs(c.low - p)))
        if len(trs) >= period:
            out[i] = sum(trs[-period:]) / period
    return out


def detect_signals(cs: list[Candle], rr: float = 2.0,
                   min_gap: int = 0, atr_mult: float = 0.0) -> list[Signal]:
    """检出 pinbar/吞没信号，并附加位置与强度特征。"""
    n = len(cs)
    atrs = atr_series(cs)
    vols = [c.volume for c in cs]
    sigs: list[Signal] = []
    last_kind: dict[str, int] = {}

    for i in range(2, n):
        c, p = cs[i], cs[i - 1]
        rng = c.high - c.low
        if rng <= 0:
            continue
        body = abs(c.close - c.open)
        upper = c.high - max(c.close, c.open)
        lower = min(c.close, c.open) - c.low
        body_ratio = body / rng
        wick_ratio = (max(upper, lower) / body) if body > 0 else 99.0

        vol_mult = 1.0
        if i >= 20:
            base = sum(vols[i - 20:i]) / 20
            vol_mult = (c.volume / base) if base > 0 else 1.0

        name = None
        action = None

        # pinbar：影线 >= 2× 实体，收盘在中/上（下）1/3
        if body > 0 and max(upper, lower) >= 2.0 * body:
            if lower > upper and lower >= 2.0 * body and c.close >= c.low + rng * 2 / 3:
                name, action = "bullish_pinbar", "BUY"
            elif upper > lower and upper >= 2.0 * body and c.close <= c.high - rng * 2 / 3:
                name, action = "bearish_pinbar", "SELL"

        # 吞没
        if name is None:
            if c.close > c.open and p.close < p.open and c.close >= p.open and c.open <= p.close:
                name, action = "bullish_engulfing", "BUY"
            elif c.close < c.open and p.close > p.open and c.close <= p.open and c.open >= p.close:
                name, action = "bearish_engulfing", "SELL"

        if name is None:
            continue

        # 最小间隔（按形态类别）
        kind = "pinbar" if "pinbar" in name else "engulfing"
        if min_gap > 1 and i - last_kind.get(kind, -10**9) < min_gap:
            continue
        last_kind[kind] = i

        # ---- 结构止损 ----
        if action == "BUY":
            stop = min(lower and c.low or c.low, p.low) - (c.high - c.low) * 0.01
            stop = min(c.low, p.low) * 0.999 if min(c.low, p.low) > 0 else c.low
        else:
            stop = max(c.high, p.high) * 1.001
        entry = c.close
        risk = abs(entry - stop)
        if risk <= 0:
            continue

        # ---- 上下文特征 ----
        # 摆动极值：过去 100 根的最高/最低
        lo_win = max(0, i - 100)
        hi100 = max(x.high for x in cs[lo_win:i]) if i > lo_win else c.high
        lo100 = min(x.low for x in cs[lo_win:i]) if i > lo_win else c.low
        rng100 = hi100 - lo100
        if rng100 > 0:
            pos = (c.close - lo100) / rng100           # 0 = 100根最低，1 = 最高
            near_extreme = pos <= 0.20 or pos >= 0.80
            # 距最近极值的距离（以 R 为单位）
            d_hi = abs(hi100 - entry) / risk
            d_lo = abs(entry - lo100) / risk
            dist_to_swing = min(d_hi, d_lo)
        else:
            near_extreme, dist_to_swing = False, 99.0

        # 趋势状态：MA20 斜率
        if i >= 25:
            ma_now = sum(x.close for x in cs[i - 20:i]) / 20
            ma_prev = sum(x.close for x in cs[i - 25:i - 5]) / 20
            slope = (ma_now - ma_prev) / ma_prev if ma_prev else 0
            atr_v = atrs[i] or risk
            if slope > 0.2 * atr_v / entry:
                trend_state = "up"
            elif slope < -0.2 * atr_v / entry:
                trend_state = "down"
            else:
                trend_state = "flat"
        else:
            trend_state = "flat"

        sigs.append(Signal(i, action, entry, stop, risk, trend_state,
                           dist_to_swing, body_ratio, wick_ratio, vol_mult, near_extreme))
    return sigs


# --------------------------------------------------------------------------
# 结果评估
# --------------------------------------------------------------------------
def outcome(cs, s: Signal, rr=2.0, max_bars=200) -> Optional[float]:
    """返回以 R 计的净收益（含往返手续费）。"""
    tp = s.entry + rr * s.risk if s.action == "BUY" else s.entry - rr * s.risk
    for j in range(s.idx + 1, min(len(cs), s.idx + 1 + max_bars)):
        c = cs[j]
        if s.action == "BUY":
            hit_sl = c.low <= s.stop
            hit_tp = c.high >= tp
        else:
            hit_sl = c.high >= s.stop
            hit_tp = c.low <= tp
        if hit_sl and hit_tp:
            gross = -1.0                      # 保守：同根双触记亏损
        elif hit_sl:
            gross = -1.0
        elif hit_tp:
            gross = rr
        else:
            continue
        fee_r = ROUND_TRIP * s.entry / s.risk
        return gross - fee_r
    # 超时：按最后收盘价结算
    last = cs[min(len(cs) - 1, s.idx + max_bars)].close
    move = (last - s.entry) if s.action == "BUY" else (s.entry - last)
    gross = move / s.risk
    fee_r = ROUND_TRIP * s.entry / s.risk
    return gross - fee_r


def stats(rets: list[float]) -> dict:
    n = len(rets)
    if n < 2:
        return {"n": n, "mean": 0.0, "t": 0.0, "sd": 0.0}
    m = st.mean(rets)
    sd = st.stdev(rets)
    se = sd / math.sqrt(n)
    return {"n": n, "mean": m, "sd": sd, "t": (m / se if se else 0.0)}


def main() -> None:
    print("=" * 104)
    print("价格行为学第一性原理检验 —— 形态、位置、强度，哪个维度真的有效？")
    print("=" * 104)

    all_res = {}
    for sym, bar, bars in [("ETH-USDT-SWAP", "4H", 6570), ("BTC-USDT-SWAP", "4H", 6570)]:
        print(f"\n拉取 {sym} {bar} {bars} 根 ...")
        cs = fetch(sym, bar, bars)
        print(f"  实际 {len(cs)} 根：{cs[0].ts} ~ {cs[-1].ts}")
        sigs = detect_signals(cs, rr=2.0, min_gap=0, atr_mult=0.0)
        rows = []
        for s in sigs:
            r = outcome(cs, s)
            if r is not None:
                rows.append((s, r))
        print(f"  信号 {len(sigs)} 个，可结算 {len(rows)} 个")

        base = stats([r for _, r in rows])
        print(f"\n  【基准·全部形态混合】n={base['n']} 净期望 {base['mean']:+.4f}R  t={base['t']:+.2f}")

        # ---- 【A】按位置分层 ----
        print(f"\n  【A】位置分层：距离最近摆动极值多远（以 R 为单位）")
        print(f"      {'距离区间':<20}{'n':>7}{'净期望':>12}{'t':>9}")
        bands = [(0, 1), (1, 2), (2, 5), (5, 10), (10, 999)]
        for lo, hi in bands:
            sub = [r for s, r in rows if lo <= s.dist_to_swing < hi]
            stt = stats(sub)
            if stt["n"] >= 20:
                print(f"      {f'{lo}~{hi}R':<20}{stt['n']:>7}{stt['mean']:>+12.4f}{stt['t']:>+9.2f}")

        # ---- 【B】趋势状态 × 形态方向 ----
        print(f"\n  【B】趋势状态 × 方向（价格行为学：顺势延续 / 逆势反转）")
        print(f"      {'趋势':<8}{'方向':<7}{'n':>7}{'净期望':>12}{'t':>9}   {'含义':<24}")
        for tr in ["up", "down", "flat"]:
            for act in ["BUY", "SELL"]:
                sub = [r for s, r in rows if s.trend_state == tr and s.action == act]
                stt = stats(sub)
                if stt["n"] < 20:
                    continue
                align = (tr == "up" and act == "BUY") or (tr == "down" and act == "SELL")
                if tr == "flat":
                    meaning = "无趋势（纯形态）"
                elif align:
                    meaning = "顺势（延续假设）"
                else:
                    meaning = "逆势（反转假设）"
                print(f"      {tr:<8}{act:<7}{stt['n']:>7}{stt['mean']:>+12.4f}"
                      f"{stt['t']:>+9.2f}   {meaning:<24}")

        # ---- 【C】强度分层 ----
        print(f"\n  【C-a】实体占比（压力强度代理）：实体越大是否越有效？")
        print(f"      {'实体占比':<16}{'n':>7}{'净期望':>12}{'t':>9}")
        for lo, hi in [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]:
            sub = [r for s, r in rows if lo <= s.body_ratio < hi]
            stt = stats(sub)
            if stt["n"] >= 20:
                print(f"      {f'{lo:.1f}~{hi:.1f}':<16}{stt['n']:>7}{stt['mean']:>+12.4f}{stt['t']:>+9.2f}")

        print(f"\n  【C-b】成交量倍数（订单流强度代理）：放量是否更有效？")
        print(f"      {'量能倍数':<16}{'n':>7}{'净期望':>12}{'t':>9}")
        for lo, hi in [(0, 0.8), (0.8, 1.2), (1.2, 2.0), (2.0, 3.0), (3.0, 99)]:
            sub = [r for s, r in rows if lo <= s.vol_mult < hi]
            stt = stats(sub)
            if stt["n"] >= 20:
                print(f"      {f'{lo:.1f}~{hi:.1f}x':<16}{stt['n']:>7}{stt['mean']:>+12.4f}{stt['t']:>+9.2f}")

        print(f"\n  【C-c】主导影线/实体（拒绝力度代理）：影线越极端是否越有效？")
        print(f"      {'影线/实体':<16}{'n':>7}{'净期望':>12}{'t':>9}")
        for lo, hi in [(2, 3), (3, 5), (5, 10), (10, 99)]:
            sub = [r for s, r in rows if lo <= s.wick_ratio < hi]
            stt = stats(sub)
            if stt["n"] >= 20:
                print(f"      {f'{lo}~{hi}':<16}{stt['n']:>7}{stt['mean']:>+12.4f}{stt['t']:>+9.2f}")

        # ---- 【D】关键位 × 顺势（价格行为学的"完整条件"）----
        print(f"\n  【D】价格行为学的完整条件：关键位 + 顺势（最严格的一层）")
        print(f"      {'条件':<34}{'n':>7}{'净期望':>12}{'t':>9}")
        tests = [
            ("全部（基准）", lambda s: True),
            ("靠近极值（<3R）", lambda s: s.dist_to_swing < 3),
            ("在关键位区间（近100根上下20%）", lambda s: s.near_extreme),
            ("顺势", lambda s: (s.trend_state == "up" and s.action == "BUY")
                              or (s.trend_state == "down" and s.action == "SELL")),
            ("顺势 + 靠近极值", lambda s: ((s.trend_state == "up" and s.action == "BUY")
                                      or (s.trend_state == "down" and s.action == "SELL"))
             and s.dist_to_swing < 3),
            ("顺势 + 关键位 + 放量>1.5x", lambda s: ((s.trend_state == "up" and s.action == "BUY")
                                                 or (s.trend_state == "down" and s.action == "SELL"))
             and s.near_extreme and s.vol_mult > 1.5),
            ("逆势（反转假设）", lambda s: (s.trend_state == "up" and s.action == "SELL")
                                     or (s.trend_state == "down" and s.action == "BUY")),
        ]
        for label, f in tests:
            sub = [r for s, r in rows if f(s)]
            stt = stats(sub)
            if stt["n"] >= 20:
                print(f"      {label:<34}{stt['n']:>7}{stt['mean']:>+12.4f}{stt['t']:>+9.2f}")

        all_res[sym] = {"n": base["n"], "mean": base["mean"], "t": base["t"]}

    print("\n" + "=" * 104)
    print("【判定】")
    print("=" * 104)
    print("  · 若【A】各距离区间的期望**没有系统性差异** → '关键位置'不是有效维度")
    print("  · 若【B】顺势/逆势的期望**都接近 0 且 t<2** → 趋势方向也不能救形态")
    print("  · 若【C】强度分层**不单调** → '形态强度'不是有效维度")
    print("  · 若【D】最严格条件仍 t<2 → **价格行为学的可测维度已全部穷尽**")

    with open("/tmp/pa_first_principles.json", "w") as f:
        json.dump(all_res, f, indent=2)
    print("\n[已写入 /tmp/pa_first_principles.json]")


if __name__ == "__main__":
    main()
