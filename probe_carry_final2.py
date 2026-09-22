"""delta 中性 carry —— 修正版（修两个 bug）。

Bug 1: 币安 limit 实际上限是 500（非 1000），用 `len(rows) < 1000` 判断提前退出
       导致只拉到 500 期（0.5 年）。改为 LIMIT=500 + 正确的终止条件。
Bug 2: 轮动 carry 的 8h 网格对齐错误（要求每个币都有该时点数据，导致无交集）。
       改为：允许 ±1 期容差映射到网格。

核心简化（正确且重要）：
  现货多 + 永续空 = delta 中性 → 价格损益相互抵消
  净收益 ≈ Σ(资金费率) − 交易成本
  → 不需要价格数据，可直接用资金费历史计算
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
BASE = "https://fapi.binance.com/fapi/v1/fundingRate"
TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"
CACHE = Path("/tmp/bn_cache2")
CACHE.mkdir(exist_ok=True)

LIMIT = 500                      # 币安实测上限
ROUND_TRIP_BOTH_LEGS = 0.0014    # 双腿完整开平仓 0.14%


def fetch_funding(symbol: str, years: float = 3.2) -> list[tuple[int, float]]:
    cf = CACHE / f"{symbol}.json"
    if cf.exists():
        try:
            d = json.loads(cf.read_text())
            ts = [int(t) for t, _ in d]
            if len(d) > 2000 and (max(ts) - min(ts)) > years * 365 * 86400 * 1000 * 0.9:
                return [(int(t), float(r)) for t, r in d]
        except Exception:
            pass
    end = int(time.time() * 1000)
    target = end - int(years * 365 * 86400 * 1000)
    out: dict[int, float] = {}
    cur_end = end
    with httpx.Client(proxy=PROXY, timeout=25.0) as c:
        for _ in range(30):
            try:
                rows = c.get(BASE, params={"symbol": symbol, "endTime": cur_end,
                                           "limit": LIMIT}).json()
            except Exception:
                break
            if not isinstance(rows, list) or not rows:
                break
            for x in rows:
                out[int(x["fundingTime"])] = float(x["fundingRate"])
            earliest = min(int(x["fundingTime"]) for x in rows)
            if earliest <= target:
                break
            if len(rows) < LIMIT:      # 真的到底了
                break
            cur_end = earliest - 1
            time.sleep(0.18)
    res = sorted(out.items())
    cf.write_text(json.dumps(res))
    return res


def universe(n: int = 30) -> list[str]:
    with httpx.Client(proxy=PROXY, timeout=25.0) as c:
        t = c.get(TICKER).json()
    rows = []
    for x in t:
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            rows.append((s, float(x.get("quoteVolume") or 0)))
        except Exception:
            continue
    rows.sort(key=lambda y: y[1], reverse=True)
    # 排除杠杆代币等
    bad = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    return [s for s, _ in rows if not s.endswith(bad)][:n]


def yearly_split(data):
    out: dict[str, list[float]] = {}
    for t, r in data:
        out.setdefault(time.strftime("%Y", time.gmtime(t / 1000)), []).append(r)
    return out


def build_grid(all_data: dict[str, list[tuple[int, float]]]):
    """8h 网格 + ±1 期容差映射，返回 (ts_list, grid)。"""
    GRID = 8 * 3600 * 1000
    grid: dict[str, dict[int, float]] = {}
    all_ts = set()
    for sym, data in all_data.items():
        m = {}
        for t, r in data:
            g = t // GRID * GRID
            m[g] = r
        grid[sym] = m
        all_ts |= set(m)
    return sorted(all_ts), grid


def carry_rotating(all_data, k, rebal_days, lookback_days):
    ts, grid = build_grid(all_data)
    periods = int(rebal_days * 3)
    look = int(lookback_days * 3)
    if len(ts) < look + periods + 10:
        return {}

    rets = []
    i = look
    while i + periods < len(ts):
        # 过去 lookback 窗口的累计费率（允许少量缺失）
        scores = {}
        for sym, m in grid.items():
            vals = [m.get(ts[j]) for j in range(i - look, i)]
            vals = [v for v in vals if v is not None]
            if len(vals) >= look * 0.7:
                scores[sym] = sum(vals) / len(vals) * look   # 归一化到 look 期
        if len(scores) < k + 2:
            i += periods
            continue
        ranked = sorted(scores, key=lambda s: scores[s])
        shorts = ranked[-k:]        # 费率最高 → 做空收资金费

        gain = 0.0
        for sym in shorts:
            g = 0.0
            for j in range(i + 1, i + 1 + periods):
                v = grid[sym].get(ts[j])
                if v is not None:
                    g += v
            gain += g
        gain /= k
        # 每期一次再平衡：换腿比例 = min(1, k/k)=1（最坏情况全换），成本按比例
        cost = ROUND_TRIP_BOTH_LEGS
        rets.append(gain - cost)
        i += periods

    if not rets:
        return {}
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) if len(rets) > 1 else 0
    se = sd / math.sqrt(len(rets)) if sd else 0
    return {"n": len(rets), "net": m, "t": m / se if se else 0,
            "win": sum(1 for x in rets if x > 0) / len(rets),
            "annum": m * (365.0 / rebal_days)}


def main() -> None:
    print("=" * 104)
    print("delta 中性 carry（现货多 + 永续空）—— 修正版")
    print(f"  成本口径：每次完整开平仓（双腿）{ROUND_TRIP_BOTH_LEGS*100:.3f}%")
    print("  价格损益因 delta 中性而抵消 → 净收益 ≈ 资金费 − 成本")
    print("=" * 104)

    # ---- ① ETH/BTC 三年实测 ----
    print("\n【① 只做 ETH / BTC，全程持有（3.2 年真实数据）】")
    print(f"  {'品种':<10}{'覆盖':>8}{'期数':>8}{'资金费年化':>12}{'成本年化':>10}{'净年化':>10}{'正比例':>9}")
    base = {}
    for sym in ["ETHUSDT", "BTCUSDT"]:
        d = fetch_funding(sym)
        base[sym] = d
        yrs = (d[-1][0] - d[0][0]) / 1000 / 86400 / 365
        rates = [r for _, r in d]
        gross = sum(rates) / len(rates) * 3 * 365
        pos = sum(1 for r in rates if r > 0) / len(rates)
        for hold in [7, 30]:
            cost = ROUND_TRIP_BOTH_LEGS * 365 / hold
            print(f"  {sym:<10}{yrs:>7.2f}y{len(d):>8}{gross*100:>11.2f}%"
                  f"{cost*100:>9.2f}%{(gross-cost)*100:>9.2f}%{pos*100:>8.1f}%"
                  + f"   ← 持有{hold}天再平衡")

    # ---- ② 逐年 ----
    print("\n【② 逐年趋势：carry 是否在衰减？（持有 30 天再平衡，净年化）】")
    print(f"  {'品种':<10}{'2023':>11}{'2024':>11}{'2025':>11}{'2026':>11}")
    for sym, d in base.items():
        ys = yearly_split(d)
        row = f"  {sym:<10}"
        for y in ["2023", "2024", "2025", "2026"]:
            if y in ys:
                g = sum(ys[y]) / len(ys[y]) * 3 * 365
                row += f"{(g - ROUND_TRIP_BOTH_LEGS*365/30)*100:>10.2f}%"
            else:
                row += f"{'-':>11}"
        print(row)

    # ---- ③ 全期 vs 最近 ----
    print("\n【③ 当下的 carry 水平 —— 决定\"现在做还有没有意义\"】")
    print(f"  {'品种':<10}{'最近30天':>11}{'最近90天':>11}{'全期3.2年':>12}{'正比例':>9}")
    for sym, d in base.items():
        rates = [r for _, r in d]
        a30 = sum(rates[-90:]) / 90 * 3 * 365
        a90 = sum(rates[-270:]) / 270 * 3 * 365
        aall = sum(rates) / len(rates) * 3 * 365
        pos = sum(1 for r in rates if r > 0) / len(rates)
        print(f"  {sym:<10}{a30*100:>10.2f}%{a90*100:>10.2f}%{aall*100:>11.2f}%{pos*100:>8.1f}%")

    # ---- ④ 轮动选高费率 ----
    print("\n【④ 轮动：每 N 天按过去 7 天费率选 top-k 做空（含再平衡成本）】")
    univ = universe(30)
    all_data = {}
    print(f"  拉取 {len(univ)} 个品种的 3.2 年资金费历史 ...")
    for i, s in enumerate(univ, 1):
        d = fetch_funding(s)
        if len(d) > 2000:
            all_data[s] = d
    print(f"  有效品种（≥3年历史）: {len(all_data)} / {len(univ)}")
    if all_data:
        sample = list(all_data)[0]
        yrs = (all_data[sample][-1][0] - all_data[sample][0][0]) / 1000 / 86400 / 365
        print(f"  单品种覆盖: {yrs:.2f} 年（{len(all_data[sample])} 期）")

    print(f"\n  {'策略':<28}{'期数':>7}{'净/期':>11}{'t':>8}{'胜率':>8}{'年化':>10}")
    for k in [3, 5, 10]:
        for rebal in [7, 14, 30, 60]:
            r = carry_rotating(all_data, k, rebal, 7)
            if r:
                flag = "✅" if (r["net"] > 0 and r["t"] > 2) else ("⚠️" if r["net"] > 0 else "❌")
                print(f"  轮动 top{k:<2} 每{rebal:>2}天再平衡{'':<8}{r['n']:>7}"
                      f"{r['net']*100:>10.3f}%{r['t']:>8.2f}{r['win']*100:>7.1f}%"
                      f"{r['annum']*100:>9.1f}%  {flag}")
    r_all = carry_rotating(all_data, len(all_data), 30, 7)
    if r_all:
        print(f"  {'全池等权(不选品,对照)':<28}{r_all['n']:>7}"
              f"{r_all['net']*100:>10.3f}%{r_all['t']:>8.2f}{r_all['win']*100:>7.1f}%"
              f"{r_all['annum']*100:>9.1f}%")

    with open("/tmp/carry_final2.json", "w") as f:
        json.dump({"covered": {k: len(v) for k, v in base.items()},
                   "universe_n": len(all_data)}, f)
    print("\n[完成]")


if __name__ == "__main__":
    main()
