"""成本口径修正 —— 这是决定 carry 是否可做的最后一关。

背景：此前用 0.14%/次「双腿开平仓」作为成本，但那是**混用了 OKX 现货与永续费率**的
粗估。查 OKX 官方费率表后发现：

    现货（Regular）  maker 0.08%  taker 0.10%
    永续（Regular）  maker 0.02%  taker 0.05%

一次完整「双腿开平仓」= 现货开 + 现货平 + 永续开 + 永续平（4 笔成交）：

    全挂单(maker)：0.08×2 + 0.02×2 = 0.20%
    全吃单(taker)：0.10×2 + 0.05×2 = 0.30%
    现货吃单 + 永续挂单：0.10×2 + 0.02×2 = 0.24%

→ **真实成本是 0.20~0.30%，不是我之前用的 0.14%（低估 43%~114%）。**

本脚本用真实费率重算所有关键量，并做阶梯敏感性。
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from pathlib import Path

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
CACHE = Path("/tmp/bn_cache2")
UNIVERSE_FILE = Path("/tmp/carry_final2.json")

# ---- 真实费率（OKX Regular 用户，全局标准档）----
SPOT_MAKER, SPOT_TAKER = 0.0008, 0.0010
PERP_MAKER, PERP_TAKER = 0.0002, 0.0005


def leg_cost(spot_maker: bool, perp_maker: bool) -> float:
    """一次完整双腿开平仓的费用率。"""
    s = SPOT_MAKER if spot_maker else SPOT_TAKER
    p = PERP_MAKER if perp_maker else PERP_TAKER
    return 2 * s + 2 * p


SCENARIOS = {
    "全挂单 maker（理想）": leg_cost(True, True),       # 0.20%
    "现货吃单+永续挂单": leg_cost(False, True),          # 0.24%
    "全吃单 taker（最差）": leg_cost(False, False),      # 0.30%
}


def load(sym: str):
    f = CACHE / f"{sym}.json"
    if not f.exists():
        return []
    return [(int(t), float(r)) for t, r in json.loads(f.read_text())]


def newey_west_t(rets, lags):
    n = len(rets)
    m = sum(rets) / n
    dev = [x - m for x in rets]
    g0 = sum(d * d for d in dev) / n
    s = g0
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        gl = sum(dev[i] * dev[i - L] for i in range(L, n)) / n
        s += 2 * w * gl
    if s <= 0:
        return 0.0
    return m / math.sqrt(s / n)


def block_bootstrap_p(rets, block=6, iters=4000, seed=42):
    random.seed(seed)
    n = len(rets)
    if n < block * 2:
        return 1.0
    m_obs = sum(rets) / n
    dev = [x - m_obs for x in rets]
    nb = n // block
    cnt = 0
    for _ in range(iters):
        smp = []
        for _ in range(nb):
            st = random.randrange(0, n - block + 1)
            smp.extend(dev[st:st + block])
        if abs(sum(smp) / len(smp)) >= abs(m_obs):
            cnt += 1
    return cnt / iters


def build_grid(all_data):
    G = 8 * 3600 * 1000
    grid, all_ts = {}, set()
    for sym, d in all_data.items():
        m = {}
        for t, r in d:
            m[t // G * G] = r
        grid[sym] = m
        all_ts |= set(m)
    return sorted(all_ts), grid


def rotation(all_data, k, rebal_days, lookback_days, cost):
    ts, grid = build_grid(all_data)
    periods, look = int(rebal_days * 3), int(lookback_days * 3)
    out = []
    i = look
    while i + periods < len(ts):
        scores = {}
        for sym, m in grid.items():
            vals = [v for v in (m.get(ts[j]) for j in range(i - look, i)) if v is not None]
            if len(vals) >= look * 0.7:
                scores[sym] = sum(vals) / len(vals)
        if len(scores) < k + 2:
            i += periods
            continue
        ranked = sorted(scores, key=lambda s: scores[s])
        shorts = ranked[-k:]
        gain = 0.0
        for sym in shorts:
            g = 0.0
            for j in range(i + 1, i + 1 + periods):
                v = grid[sym].get(ts[j])
                if v is not None:
                    g += v
            gain += g
        gain /= k
        out.append({"ts": ts[i], "net": gain - cost, "funding": gain})
        i += periods
    return out


def main():
    print("=" * 108)
    print("成本口径修正 —— OKX 真实费率下的 carry 重新评估")
    print("=" * 108)
    print(f"\n  OKX Regular 费率:  现货 maker {SPOT_MAKER*100:.2f}% / taker {SPOT_TAKER*100:.2f}%"
          f"   永续 maker {PERP_MAKER*100:.2f}% / taker {PERP_TAKER*100:.2f}%")
    print("\n  一次完整「双腿开平仓」（现货开+平，永续开+平，共 4 笔成交）:")
    for name, c in SCENARIOS.items():
        print(f"    {name:<24} {c*100:.3f}%")
    print(f"\n  ⚠️  此前使用的 0.14% 是低估值（比全挂单还低 43%，比全吃单低 114%）")

    # ---------------- ① ETH/BTC 单品种持有 ----------------
    print("\n" + "=" * 108)
    print("【① 单品种全程持有：不同成本档 × 不同再平衡周期】")
    print("=" * 108)
    for sym in ["ETHUSDT", "BTCUSDT"]:
        d = load(sym)
        if not d:
            continue
        rates = [r for _, r in d]
        gross = sum(rates) / len(rates) * 3 * 365
        pos = sum(1 for r in rates if r > 0) / len(rates)
        yrs = (d[-1][0] - d[0][0]) / 1000 / 86400 / 365
        print(f"\n  【{sym}】毛费率年化 {gross*100:+.2f}% · 正比例 {pos*100:.1f}% · 覆盖 {yrs:.2f} 年")
        print(f"    {'再平衡周期':<12}" + "".join(f"{n:>22}" for n in SCENARIOS))
        for hold in [7, 14, 30, 60, 90]:
            row = f"    {str(hold)+' 天':<12}"
            for n, c in SCENARIOS.items():
                net = gross - c * 365 / hold
                row += f"{net*100:>21.2f}%"
            print(row)

    # ---------------- ② 轮动：真实成本下的显著性 ----------------
    print("\n" + "=" * 108)
    print("【② 轮动 top3（14 天 / 回看 7 天）：真实成本下的显著性】")
    print("=" * 108)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
            "LINKUSDT", "LTCUSDT", "ADAUSDT", "AVAXUSDT", "BCHUSDT", "ARBUSDT",
            "UNIUSDT", "NEARUSDT", "SUIUSDT", "ENAUSDT", "TAOUSDT"]
    all_data = {s: load(s) for s in syms}
    all_data = {k: v for k, v in all_data.items() if len(v) > 2000}
    print(f"  有效品种 {len(all_data)} 个")
    print(f"\n  {'成本档':<24}{'成本':>9}{'净/期':>11}{'年化':>10}{'朴素t':>9}{'NW t(4)':>10}"
          f"{'NW t(8)':>10}{'bootstrap p':>13}")
    for name, c in [("0（无成本，上限）", 0.0)] + list(SCENARIOS.items()) + [("压力 2×（0.40%）", 0.0040)]:
        rows = rotation(all_data, 3, 14, 7, c)
        rl = [x["net"] for x in rows]
        n = len(rl)
        m = sum(rl) / n
        se = math.sqrt(sum((x - m) ** 2 for x in rl) / (n - 1) / n)
        p = block_bootstrap_p(rl, block=6, iters=3000)
        nw4, nw8 = newey_west_t(rl, 4), newey_west_t(rl, 8)
        flag = "✅" if nw4 > 2 else ("⚠️" if m > 0 else "❌")
        print(f"  {name:<24}{c*100:>8.3f}%{m*100:>10.4f}%{m*(365/14)*100:>9.2f}%"
              f"{m/se:>9.2f}{nw4:>10.2f}{nw8:>10.2f}{p:>13.4f}  {flag}")

    # ---------------- ③ ETH+BTC 等权（最简单的可执行方案） ----------------
    print("\n" + "=" * 108)
    print("【③ 最简方案：ETH+BTC 等权 delta 中性，不同成本 × 不同周期】")
    print("=" * 108)
    eth, btc = load("ETHUSDT"), load("BTCUSDT")
    ge = sum(r for _, r in eth) / len(eth) * 3 * 365
    gb = sum(r for _, r in btc) / len(btc) * 3 * 365
    gavg = (ge + gb) / 2
    print(f"  组合毛费率年化 = ({ge*100:.2f}% + {gb*100:.2f}%) / 2 = {gavg*100:+.2f}%")
    print(f"  最近 30 天：ETH {sum(r for _,r in eth[-90:])/90*3*365*100:+.2f}%  "
          f"BTC {sum(r for _,r in btc[-90:])/90*3*365*100:+.2f}%")
    print(f"\n  {'周期':<10}" + "".join(f"{n:>24}" for n in SCENARIOS))
    for hold in [14, 30, 60, 90]:
        row = f"  {str(hold)+' 天':<10}"
        for n, c in SCENARIOS.items():
            row += f"{(gavg - c*365/hold)*100:>23.2f}%"
        print(row)

    # ---------------- ④ 各本金年收益（用真实成本） ----------------
    print("\n" + "=" * 108)
    print("【④ 各本金年收益（ETH+BTC 等权，30 天换仓，全挂单成本）】")
    print("=" * 108)
    net30 = gavg - SCENARIOS["全挂单 maker（理想）"] * 365 / 30
    net60 = gavg - SCENARIOS["全挂单 maker（理想）"] * 365 / 60
    net30t = gavg - SCENARIOS["全吃单 taker（最差）"] * 365 / 30
    print(f"  净年化：全挂单30天 {net30*100:.2f}% · 全挂单60天 {net60*100:.2f}% · "
          f"全吃单30天 {net30t*100:.2f}%")
    print(f"\n  {'本金 USDT':>12}{'30天换仓':>12}{'60天换仓':>12}{'最差情形':>12}"
          f"{'月收益(60天)':>14}")
    for bal in [100, 500, 1000, 5000, 20000, 100000]:
        print(f"  {bal:>12,}{bal*net30:>12.2f}{bal*net60:>12.2f}{bal*net30t:>12.2f}"
              f"{bal*net60/12:>14.2f}")

    # ---------------- ⑤ 保本所需再平衡周期 ----------------
    print("\n" + "=" * 108)
    print("【⑤ 关键问题：成本要多低 / 持有期要多长，carry 才为正？】")
    print("=" * 108)
    print(f"  {'成本档':<24}{'保本周期(天)':>14}{'含义':>40}")
    for name, c in list(SCENARIOS.items()) + [("压力 0.40%", 0.0040)]:
        days = c * 365 / gavg
        note = "随时可换仓" if days < 7 else ("需持有 ≥1 周" if days < 14 else
                                          ("需持有 ≥1 月" if days < 45 else "周期过长，不可行"))
        print(f"  {name:<24}{days:>13.1f}{note:>40}")

    with open("/tmp/carry_cost.json", "w") as f:
        json.dump({"fee_model": {"spot": [SPOT_MAKER, SPOT_TAKER],
                                 "perp": [PERP_MAKER, PERP_TAKER]},
                   "scenarios": SCENARIOS,
                   "eth_btc_gross": gavg,
                   "net30_maker": net30, "net60_maker": net60,
                   "net30_taker": net30t}, f, indent=2)
    print("\n[已写入 /tmp/carry_cost.json]")


if __name__ == "__main__":
    main()
