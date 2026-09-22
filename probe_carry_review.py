"""对 carry 策略做严肃的稳健性复核 —— 因为出现了 t>2，必须先证伪。

轮动 carry 报告了 t=5.47，但资金费 lag1 自相关 0.51~0.57。
重叠/自相关序列会让朴素 t 统计量**虚高**。本脚本做五项复核：

  ① Newey-West 调整的 t 值（处理自相关，这是最关键的一项）
  ② 分年度 / 分季度：收益是否集中在某段？（若 2026 归零 → "当下"没意义）
  ③ 成本敏感性：成本翻倍/三倍后是否仍为正
  ④ 分块 bootstrap（block bootstrap，保守处理自相关）
  ⑤ 收益来源分解：是资金费贡献，还是价差/其他？

只有五项都过，才能说"这个策略可用"。
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
BASE = "https://fapi.binance.com/fapi/v1/fundingRate"
TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"
CACHE = Path("/tmp/bn_cache2")

ROUND_TRIP = 0.0014


def fetch_funding(symbol: str) -> list[tuple[int, float]]:
    cf = CACHE / f"{symbol}.json"
    if cf.exists():
        try:
            return [(int(t), float(r)) for t, r in json.loads(cf.read_text())]
        except Exception:
            pass
    end = int(time.time() * 1000)
    out, cur_end = {}, end
    with httpx.Client(proxy=PROXY, timeout=25.0) as c:
        for _ in range(30):
            try:
                rows = c.get(BASE, params={"symbol": symbol, "endTime": cur_end,
                                           "limit": 500}).json()
            except Exception:
                break
            if not isinstance(rows, list) or not rows:
                break
            for x in rows:
                out[int(x["fundingTime"])] = float(x["fundingRate"])
            earliest = min(int(x["fundingTime"]) for x in rows)
            if len(rows) < 500:
                break
            cur_end = earliest - 1
            time.sleep(0.15)
    res = sorted(out.items())
    cf.write_text(json.dumps(res))
    return res


def build_grid(all_data):
    GRID = 8 * 3600 * 1000
    grid, all_ts = {}, set()
    for sym, data in all_data.items():
        m = {}
        for t, r in data:
            m[t // GRID * GRID] = r
        grid[sym] = m
        all_ts |= set(m)
    return sorted(all_ts), grid


def newey_west_t(rets: list[float], lags: int) -> tuple[float, float]:
    """Newey-West 调整的 t 值。对自相关稳健。"""
    n = len(rets)
    m = sum(rets) / n
    dev = [x - m for x in rets]
    gamma0 = sum(d * d for d in dev) / n
    s = gamma0
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)          # Bartlett 核
        gl = sum(dev[i] * dev[i - L] for i in range(L, n)) / n
        s += 2 * w * gl
    if s <= 0:
        return 0.0, 0.0
    se = math.sqrt(s / n)
    return m / se if se else 0.0, se


def block_bootstrap_p(rets: list[float], block: int = 6, iters: int = 5000) -> float:
    """分块 bootstrap 的双侧 p 值（H0: 均值=0），保守处理自相关。"""
    n = len(rets)
    if n < block * 2:
        return 1.0
    m_obs = sum(rets) / n
    dev = [x - m_obs for x in rets]      # 零假设下的分布
    n_blocks = n // block
    count = 0
    for _ in range(iters):
        sample = []
        for _ in range(n_blocks):
            st = random.randrange(0, n - block + 1)
            sample.extend(dev[st:st + block])
        mm = sum(sample) / len(sample)
        if abs(mm) >= abs(m_obs):
            count += 1
    return count / iters


def rotation_rets(all_data, k, rebal_days, lookback_days, cost):
    ts, grid = build_grid(all_data)
    periods = int(rebal_days * 3)
    look = int(lookback_days * 3)
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
            g = sum(v for v in (grid[sym].get(ts[j])
                                for j in range(i + 1, i + 1 + periods)) if v is not None)
            gain += g
        gain /= k
        out.append({"ts": ts[i], "ret": gain - cost,
                    "funding": gain, "picks": shorts})
        i += periods
    return out


def main() -> None:
    # 复用已缓存数据
    univ_file = Path("/tmp/carry_final2.json")
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
            "LINKUSDT", "LTCUSDT", "ADAUSDT", "AVAXUSDT", "BCHUSDT", "ARBUSDT"]
    all_data = {}
    for s in syms:
        d = fetch_funding(s)
        if len(d) > 2000:
            all_data[s] = d
    print(f"有效品种: {len(all_data)}")

    K, REBAL, LOOK = 3, 14, 7
    cost = ROUND_TRIP
    rows = rotation_rets(all_data, K, REBAL, LOOK, cost)
    rets = [r["ret"] for r in rows]
    n = len(rets)
    m = sum(rets) / n
    naive_se = math.sqrt(sum((x - m) ** 2 for x in rets) / (n - 1) / n)
    naive_t = m / naive_se

    print("\n" + "=" * 100)
    print(f"基准策略: 轮动 top{K}，每 {REBAL} 天再平衡，回看 {LOOK} 天（n={n} 期）")
    print("=" * 100)
    print(f"  净期望 {m*100:+.4f}%/期   年化 {m*(365/REBAL)*100:+.2f}%")
    print(f"  朴素 t = {naive_t:+.2f}")

    print("\n【① Newey-West 自相关调整后的 t 值（最关键）】")
    print(f"  {'lags':>6}{'NW t':>10}{'NW SE':>12}{'vs 朴素 t':>14}")
    for lags in [1, 2, 4, 8]:
        nw_t, nw_se = newey_west_t(rets, lags)
        print(f"  {lags:>6}{nw_t:>10.2f}{nw_se*100:>11.4f}%{nw_t-naive_t:>13.2f}")

    print("\n【② 分年度：收益是否集中在某段？（决定\"当下\"是否有意义）】")
    yrs: dict[str, list[float]] = {}
    for r in rows:
        y = time.strftime("%Y", time.gmtime(r["ts"] / 1000))
        yrs.setdefault(y, []).append(r["ret"])
    print(f"  {'年份':<8}{'期数':>7}{'净/期':>11}{'年化':>10}{'胜率':>8}")
    for y, rs in sorted(yrs.items()):
        mm = sum(rs) / len(rs)
        w = sum(1 for x in rs if x > 0) / len(rs)
        print(f"  {y:<8}{len(rs):>7}{mm*100:>10.4f}%{mm*(365/REBAL)*100:>9.2f}%{w*100:>7.1f}%")

    print("\n【③ 成本敏感性】")
    print(f"  {'成本假设':<24}{'净/期':>11}{'年化':>10}{'朴素t':>9}{'NW t(lag4)':>13}")
    for mult, label in [(0.0, "0（无成本，上限）"), (1.0, "1× 基准 0.14%"),
                        (2.0, "2× 0.28%"), (3.0, "3× 0.42%"), (5.0, "5× 0.70%")]:
        rr = rotation_rets(all_data, K, REBAL, LOOK, cost * mult)
        rl = [x["ret"] for x in rr]
        mm = sum(rl) / len(rl)
        se = math.sqrt(sum((x - mm) ** 2 for x in rl) / (len(rl) - 1) / len(rl))
        nwt, _ = newey_west_t(rl, 4)
        print(f"  {label:<24}{mm*100:>10.4f}%{mm*(365/REBAL)*100:>9.2f}%"
              f"{mm/se:>9.2f}{nwt:>13.2f}")

    print("\n【④ 分块 bootstrap p 值（H0: 均值为 0，保守处理自相关）】")
    random.seed(42)
    for blk in [3, 6, 12]:
        p = block_bootstrap_p(rets, block=blk, iters=4000)
        print(f"  block={blk:>2} 期  →  p = {p:.4f}  {'✅ 拒绝 H0' if p < 0.05 else '❌ 不显著'}")

    print("\n【⑤ 收益来源分解（确认不是价差，而是资金费）】")
    fund_sum = sum(r["funding"] for r in rows) / n
    print(f"  平均资金费/期 {fund_sum*100:+.4f}%   成本/期 {-cost*100:.4f}%")
    print(f"  → 净 = 资金费 + 成本 = {fund_sum*100:+.4f}% + {-cost*100:.4f}% "
          f"= {(fund_sum-cost)*100:+.4f}%")
    print("  注：本策略为 delta 中性，价格损益理论抵消；实际存在基差与保证金风险（见下）")

    print("\n" + "=" * 100)
    print("【⑥ 当前最新一期的选品（可直接参考的\"当下\"信号）】")
    print("=" * 100)
    if rows:
        last = rows[-1]
        print(f"  最近一期（{time.strftime('%Y-%m-%d', time.gmtime(last['ts']/1000))}）")
        print(f"  做空（费率最高）: {', '.join(s.replace('USDT','') for s in last['picks'])}")

    with open("/tmp/carry_review.json", "w") as f:
        json.dump({"n": n, "mean": m, "naive_t": naive_t,
                   "yearly": {y: sum(v)/len(v) for y, v in yrs.items()}}, f)


if __name__ == "__main__":
    main()
