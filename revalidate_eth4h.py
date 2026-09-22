"""独立复核脚本：拉取更长历史（默认 3 年）验证 ETH 4H 突破配置。

用法（需要能访问 OKX，必要时加代理）：

    # 直连
    .venv/bin/python revalidate_eth4h.py

    # 走代理（OKX 被墙时）
    OKX_PROXY=http://127.0.0.1:7890 .venv/bin/python revalidate_eth4h.py

    # 自定义：3 年 4H ≈ 6570 根；也可换 1H 做交叉验证
    .venv/bin/python revalidate_eth4h.py --bars 6570 --bar 4H

输出：净期望、区间稳健性（平台/尖峰）、分季度、随机入场检验、bootstrap 置信区间。
判定：只有"区间普遍为正 + 随机检验显著 + 各段符号一致"三项全过，才算通过独立复核。
"""
from __future__ import annotations

import argparse
import os
import random
import statistics as st
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from okx_client import OKXClient, Candle  # noqa: E402
from patterns import detect_engulfing, detect_pinbar  # noqa: E402
from volume import volume_ratio_at  # noqa: E402

FEE = 0.0007  # 挂单进 0.020% + 吃单出 0.050%


# ----------------------------- 数据 -----------------------------------------
def fetch(sym: str, bar: str, bars: int, proxy: str | None) -> list:
    cli = OKXClient(proxy=proxy)
    cs = cli.get_candles(sym, bar=bar, limit=bars)
    print(f"  拉到 {sym} {bar} {len(cs)} 根")
    return cs


# ----------------------------- 指标 -----------------------------------------
def atr_at(c, idx, period=14):
    if idx < period:
        return None
    trs = [max(c[j].high - c[j].low, abs(c[j].high - c[j - 1].close),
               abs(c[j].low - c[j - 1].close)) for j in range(idx - period + 1, idx + 1)]
    m = sum(trs) / len(trs)
    return m if m > 0 else None


def outcome(c, idx, is_buy, entry, stop, tp, max_bars=400):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for j in range(idx + 1, min(len(c), idx + 1 + max_bars)):
        x = c[j]
        if is_buy:
            if x.low <= stop:
                return -1.0, "L"
            if x.high >= tp:
                return abs(tp - entry) / risk, "W"
        else:
            if x.high >= stop:
                return -1.0, "L"
            if x.low <= tp:
                return abs(tp - entry) / risk, "W"
    return 0.0, "O"


def collect(c, rr=2.0, entry_mode="breakout"):
    """突破入场：3 根内价格越过形态极值则成交，否则作废。"""
    out = []
    for idx in range(60, len(c)):
        cur = c[idx]
        for pat in (detect_pinbar(cur, rr=rr), detect_engulfing(c[idx - 1], cur, rr=rr)):
            if pat is None:
                continue
            is_buy = pat.action == "BUY"
            if entry_mode == "close":
                j, px, stop = idx, pat.entry, pat.stop
            else:
                level = cur.high if is_buy else cur.low
                fill = None
                for jj in range(idx + 1, min(len(c), idx + 4)):
                    cc = c[jj]
                    if is_buy and cc.high >= level:
                        fill = (jj, level); break
                    if (not is_buy) and cc.low <= level:
                        fill = (jj, level); break
                if fill is None:
                    continue
                j, px, stop = fill[0], fill[1], pat.stop
            risk = abs(px - stop)
            if risk <= 0:
                continue
            tp = px + rr * risk if is_buy else px - rr * risk
            oc = outcome(c, j, is_buy, px, stop, tp)
            if oc is None:
                continue
            r, res = oc
            out.append({"r": r, "res": res, "ts": cur.ts, "stop_pct": risk / px,
                        "kind": "pb" if "pinbar" in pat.name else "en"})
    return out


def apply_gap(tr, gap):
    if gap <= 1:
        return list(tr)
    tr = sorted(tr, key=lambda t: t["ts"])
    last, out = {}, []
    for t in tr:
        k = t["kind"]
        if k not in last or t["ts"] - last[k] >= gap * 14_400_000:
            out.append(t); last[k] = t["ts"]
    return out


def net(tr):
    closed = [t for t in tr if t["res"] != "O"]
    if len(closed) < 25:
        return None, None
    gross = sum(t["r"] for t in closed) / len(closed)
    med = st.median([t["stop_pct"] for t in tr])
    return gross - FEE / med, gross


