"""分离实验：形态 vs 止损宽度 —— 上一步那个 +0.111R 的差异来自哪里？

上一步发现（BTC 4H）：
    形态策略    n=2229  净期望 −0.0716R  t=−2.39
    纯趋势策略  n=4860  净期望 +0.0398R  t=+1.93
    差异 +0.1114R

但两个策略**同时变了两个东西**：
  (a) 有没有形态过滤
  (b) 止损口径（形态=结构止损=极紧 / 纯趋势=1.5×ATR=较宽）

本项目已知：止损越窄，f（费率占 R 的比例）越大，成本越重。
所以必须做 2×2 分解，把两个因素分开。

同时报告每组的 f 值（= 往返费率 / 止损占价比例），这是本项目一贯的口径。

**判定标准（开跑前定死）**：
  - 若"形态 vs 无形态"在同一止损口径下的差异 |Δ| < 0.05R → **差异来自止损宽度，不是形态**
  - 若差异仍 > 0.10R → 形态本身确实有（负）贡献
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics as st

from okx_client import OKXClient, Candle

PROXY = os.getenv("OKX_PROXY") or "http://127.0.0.1:7897"
ROUND_TRIP = 0.0007


def fetch(sym, bar, bars):
    return OKXClient(proxy=PROXY, cache_dir=".cache").get_candles(
        sym, bar=bar, limit=bars, use_cache=True)


def atr_series(cs, period=14):
    out = [None] * len(cs)
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


def find_patterns(cs):
    """返回 [(idx, action, entry, struct_stop)]，结构止损 = 形态极值。"""
    out = []
    for i in range(2, len(cs)):
        c, p = cs[i], cs[i - 1]
        rng = c.high - c.low
        if rng <= 0:
            continue
        body = abs(c.close - c.open)
        upper, lower = c.high - max(c.close, c.open), min(c.close, c.open) - c.low
        action = None
        if body > 0 and max(upper, lower) >= 2.0 * body:
            if lower > upper and lower >= 2.0 * body and c.close >= c.low + rng * 2 / 3:
                action = "BUY"
            elif upper > lower and upper >= 2.0 * body and c.close <= c.high - rng * 2 / 3:
                action = "SELL"
        if action is None:
            if c.close > c.open and p.close < p.open and c.close >= p.open and c.open <= p.close:
                action = "BUY"
            elif c.close < c.open and p.close > p.open and c.close <= p.open and c.open >= p.close:
                action = "SELL"
        if action is None:
            continue
        stp = (min(c.low, p.low) - rng * 0.1) if action == "BUY" \
            else (max(c.high, p.high) + rng * 0.1)
        out.append((i, action, c.close, stp))
    return out


def trend_dir(cs, i, atrs):
    """MA20 斜率方向，无趋势返回 None。"""
    if i < 26 or atrs[i] is None:
        return None
    ma_now = sum(x.close for x in cs[i - 20:i]) / 20
    ma_prev = sum(x.close for x in cs[i - 25:i - 5]) / 20
    slope = (ma_now - ma_prev) / ma_prev if ma_prev else 0
    th = 0.2 * atrs[i] / cs[i].close
    if slope > th:
        return "BUY"
    if slope < -th:
        return "SELL"
    return None


def outcome(cs, i, action, entry, stop, rr=2.0, max_bars=200):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    tp = entry + rr * risk if action == "BUY" else entry - rr * risk
    fee_r = ROUND_TRIP * entry / risk
    for j in range(i + 1, min(len(cs), i + 1 + max_bars)):
        c = cs[j]
        if action == "BUY":
            hit_sl, hit_tp = c.low <= stop, c.high >= tp
        else:
            hit_sl, hit_tp = c.high >= stop, c.low <= tp
        if hit_sl:
            return -1.0 - fee_r
        if hit_tp:
            return rr - fee_r
    last = cs[min(len(cs) - 1, i + max_bars)].close
    move = (last - entry) if action == "BUY" else (entry - last)
    return move / risk - fee_r


def stt(xs):
    n = len(xs)
    if n < 2:
        return {"n": n, "mean": 0.0, "t": 0.0, "sd": 0.0}
    m, sd = st.mean(xs), st.stdev(xs)
    return {"n": n, "mean": m, "sd": sd, "t": m / (sd / math.sqrt(n)) if sd else 0.0}


def main():
    random.seed(20260922)
    print("=" * 108)
    print("分离实验：形态的作用 vs 止损宽度的作用（2×2 分解）")
    print("=" * 108)

    for sym in ["ETH-USDT-SWAP", "BTC-USDT-SWAP"]:
        cs = fetch(sym, "4H", 6570)
        atrs = atr_series(cs)
        pats = find_patterns(cs)
        print(f"\n{'='*108}\n【{sym}】{len(cs)} 根 · 形态 {len(pats)} 个\n{'='*108}")

        # 形态的结构止损宽度（占价）
        widths = [abs(e - s) / e for _, _, e, s in pats]
        print(f"  形态结构止损宽度：中位 {st.median(widths)*100:.3f}%  "
              f"均值 {st.mean(widths)*100:.3f}%")
        print(f"  → 形态 f 中位 = {ROUND_TRIP/st.median(widths):.3f}R（每笔被费率吃掉）")
        atr_w = [1.5 * atrs[i] / cs[i].close for i in range(30, len(cs))
                 if atrs[i] and atrs[i] > 0]
        print(f"  1.5×ATR 止损宽度：中位 {st.median(atr_w)*100:.3f}%  "
              f"均值 {st.mean(atr_w)*100:.3f}%")
        print(f"  → ATR 档 f 中位 = {ROUND_TRIP/st.median(atr_w):.3f}R")

        # ---------- 2×2 分解 ----------
        print(f"\n  {'':<4}{'止损口径':<16}{'是否有形态':<14}{'n':>7}{'净期望':>12}"
              f"{'t':>9}{'f(中位)':>10}")
        grid = {}
        for stop_name in ["结构止损（紧）", "1.5×ATR（宽）"]:
            for use_pat in [True, False]:
                rets, fs = [], []
                if use_pat:
                    cand = [(i, a, e, (s if stop_name.startswith("结构")
                                       else (e - 1.5 * atrs[i] if a == "BUY" else e + 1.5 * atrs[i])))
                            for i, a, e, s in pats if atrs[i]]
                else:
                    cand = []
                    for i in range(30, len(cs) - 210):
                        if not atrs[i] or atrs[i] <= 0:
                            continue
                        d = trend_dir(cs, i, atrs)
                        if d is None:
                            continue
                        if stop_name.startswith("结构"):
                            # 用最近 5 根的极值当"结构止损"（无形态时的等价结构）
                            lo5 = min(x.low for x in cs[max(0, i-5):i+1])
                            hi5 = max(x.high for x in cs[max(0, i-5):i+1])
                            s = lo5 * 0.999 if d == "BUY" else hi5 * 1.001
                        else:
                            s = cs[i].close - 1.5 * atrs[i] if d == "BUY" \
                                else cs[i].close + 1.5 * atrs[i]
                        cand.append((i, d, cs[i].close, s))
                for i, a, e, s in cand:
                    r = outcome(cs, i, a, e, s)
                    if r is not None:
                        rets.append(r)
                        if abs(e - s) > 0:
                            fs.append(ROUND_TRIP / (abs(e - s) / e))
                stt_ = stt(rets)
                grid[(stop_name, use_pat)] = stt_
                if stt_["n"] >= 10:
                    print(f"      {stop_name:<16}{'形态' if use_pat else '纯趋势(无形态)':<14}"
                          f"{stt_['n']:>7}{stt_['mean']:>+12.4f}{stt_['t']:>+9.2f}"
                          f"{(st.median(fs) if fs else 0):>10.3f}")

        # ---------- 分解结论 ----------
        print(f"\n  【分解：形态的净贡献（同止损口径下，形态 − 无形态）】")
        for stop_name in ["结构止损（紧）", "1.5×ATR（宽）"]:
            a = grid[(stop_name, True)]
            b = grid[(stop_name, False)]
            if a["n"] >= 10 and b["n"] >= 10:
                d = a["mean"] - b["mean"]
                flag = "❌ 差异小（来自止损口径）" if abs(d) < 0.05 \
                    else ("⚠️ 形态有负贡献" if d < 0 else "⚠️ 形态有正贡献")
                print(f"      {stop_name:<16} 形态 {a['mean']:+.4f}  −  无形态 {b['mean']:+.4f}"
                      f"  =  {d:+.4f}R   {flag}")

        print(f"\n  【分解：止损口径的净贡献（同形态状态，宽 − 紧）】")
        for use_pat in [True, False]:
            a = grid[("1.5×ATR（宽）", use_pat)]
            b = grid[("结构止损（紧）", use_pat)]
            if a["n"] >= 10 and b["n"] >= 10:
                d = a["mean"] - b["mean"]
                print(f"      {'有形态' if use_pat else '无形态':<16} 宽 {a['mean']:+.4f}"
                      f"  −  紧 {b['mean']:+.4f}  =  {d:+.4f}R"
                      f"   {'✅ 宽止损显著更好' if d > 0.10 else ''}")

    print("\n" + "=" * 108)
    print("【解读】")
    print("=" * 108)
    print("  · 若『形态的净贡献』很小（|Δ|<0.05R），说明形态本身既不加分也不减分 ——")
    print("    它的全部表现差异都可由止损口径解释。")
    print("  · 若『止损口径的净贡献』为正且大（>0.10R），说明**真正重要的是风险管理结构**，")
    print("    这是价格行为学里唯一被数据支持的成分（与形态无关）。")
    print("  · 结合前一步（形态 α ≈ 0、纯趋势 α ≈ 0）：")
    print("    两个'信号源'都不含信息 ⇒ 差异只能来自**止损口径 → f → 成本**。")

    with open("/tmp/pa_decompose.json", "w") as f:
        json.dump({"note": "见终端"}, f)
    print("\n[已写入 /tmp/pa_decompose.json]")


if __name__ == "__main__":
    main()
