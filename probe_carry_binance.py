"""当下有什么好策略？—— 第四实证：用币安长历史验证资金费的跨期稳定性。

问题：OKX 资金费历史只有 96 天，无法判断 carry 是否跨年稳定。
      币安 API（fapi/v1/fundingRate）支持 startTime 翻页 → 可拉数年历史。

这解决了报告的局限 A：「年化 3.9~5.4% 的跨年稳定性未验证」。
验证要点：
  ① 分年度/分季度，资金费是否持续为正？
  ② 正比例是否跨期稳定？
  ③ 极端年份（如熊市）资金费是否转负？
  ④ 与 OKX 实测的 96 天是否一致（交叉验证）
"""
from __future__ import annotations

import json
import math
import os
import time

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
BASE = "https://fapi.binance.com/fapi/v1/fundingRate"


def fetch_binance(symbol: str, years: float = 3.0) -> list[tuple[int, float]]:
    """向前翻页拉取资金费历史（币安 8h 一期）。"""
    end = int(time.time() * 1000)
    start_target = end - int(years * 365 * 86400 * 1000)
    out: list[tuple[int, float]] = []
    cur_end = end
    with httpx.Client(proxy=PROXY, timeout=25.0) as c:
        for _ in range(40):
            p = {"symbol": symbol, "endTime": cur_end, "limit": 1000}
            try:
                r = c.get(BASE, params=p)
                r.raise_for_status()
                rows = r.json()
            except Exception as e:
                print(f"    [warn] {symbol}: {e}")
                break
            if not isinstance(rows, list) or not rows:
                break
            got = [(int(x["fundingTime"]), float(x["fundingRate"])) for x in rows]
            out.extend(got)
            earliest = min(t for t, _ in got)
            if earliest <= start_target:
                break
            cur_end = earliest - 1
            time.sleep(0.25)
    dedup = {t: r for t, r in out}
    return sorted(dedup.items())


def analyze(symbol: str, data: list[tuple[int, float]]) -> dict:
    ts = [t for t, _ in data]
    rates = [r for _, r in data]
    n = len(rates)
    span = (ts[-1] - ts[0]) / 1000 / 86400

    def agg(rs):
        m = sum(rs) / len(rs)
        return {
            "n": len(rs), "annum": m * 3 * 365,
            "pos": sum(1 for x in rs if x > 0) / len(rs),
            "mean_8h": m,
        }

    # 分季度
    quarters: dict[str, list[float]] = {}
    for t, r in zip(ts, rates):
        tm = time.gmtime(t / 1000)
        y, mo = tm.tm_year, tm.tm_mon
        q = f"{y}Q{(mo - 1) // 3 + 1}"
        quarters.setdefault(q, []).append(r)

    quarter_stats = {q: agg(rs) for q, rs in sorted(quarters.items())}

    # 分年
    years: dict[str, list[float]] = {}
    for t, r in zip(ts, rates):
        y = time.strftime("%Y", time.gmtime(t / 1000))
        years.setdefault(y, []).append(r)
    year_stats = {y: agg(rs) for y, rs in sorted(years.items())}

    # 负费率区间的长度分布（连续为负的段）
    neg_runs, cur = [], 0
    for r in rates:
        if r < 0:
            cur += 1
        else:
            if cur:
                neg_runs.append(cur)
            cur = 0
    if cur:
        neg_runs.append(cur)

    overall = agg(rates)
    return {
        "symbol": symbol, "n": n, "span_days": span,
        "overall": overall, "quarters": quarter_stats, "years": year_stats,
        "neg_run_max": max(neg_runs) if neg_runs else 0,
        "neg_run_avg": sum(neg_runs) / len(neg_runs) if neg_runs else 0,
        "neg_ratio": sum(1 for r in rates if r < 0) / n,
        "first": time.strftime("%Y-%m-%d", time.gmtime(ts[0] / 1000)),
        "last": time.strftime("%Y-%m-%d", time.gmtime(ts[-1] / 1000)),
    }


def main() -> None:
    print("=" * 100)
    print("币安资金费长历史（8h 一期）—— 验证 carry 的跨期稳定性")
    print("=" * 100)

    symbols = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    results = {}
    for s in symbols:
        print(f"\n拉取 {s} ...")
        d = fetch_binance(s, years=3.0)
        if not d:
            print("  无数据")
            continue
        a = analyze(s, d)
        results[s] = a
        print(f"  {a['n']} 期 · {a['first']} ~ {a['last']} ({a['span_days']:.0f} 天 "
              f"= {a['span_days']/365:.2f} 年)")

    print("\n" + "=" * 100)
    print("【A】全期与分年度：资金费 carry 是否跨年稳定为正？")
    print("=" * 100)
    for s, a in results.items():
        o = a["overall"]
        print(f"\n【{s}】全期年化 {o['annum']*100:+.2f}% · 正比例 {o['pos']*100:.1f}% · "
              f"覆盖 {a['span_days']/365:.2f} 年")
        print(f"  {'年份':<8}{'样本期':>8}{'年化':>11}{'正比例':>10}{'判定':>8}")
        for y, st in a["years"].items():
            ok = st["annum"] > 0 and st["pos"] > 0.6
            print(f"  {y:<8}{st['n']:>8}{st['annum']*100:>10.2f}%{st['pos']*100:>9.1f}%"
                  f"{'  ✅' if ok else '  ⚠️'}")

    print("\n" + "=" * 100)
    print("【B】分季度：是否有季度转负？（熊市/极端行情检验）")
    print("=" * 100)
    for s, a in results.items():
        qs = a["quarters"]
        neg_q = [q for q, st in qs.items() if st["annum"] < 0]
        print(f"\n【{s}】共 {len(qs)} 个季度，其中 {len(neg_q)} 个年化为负 "
              f"({len(neg_q)/len(qs)*100:.0f}%)")
        if neg_q:
            print(f"  负的季度: {', '.join(neg_q)}")
        vals = [st["annum"] for st in qs.values()]
        print(f"  季度年化分布: 最小 {min(vals)*100:+.1f}%  中位 "
              f"{sorted(vals)[len(vals)//2]*100:+.1f}%  最大 {max(vals)*100:+.1f}%")

    print("\n" + "=" * 100)
    print("【C】负费率连续段（空头需承受的逆风）")
    print("=" * 100)
    for s, a in results.items():
        print(f"  {s:<10} 负费率占比 {a['neg_ratio']*100:5.1f}%  "
              f"最长连续负 {a['neg_run_max']:>3} 期 ({a['neg_run_max']/3:.1f} 天)  "
              f"平均连续负 {a['neg_run_avg']:.1f} 期")

    print("\n" + "=" * 100)
    print("【D】交叉验证：币安 vs OKX（同期 96 天）")
    print("=" * 100)
    try:
        okx = json.load(open("/tmp/carry_stability.json"))
        for sym, okx_key in [("ETHUSDT", "ETH-USDT-SWAP"), ("BTCUSDT", "BTC-USDT-SWAP")]:
            if okx_key in okx and sym in results:
                okx_ann = okx[okx_key]["mean_annum"]
                bn_ann = results[sym]["overall"]["annum"]
                print(f"  {sym:<10} OKX(96天) {okx_ann*100:+.2f}%   "
                      f"币安(全期) {bn_ann*100:+.2f}%   "
                      f"差异 {(bn_ann-okx_ann)*100:+.2f}pp")
    except FileNotFoundError:
        print("  （缺 /tmp/carry_stability.json）")

    with open("/tmp/carry_binance.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n[已写入 /tmp/carry_binance.json]")


if __name__ == "__main__":
    main()
