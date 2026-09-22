"""摩擦成本敏感性：加上滑点与永续资金费后，策略还剩多少？

此前所有结论都建立在"往返 0.070%"这一个假设上。但真实交易还有两笔成本：
  ① 滑点：止盈/止损触发后市价平，成交价劣于理论价
  ② 资金费：永续合约每 8 小时结算一次，持仓期间持续发生

用法：
    OKX_PROXY=http://127.0.0.1:1087 .venv/bin/python revalidate_frictions.py
    OKX_PROXY=http://127.0.0.1:1087 .venv/bin/python revalidate_frictions.py --sym BTC-USDT-SWAP
"""
from __future__ import annotations

import argparse
import math
import os
import random
import statistics as st
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from okx_client import OKXClient  # noqa: E402
from patterns import detect_engulfing, detect_pinbar  # noqa: E402
from revalidate_eth4h import FEE, apply_gap, fetch  # noqa: E402

BAR_HOURS = 4.0          # 4H K 线
FUNDING_PER_DAY = 0.0003  # 0.03%/天 = 每 8h 0.01%（ETH/BTC 长期均值量级）
MAX_BARS = 400


def outcome(c, idx, is_buy, entry, stop, tp, max_bars=MAX_BARS):
    """复刻项目判定逻辑，但额外返回持仓根数。"""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for j in range(idx + 1, min(len(c), idx + 1 + max_bars)):
        x = c[j]
        if is_buy:
            if x.low <= stop:
                return -1.0, "L", j
            if x.high >= tp:
                return abs(tp - entry) / risk, "W", j
        else:
            if x.high >= stop:
                return -1.0, "L", j
            if x.low <= tp:
                return abs(tp - entry) / risk, "W", j
    return 0.0, "O", min(len(c), idx + 1 + max_bars)


def collect_full(c, rr=2.0):
    """与 revalidate_eth4h.collect 一致，但记录方向与持仓根数。"""
    out = []
    for idx in range(60, len(c)):
        cur = c[idx]
        for pat in (detect_pinbar(cur, rr=rr), detect_engulfing(c[idx - 1], cur, rr=rr)):
            if pat is None:
                continue
            is_buy = pat.action == "BUY"
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
            r, res, exit_j = oc
            out.append({
                "r": r, "res": res, "ts": cur.ts, "stop_pct": risk / px,
                "kind": "pb" if "pinbar" in pat.name else "en",
                "is_buy": is_buy, "hold": max(1, exit_j - j),
            })
    return out


def evaluate(tr, slip=0.0, daily_funding=0.0, funding_mode="all", min_closed=6):
    """把滑点与资金费折成 R 后，重算净期望与显著性。

    slip            : 单边滑点（占价格比例），只作用于市价出场
    daily_funding   : 每日资金费（占名义比例）
    funding_mode    : "all" 所有持仓都付（最保守） / "directional" 仅多头付、空头收
    """
    cl = [t for t in tr if t["res"] != "O"]
    if len(cl) < min_closed:
        return None
    med = st.median([t["stop_pct"] for t in tr])
    if med <= 0:
        return None

    f_fee = FEE / med
    f_slip = slip / med
    xs = []
    for t in cl:
        days = t["hold"] * BAR_HOURS / 24.0
        f_fund = daily_funding * days / med
        if funding_mode == "directional":
            # 多头付、空头收（正资金费常态）→ 净成本 = (多头笔数-空头笔数) 分摊
            f_fund = f_fund if t["is_buy"] else -f_fund
        xs.append(t["r"] - f_fee - f_slip - f_fund)

    m = sum(xs) / len(xs)
    se = st.pstdev(xs) / math.sqrt(len(xs))
    rng = random.Random(7)
    bs = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(4000))
    return {
        "n": len(cl), "net": m, "se": se, "t": m / se if se else 0.0,
        "lo": bs[100], "hi": bs[3900],
        "hold_med": st.median([t["hold"] for t in cl]),
        "days": st.median([t["hold"] for t in cl]) * BAR_HOURS / 24.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="ETH-USDT-SWAP")
    ap.add_argument("--bar", default="4H")
    ap.add_argument("--bars", type=int, default=6570)
    ap.add_argument("--proxy", default=os.getenv("OKX_PROXY"))
    ap.add_argument("--gaps", default="4,8,12,20,40")
    a = ap.parse_args()

    print("=" * 104)
    print(f"摩擦成本敏感性：{a.sym} {a.bar} 突破 · 结构止损 · 无趋势")
    print("=" * 104)

    c = fetch(a.sym, a.bar, a.bars, a.proxy)
    tr = collect_full(c)
    gaps = [int(x) for x in a.gaps.split(",")]

    print(f"\n  原始信号 {len(tr)} 个；样例持仓中位 "
          f"{st.median([t['hold'] for t in collect_full(c) if t['res'] != 'O'])*0.1667:.1f} 天")

    scenarios = [
        ("基准：仅手续费 0.070%",        0.0,    0.0,    "all"),
        ("+滑点 0.01%",                  0.0001, 0.0,    "all"),
        ("+滑点 0.03%",                  0.0003, 0.0,    "all"),
        ("+资金费 0.03%/天（保守：都付）", 0.0,    0.0003, "all"),
        ("+资金费（实际：多付空收）",      0.0,    0.0003, "directional"),
        ("**全叠加：滑点0.03% + 资金费0.03%/天（都付）**", 0.0003, 0.0003, "all"),
        ("压力：滑点0.05% + 资金费0.06%/天", 0.0005, 0.0006, "all"),
    ]

    for g in gaps:
        seg = apply_gap(tr, g)
        print(f"\n  ── 间隔 {g}（{len(seg)} 笔） " + "─" * 50)
        print(f"     {'情景':<44}{'净期望':>10}{'SE':>8}{'t':>7}{'95%CI 下界':>12}{'判定':>8}")
        for name, slip, fund, mode in scenarios:
            r = evaluate(seg, slip=slip, daily_funding=fund, funding_mode=mode)
            if r is None:
                print(f"     {name:<44}{'样本不足':>10}")
                continue
            ok = (abs(r["t"]) > 2) and (r["lo"] > 0)
            print(f"     {name:<44}{r['net']:>+10.4f}{r['se']:>8.4f}"
                  f"{r['t']:>+7.2f}{r['lo']:>+12.4f}{'✅ 显著' if ok else '❌ 不显著':>8}")

    print("\n" + "=" * 104)
    print("  判定：|t|>2 且 Bootstrap 95%CI 下界>0 才算显著。")
    print("  注：资金费'都付'是最保守口径；实际正资金费下多头付、空头收，部分自然对冲。")
    print("=" * 104)


if __name__ == "__main__":
    main()
