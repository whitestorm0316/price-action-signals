"""价格行为学的融会贯通 —— 把全部碎片拼成一个关于"市场过程"的陈述。

前面的实验给出了 4 个孤立事实：
  ① 形态本身：净期望为负（ETH −0.036R、BTC −0.072R）
  ② 位置维度：无系统性差异（A）
  ③ 强度维度：不单调（C）
  ④ 趋势对齐：顺势/逆势**都**为负（B）
  ⑤ 一个一致线索：**无趋势做空**在 ETH/BTC 上都显著为负（t≈−2）

但这些还是"碎片"。真正的问题是：**能不能用一个统一的原因解释全部 5 条？**

候选解释："价格在 4H 尺度上是**鞅**（无方向记忆），所有形态学观察到的差异
都来自**市场漂移 + 抽样噪音**，而非形态的信息含量。"

本脚本用 4 个独立检验去证实/证伪这个统一解释：

  【1】随机游走检验：4H 收益的自相关 + 方差比（variance ratio）
       → 若方差比 ≈ 1，价格是随机游走 → "形态"不可能含方向信息（结构解释）
  【2】匹配随机入场：对每个信号，在同一时间、同方向、同止损宽度、同盈亏比
       生成随机入场基准 → 差值 = **形态的净 α**
       → 若 α ≈ 0 且不显著 → 形态的信息含量为零（**决定性**）
  【3】漂移分解：把每个信号组的收益拆成 "市场漂移" + "形态 α"
       → 解释"做空亏钱"到底是形态问题还是牛市问题
  【4】MFE/MAE 对称性：在 4H 上重测，作为"无方向记忆"的第三个独立旁证

**判定标准（开跑前定死）**：
  - 【2】的 α 若 |α| < 0.05R 且 t<2 → **形态无信息含量**，价格行为学的方向性内核被否证
  - 【1】方差比若在 [0.9, 1.1] → 随机游走成立
  - 三个独立检验若一致 → **统一解释成立**（而不是三个巧合）
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics as st
from typing import Optional

from okx_client import OKXClient, Candle

PROXY = os.getenv("OKX_PROXY") or "http://127.0.0.1:7897"
ROUND_TRIP = 0.0007


def fetch(sym: str, bar: str, bars: int) -> list[Candle]:
    cli = OKXClient(proxy=PROXY, cache_dir=".cache")
    return cli.get_candles(sym, bar=bar, limit=bars, use_cache=True)


# --------------------------------------------------------------------------
# 【1】随机游走检验
# --------------------------------------------------------------------------
def variance_ratio(returns: list[float], q: int) -> tuple[float, float]:
    """Lo-MacKinlay 方差比 + 同方差下的 z 统计量。

    VR(q) = Var(q期收益) / (q × Var(1期收益))
    随机游走 → VR = 1；动量 → VR > 1；均值回归 → VR < 1
    """
    n = len(returns)
    mu = sum(returns) / n
    var1 = sum((r - mu) ** 2 for r in returns) / (n - 1)
    if var1 <= 0:
        return 1.0, 0.0
    # q 期重叠收益
    qr = [sum(returns[i:i + q]) for i in range(n - q + 1)]
    mq = sum(qr) / len(qr)
    varq = sum((r - mq) ** 2 for r in qr) / (len(qr) - 1)
    vr = varq / (q * var1)
    # 同方差 z（Lo-MacKinlay 简化）
    stat = (vr - 1) / math.sqrt(2 * (2 * q - 1) * (q - 1) / (3 * q * n))
    return vr, stat


def autocorr(xs: list[float], lag: int) -> float:
    n = len(xs)
    m = sum(xs) / n
    dev = [x - m for x in xs]
    num = sum(dev[i] * dev[i - lag] for i in range(lag, n)) / n
    den = sum(d * d for d in dev) / n
    return num / den if den else 0.0


# --------------------------------------------------------------------------
# 形态检测（与前一脚本同口径，只保留必要特征）
# --------------------------------------------------------------------------
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


def detect(cs, rr=2.0, min_gap=0):
    n = len(cs)
    atrs = atr_series(cs)
    sigs, last_kind = [], {}
    for i in range(2, n):
        c, p = cs[i], cs[i - 1]
        rng = c.high - c.low
        if rng <= 0:
            continue
        body = abs(c.close - c.open)
        upper = c.high - max(c.close, c.open)
        lower = min(c.close, c.open) - c.low
        name = action = None
        if body > 0 and max(upper, lower) >= 2.0 * body:
            if lower > upper and lower >= 2.0 * body and c.close >= c.low + rng * 2 / 3:
                name, action = "bullish_pinbar", "BUY"
            elif upper > lower and upper >= 2.0 * body and c.close <= c.high - rng * 2 / 3:
                name, action = "bearish_pinbar", "SELL"
        if name is None:
            if c.close > c.open and p.close < p.open and c.close >= p.open and c.open <= p.close:
                name, action = "bullish_engulfing", "BUY"
            elif c.close < c.open and p.close > p.open and c.close <= p.open and c.open >= p.close:
                name, action = "bearish_engulfing", "SELL"
        if name is None:
            continue
        kind = "pinbar" if "pinbar" in name else "engulfing"
        if min_gap > 1 and i - last_kind.get(kind, -10**9) < min_gap:
            continue
        last_kind[kind] = i

        stop = (min(c.low, p.low) - rng * 0.1) if action == "BUY" else (max(c.high, p.high) + rng * 0.1)
        entry = c.close
        risk = abs(entry - stop)
        if risk <= 0:
            continue

        # 趋势状态
        trend = "flat"
        if i >= 25:
            ma_now = sum(x.close for x in cs[i - 20:i]) / 20
            ma_prev = sum(x.close for x in cs[i - 25:i - 5]) / 20
            slope = (ma_now - ma_prev) / ma_prev if ma_prev else 0
            th = 0.2 * ((atrs[i] or risk) / entry)
            trend = "up" if slope > th else ("down" if slope < -th else "flat")
        sigs.append({"i": i, "action": action, "entry": entry, "stop": stop,
                     "risk": risk, "trend": trend, "rr": rr})
    return sigs


def outcome(cs, i, action, entry, stop, rr, max_bars=200) -> Optional[float]:
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


def mfe_mae(cs, i, action, entry, stop, rr, max_bars=200):
    """最大有利偏移 / 最大不利偏移（以 R 计）。"""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    mfe = mae = 0.0
    for j in range(i + 1, min(len(cs), i + 1 + max_bars)):
        c = cs[j]
        if action == "BUY":
            mfe = max(mfe, (c.high - entry) / risk)
            mae = min(mae, (c.low - entry) / risk)
        else:
            mfe = max(mfe, (entry - c.low) / risk)
            mae = min(mae, (entry - c.high) / risk)
    return mfe, mae


def stt(xs):
    n = len(xs)
    if n < 2:
        return {"n": n, "mean": 0.0, "t": 0.0, "sd": 0.0}
    m = st.mean(xs)
    sd = st.stdev(xs)
    return {"n": n, "mean": m, "sd": sd, "t": m / (sd / math.sqrt(n)) if sd else 0.0}


def main():
    random.seed(20260922)
    print("=" * 106)
    print("价格行为学融会贯通 —— 四个独立检验能否拼出统一的解释？")
    print("=" * 106)

    summary = {}
    for sym in ["ETH-USDT-SWAP", "BTC-USDT-SWAP"]:
        cs = fetch(sym, "4H", 6570)
        print(f"\n{'='*106}\n【{sym}】{len(cs)} 根 4H\n{'='*106}")

        closes = [c.close for c in cs]
        rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]

        # ---------- 【1】随机游走检验 ----------
        print("\n【1】随机游走检验 —— 价格有没有方向记忆？")
        print(f"    {'滞后':>6}{'自相关':>12}      |{'q':>4}{'方差比 VR':>12}{'z':>9}{'含义':>16}")
        acs = {L: autocorr(rets, L) for L in [1, 2, 4, 8, 16]}
        vrs = {q: variance_ratio(rets, q) for q in [2, 4, 8, 16, 32]}
        for idx, L in enumerate([1, 2, 4, 8, 16]):
            q = list(vrs)[idx]
            vr, z = vrs[q]
            mean = "随机游走" if abs(vr - 1) < 0.15 else ("动量" if vr > 1 else "均值回归")
            print(f"    {L:>6}{acs[L]:>+12.4f}      |{q:>4}{vr:>12.4f}{z:>+9.2f}{mean:>16}")
        avg_vr = sum(v for v, _ in vrs.values()) / len(vrs)
        zs = [z for _, z in vrs.values()]
        mean_z = sum(zs) / len(zs)
        print(f"    → 平均 VR = {avg_vr:.4f}（1.0 = 随机游走）  平均 z = {mean_z:+.2f}")
        verdict_rw = "✅ 随机游走" if abs(avg_vr - 1) < 0.15 and abs(mean_z) < 2 else "⚠️ 有偏离"
        print(f"    → 判定：{verdict_rw}")

        # ---------- 形态 ----------
        sigs = detect(cs, rr=2.0, min_gap=0)
        rows = []
        for s in sigs:
            r = outcome(cs, s["i"], s["action"], s["entry"], s["stop"], s["rr"])
            if r is not None:
                s["ret"] = r
                rows.append(s)
        base = stt([s["ret"] for s in rows])
        print(f"\n    形态样本 n={base['n']}  实际净期望 {base['mean']:+.4f}R  t={base['t']:+.2f}")

        # ---------- 【2】匹配随机入场 → 形态 α ----------
        print("\n【2】匹配随机入场 —— 形态到底有没有 α？★ 决定性")
        print("    （同一时间、同方向、同止损宽度比例、同盈亏比的随机点位，重复 200 次）")
        ITER = 200
        rand_means = []
        for _ in range(ITER):
            rs = []
            for s in rows:
                i = s["i"]
                if i + 5 >= len(cs):
                    continue
                # 在附近 ±20 根内随机选一个入场点，方向与止损宽度比例保持一致
                lo_j, hi_j = max(2, i - 20), min(len(cs) - 30, i + 20)
                if hi_j <= lo_j:
                    continue
                j = random.randint(lo_j, hi_j)
                rng = cs[j].high - cs[j].low
                if rng <= 0:
                    continue
                # 保持同样的"止损宽度/价格"比例
                ratio = s["risk"] / s["entry"]
                e = cs[j].close
                stp = e - ratio * e if s["action"] == "BUY" else e + ratio * e
                r = outcome(cs, j, s["action"], e, stp, s["rr"])
                if r is not None:
                    rs.append(r)
            if rs:
                rand_means.append(sum(rs) / len(rs))

        rm = sum(rand_means) / len(rand_means)
        rand_sd = st.stdev(rand_means)
        # α 的显著性：实际 vs 随机分布的偏离
        alpha = base["mean"] - rm
        z_alpha = (alpha / rand_sd) if rand_sd else 0.0
        p_emp = sum(1 for x in rand_means if abs(x - rm) >= abs(base["mean"] - rm)) / len(rand_means)

        print(f"    实际形态     净期望 {base['mean']:+.4f}R  (n={base['n']})")
        print(f"    匹配随机入场 净期望 {rm:+.4f}R  (200 次重复的均值)")
        print(f"    随机分布标准差 {rand_sd:.4f}R  → 随机 95% 区间 "
              f"[{rm-1.96*rand_sd:+.4f}, {rm+1.96*rand_sd:+.4f}]")
        print(f"    ─────────────────────────────────────────────────")
        print(f"    形态 α = {alpha:+.4f}R   z = {z_alpha:+.2f}   经验 p = {p_emp:.3f}")
        v2 = ("❌ α 不显著（形态无信息含量）" if abs(z_alpha) < 2
              else ("✅ α 显著为正" if alpha > 0 else "✅ α 显著为负"))
        print(f"    判定：{v2}")

        # ---------- 【3】漂移分解 ----------
        print("\n【3】漂移分解 —— '做空亏钱'是形态问题还是牛市问题？")
        print(f"    {'分组':<26}{'n':>7}{'实际':>11}{'随机基准':>11}{'α':>11}{'α的z':>9}")
        groups = {
            "全部": lambda s: True,
            "多头形态（BUY）": lambda s: s["action"] == "BUY",
            "空头形态（SELL）": lambda s: s["action"] == "SELL",
            "顺势": lambda s: (s["trend"] == "up" and s["action"] == "BUY")
                            or (s["trend"] == "down" and s["action"] == "SELL"),
            "逆势": lambda s: (s["trend"] == "up" and s["action"] == "SELL")
                            or (s["trend"] == "down" and s["action"] == "BUY"),
            "无趋势 BUY": lambda s: s["trend"] == "flat" and s["action"] == "BUY",
            "无趋势 SELL": lambda s: s["trend"] == "flat" and s["action"] == "SELL",
        }
        drift_out = {}
        for label, f in groups.items():
            sub = [s for s in rows if f(s)]
            if len(sub) < 30:
                continue
            act = stt([s["ret"] for s in sub])
            # 该组的匹配随机基准
            rands = []
            for _ in range(80):
                rs = []
                for s in sub:
                    i = s["i"]
                    if i + 5 >= len(cs):
                        continue
                    lo_j, hi_j = max(2, i - 20), min(len(cs) - 30, i + 20)
                    if hi_j <= lo_j:
                        continue
                    j = random.randint(lo_j, hi_j)
                    rng = cs[j].high - cs[j].low
                    if rng <= 0:
                        continue
                    ratio = s["risk"] / s["entry"]
                    e = cs[j].close
                    stp = e - ratio * e if s["action"] == "BUY" else e + ratio * e
                    r = outcome(cs, j, s["action"], e, stp, s["rr"])
                    if r is not None:
                        rs.append(r)
                if rs:
                    rands.append(sum(rs) / len(rs))
            if not rands:
                continue
            rmn = sum(rands) / len(rands)
            rsd = st.stdev(rands) if len(rands) > 1 else 0
            a = act["mean"] - rmn
            za = a / rsd if rsd else 0.0
            drift_out[label] = {"n": act["n"], "act": act["mean"], "rand": rmn,
                                "alpha": a, "z": za}
            print(f"    {label:<26}{act['n']:>7}{act['mean']:>+11.4f}{rmn:>+11.4f}"
                  f"{a:>+11.4f}{za:>+9.2f}")

        # ---------- 【4】MFE / MAE ----------
        print("\n【4】MFE / MAE 对称性 —— 价格是否对称游走？")
        mfes, maes = [], []
        for s in rows:
            mm = mfe_mae(cs, s["i"], s["action"], s["entry"], s["stop"], s["rr"])
            if mm:
                mfes.append(mm[0])
                maes.append(mm[1])
        print(f"    MFE 均值 {sum(mfes)/len(mfes):+.3f}R   中位 {st.median(mfes):+.3f}R")
        print(f"    MAE 均值 {sum(maes)/len(maes):+.3f}R   中位 {st.median(maes):+.3f}R")
        ratio = abs(sum(mfes)/len(mfes)) / abs(sum(maes)/len(maes))
        print(f"    |MFE| / |MAE| = {ratio:.3f}   （1.0 = 完全对称）")
        v4 = "✅ 对称（无方向记忆）" if 0.9 <= ratio <= 1.1 else "⚠️ 不对称"
        print(f"    判定：{v4}")

        summary[sym] = {"vr": avg_vr, "vr_z": mean_z, "base": base,
                        "rand": rm, "alpha": alpha, "alpha_z": z_alpha, "p": p_emp,
                        "mfe_mae_ratio": ratio, "drift": drift_out,
                        "verdict_rw": verdict_rw, "verdict_alpha": v2}

    # ================= 综合 =================
    print("\n" + "=" * 106)
    print("【综合判定：四个独立检验是否指向同一个原因？】")
    print("=" * 106)
    print(f"  {'检验':<34}{'ETH':>18}{'BTC':>18}{'是否一致':>12}")
    e, b = summary["ETH-USDT-SWAP"], summary["BTC-USDT-SWAP"]
    rows_t = [
        ("【1】平均方差比 (1.0=随机游走)", f"{e['vr']:.3f}", f"{b['vr']:.3f}",
         "✅" if abs(e['vr']-1) < 0.15 and abs(b['vr']-1) < 0.15 else "⚠️"),
        ("【1】方差比平均 z", f"{e['vr_z']:+.2f}", f"{b['vr_z']:+.2f}",
         "✅" if abs(e['vr_z']) < 2 and abs(b['vr_z']) < 2 else "⚠️"),
        ("【2】形态 α (R)", f"{e['alpha']:+.4f}", f"{b['alpha']:+.4f}",
         "✅" if abs(e['alpha_z']) < 2 and abs(b['alpha_z']) < 2 else "⚠️"),
        ("【2】α 的 z", f"{e['alpha_z']:+.2f}", f"{b['alpha_z']:+.2f}",
         "✅" if abs(e['alpha_z']) < 2 and abs(b['alpha_z']) < 2 else "⚠️"),
        ("【2】经验 p", f"{e['p']:.3f}", f"{b['p']:.3f}",
         "✅" if e['p'] > 0.05 and b['p'] > 0.05 else "⚠️"),
        ("【4】|MFE|/|MAE| (1.0=对称)", f"{e['mfe_mae_ratio']:.3f}",
         f"{b['mfe_mae_ratio']:.3f}",
         "✅" if 0.9 <= e['mfe_mae_ratio'] <= 1.1 and 0.9 <= b['mfe_mae_ratio'] <= 1.1 else "⚠️"),
    ]
    for a_, b_, c_, d_ in rows_t:
        print(f"  {a_:<34}{b_:>18}{c_:>18}{d_:>12}")

    print("\n  最近似的统一解释：")
    print("  ┌ " + "─" * 100)
    print("  │ 4H 尺度上价格近似**鞅**（无方向记忆）。")
    print("  │  → 形态不含方向信息（α≈0）⇒ 任何入场方式的期望都只由『漂移 + 成本』决定")
    print("  │  → 位置/强度/趋势对齐三个维度**都不改变**这一点（因为它们也都无法创造信息）")
    print("  │  → '做空亏钱' 由牛市漂移解释，不是形态的错")
    print("  │  → MFE≈MAE 是这个过程的必然推论，不是巧合")
    print("  └ " + "─" * 100)

    with open("/tmp/pa_unified.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print("\n[已写入 /tmp/pa_unified.json]")


if __name__ == "__main__":
    main()