def rand_test(c, n, stop_pct, rr=2.0, trials=500, seed=11):
    rng = random.Random(seed)
    lo, hi = 60, len(c) - 401
    dist = []
    for _ in range(trials):
        rs = []
        for _ in range(n):
            i = rng.randint(lo, hi)
            buy = rng.random() < 0.5
            e = c[i].close
            rk = e * stop_pct
            sl = e - rk if buy else e + rk
            tp = e + rr * rk if buy else e - rr * rk
            for j in range(i + 1, min(len(c), i + 401)):
                x = c[j]
                if buy:
                    if x.low <= sl: rs.append(-1.0); break
                    if x.high >= tp: rs.append(rr); break
                else:
                    if x.high >= sl: rs.append(-1.0); break
                    if x.low <= tp: rs.append(rr); break
            else:
                rs.append(0.0)
        dist.append(sum(rs) / len(rs))
    return sorted(dist)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="ETH-USDT-SWAP")
    ap.add_argument("--bar", default="4H")
    ap.add_argument("--bars", type=int, default=6570, help="3 年 4H ≈ 6570")
    ap.add_argument("--proxy", default=os.getenv("OKX_PROXY"))
    a = ap.parse_args()

    print("=" * 104)
    print(f"独立复核：{a.sym} {a.bar} 突破入场 结构止损 无趋势   （目标 {a.bars} 根）")
    print("=" * 104)
    if not a.proxy:
        print("  提示：未设置代理。若 OKX 不可达，请用 OKX_PROXY=http://127.0.0.1:7890 重跑。")

    c = fetch(a.sym, a.bar, a.bars, a.proxy)
    trades = collect(c)

    print(f"\n  ① 最优间隔与区间稳健性（平台可信，尖峰可疑）")
    vals, best = [], (None, -9)
    for g in range(2, 41, 2):
        v, _ = net(apply_gap(trades, g))
        if v is None:
            continue
        vals.append(v)
        if v > best[1]:
            best = (g, v)
    if vals:
        pos = sum(1 for v in vals if v > 0)
        print(f"     正间隔 {pos}/{len(vals)} ({pos/len(vals)*100:.0f}%)  "
              f"平均 {sum(vals)/len(vals):+.4f}R  中位 {st.median(vals):+.4f}R  "
              f"最优间隔={best[0]} ({best[1]:+.4f}R)")
        print(f"     → {'✅ 平台，稳健' if pos/len(vals) >= 0.7 else '❌ 尖峰/震荡，不可信'}")

    print(f"\n  ② 分季度")
    tr = apply_gap(trades, best[0] or 8)
    t0 = tr[0]["ts"]
    q = 90 * 86_400_000
    nq = max(1, (tr[-1]["ts"] - t0) // q + 1)
    signs = []
    for k in range(int(nq)):
        seg = [t for t in tr if t0 + k * q <= t["ts"] < t0 + (k + 1) * q]
        v, _ = net(seg)
        if v is None:
            continue
        signs.append(v > 0)
        print(f"     第{k+1}季: 笔{len(seg):>4}  净期望 {v:+.4f}R  {'✅' if v > 0 else '❌'}")
    if signs:
        print(f"     → 正季度 {sum(signs)}/{len(signs)}  "
              f"{'✅ 符号一致' if all(signs) or not any(signs) else '⚠️ 符号不一致'}")

    print(f"\n  ③ 随机入场检验")
    stop_pct = st.median([t["stop_pct"] for t in tr])
    _, gross = net(tr)
    dist = rand_test(c, len(tr), stop_pct)
    mean_r = sum(dist) / len(dist)
    p = sum(1 for x in dist if abs(x - mean_r) >= abs(gross - mean_r)) / len(dist)
    print(f"     真实毛期望 {gross:+.4f}R   随机均值 {mean_r:+.4f}R   经验 p = {p:.3f}")
    print(f"     → {'✅ 显著优于随机' if p <= 0.05 else '❌ 与随机无显著差别'}")

    print(f"\n  ④ Bootstrap 95% 置信区间（10,000 次）")
    rng = random.Random(1)
    f = FEE / stop_pct
    xs = [t["r"] - f for t in tr]
    means = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(10000))
    lo_, hi_ = means[250], means[9750]
    print(f"     [{lo_:+.4f}, {hi_:+.4f}]  → {'✅ 下界>0' if lo_ > 0 else '❌ 下界<0'}")

    print("\n" + "=" * 104)
    print("  结论：三项（区间普遍为正 / 随机检验显著 / 各季度符号一致）全过才算通过独立复核。")
    print("=" * 104)


if __name__ == "__main__":
    main()
