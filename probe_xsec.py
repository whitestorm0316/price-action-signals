"""方向评估实证 C：横截面动量（cross-sectional momentum）可行性。

这是本报告最关键的实验。动机：
  前两个实证显示，单品种动量在扣摩擦后**要么为负、要么 t<2**。
  横截面动量（做多相对强势、做空相对弱势）与单品种动量是**不同假设来源**，
  且学术文献（Jegadeesh-Titman 1993；加密市场 Asness et al.）显示它在
  高换手/多品种场景下可能提供额外分散收益。

本脚本严格检验：
  1. 在 OKX 流动性最好的 N 个 USDT 永续上构建横截面动量组合
  2. 每个调仓周期：按过去 lookback 收益排序 → 多头部 k 个、空尾部 k 个
  3. **按净期望评判**：扣往返手续费+滑点、扣真实资金费（多头付/空头收）
  4. 给出 t 值与逐期收益序列，判断是否显著
  5. 同时做**单品种动量**对照，量化横截面带来的分散增益

数据：OKX /api/v5/market/history-candles（1D 与 4H），公开接口，本地缓存。
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
CACHE = Path("/tmp/xsec_cache")
CACHE.mkdir(exist_ok=True)
HIST = "https://www.okx.com/api/v5/market/history-candles"
PAGE_DELAY = 0.22

# 摩擦口径：往返手续费 0.070% + 滑点 0.030%
ROUND_TRIP = 0.0010
# 资金费（实测均值，每天）：多头支付、空头收取
FUNDING_PER_DAY = 0.00010   # 用两品种实测均值的中位口径（ETH 0.0107%/天, BTC 0.0149%/天）


def fetch_candles(inst: str, bar: str, target: int) -> list[list[float]]:
    """分页向前拉取历史 K 线（升序返回）。"""
    cf = CACHE / f"{inst}_{bar}.json"
    if cf.exists():
        try:
            d = json.loads(cf.read_text())
            if len(d) >= target * 0.9:
                return d
        except Exception:
            pass
    rows: list[list[float]] = []
    after = None
    with httpx.Client(proxy=PROXY, timeout=25.0) as c:
        while len(rows) < target:
            params = {"instId": inst, "bar": bar, "limit": 100}
            if after:
                params["after"] = after
            try:
                r = c.get(HIST, params=params)
                r.raise_for_status()
                d = r.json()
            except Exception as e:
                print(f"    [warn] {inst} {bar}: {e}")
                break
            if d.get("code") != "0":
                break
            data = d.get("data") or []
            if not data:
                break
            rows.extend(data)
            after = data[-1][0]
            time.sleep(PAGE_DELAY)
    out = []
    for r in rows:
        out.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])])
    out.sort(key=lambda x: x[0])
    # 去重（按 ts）
    dedup = {}
    for r in out:
        dedup[r[0]] = r
    out = [dedup[k] for k in sorted(dedup)]
    cf.write_text(json.dumps(out))
    return out


def load_universe(n: int = 25) -> list[str]:
    """取流动性最好的 n 个 USDT 永续（按美元名义成交额）。"""
    with httpx.Client(proxy=PROXY, timeout=25.0) as c:
        r = c.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "SWAP"})
        r.raise_for_status()
        data = r.json()["data"]
    rows = []
    for x in data:
        iid = x["instId"]
        if not iid.endswith("-USDT-SWAP"):
            continue
        try:
            vol_usd = float(x.get("volCcy24h") or 0) * float(x.get("last") or 0)
            rows.append((iid, vol_usd))
        except Exception:
            continue
    rows.sort(key=lambda t: t[1], reverse=True)
    # 排除稳定币对（无方向性）
    excl = {"USDC-USDT-SWAP", "USDE-USDT-SWAP", "DAI-USDT-SWAP", "USD0-USDT-SWAP"}
    picked = [i for i, _ in rows if i not in excl][:n]
    return picked


def align(prices: dict[str, dict[int, float]], ts_list: list[int]) -> dict[int, dict[str, float]]:
    """把各品种的价格对齐到统一时间轴（仅在所有品种都有价的时点开调仓）。"""
    out = {}
    for ts in ts_list:
        row = {}
        for inst, series in prices.items():
            if ts in series:
                row[inst] = series[ts]
        if len(row) >= 10:
            out[ts] = row
    return out


def backtest_xsec(prices_by_inst: dict[str, list[list[float]]], bar_hours: float,
                  lookback: int, hold: int, k: int) -> dict:
    """横截面动量回测。每个调仓日：多头部 k、空尾部 k，等权。"""
    # 构建各品种 ts → price 映射
    series = {}
    all_ts = set()
    for inst, cs in prices_by_inst.items():
        m = {int(r[0]): r[4] for r in cs}  # 收盘价
        series[inst] = m
        all_ts |= set(m.keys())
    ts_sorted = sorted(all_ts)
    idx_of = {t: i for i, t in enumerate(ts_sorted)}

    period_returns = []
    period_details = []
    t = lookback
    while t + hold < len(ts_sorted):
        t_now = ts_sorted[t]
        t_exit = ts_sorted[t + hold]
        # 选品：需要 lookback 前有价
        t_past = ts_sorted[t - lookback]
        scorable = []
        for inst in series:
            p0 = series[inst].get(t_past)
            p1 = series[inst].get(t_now)
            pe = series[inst].get(t_exit)
            if p0 and p1 and pe and p0 > 0:
                scorable.append((inst, p1 / p0 - 1.0, p1, pe))
        if len(scorable) < 2 * k + 2:
            t += hold
            continue
        scorable.sort(key=lambda x: x[1], reverse=True)
        longs = scorable[:k]
        shorts = scorable[-k:]

        # 组合收益：多头 + 空头
        rets = []
        for _, _, p1, pe in longs:
            gross = pe / p1 - 1.0
            net = gross - ROUND_TRIP - FUNDING_PER_DAY * (hold * bar_hours / 24.0)
            rets.append(net)
        for _, _, p1, pe in shorts:
            gross = -(pe / p1 - 1.0)
            net = gross - ROUND_TRIP + FUNDING_PER_DAY * (hold * bar_hours / 24.0)
            rets.append(net)
        pr = sum(rets) / len(rets) if rets else 0.0
        period_returns.append(pr)
        period_details.append({
            "ts": t_now, "n": len(scorable), "net": pr,
            "longs": [x[0] for x in longs], "shorts": [x[0] for x in shorts],
        })
        t += hold

    if not period_returns:
        return {"n": 0}
    m = sum(period_returns) / len(period_returns)
    sd = math.sqrt(sum((x - m) ** 2 for x in period_returns) / (len(period_returns) - 1)) \
        if len(period_returns) > 1 else 0.0
    se = sd / math.sqrt(len(period_returns)) if len(period_returns) > 1 else float("inf")
    tstat = m / se if se > 0 else 0.0
    win = sum(1 for x in period_returns if x > 0) / len(period_returns)
    days = hold * bar_hours / 24.0
    return {
        "n": len(period_returns), "mean": m, "sd": sd, "se": se, "t": tstat, "win": win,
        "per_period_days": days,
        "annum": m * (365.0 / days) if days else 0,
        "best": max(period_returns), "worst": min(period_returns),
        "details": period_details,
    }


def backtest_single(prices_by_inst: dict[str, list[list[float]]], bar_hours: float,
                    lookback: int, hold: int) -> dict:
    """单品种 TSMOM 对照（每个品种独立跑，再取平均）。"""
    results = {}
    for inst, cs in prices_by_inst.items():
        m = {int(r[0]): r[4] for r in cs}
        ts_sorted = sorted(m.keys())
        rets = []
        t = lookback
        while t + hold < len(ts_sorted):
            p_past = m[ts_sorted[t - lookback]]
            p_now = m[ts_sorted[t]]
            p_exit = m[ts_sorted[t + hold]]
            past = p_now / p_past - 1.0
            if past != 0:
                side = 1 if past > 0 else -1
                gross = side * (p_exit / p_now - 1.0)
                net = gross - ROUND_TRIP - side * FUNDING_PER_DAY * (hold * bar_hours / 24.0)
                rets.append(net)
            t += hold
        if rets:
            mm = sum(rets) / len(rets)
            sd = math.sqrt(sum((x - mm) ** 2 for x in rets) / (len(rets) - 1)) if len(rets) > 1 else 0
            se = sd / math.sqrt(len(rets)) if len(rets) > 1 else float("inf")
            results[inst] = {"n": len(rets), "mean": mm,
                             "t": mm / se if se > 0 else 0.0,
                             "win": sum(1 for x in rets if x > 0) / len(rets)}
    return results


def main() -> None:
    print("=" * 100)
    print("横截面动量实验 · OKX 流动性最好的 USDT 永续")
    print(f"  摩擦: 往返 {ROUND_TRIP*100:.3f}%（手续费 0.070% + 滑点 0.030%）"
          f" | 资金费 {FUNDING_PER_DAY*100:.3f}%/天（多头付/空头收）")
    print("=" * 100)

    uni = load_universe(25)
    print(f"\n币种池（前 25 流动性）: {', '.join(x.replace('-USDT-SWAP','') for x in uni)}\n")

    # 拉 1D 数据（目标 1100 天 ≈ 3 年）+ 4H（目标 2000 根）
    daily, h4 = {}, {}
    for i, inst in enumerate(uni, 1):
        print(f"  [{i:>2}/{len(uni)}] {inst:<22} 拉取 1D ...", end="", flush=True)
        cs = fetch_candles(inst, "1D", 1100)
        daily[inst] = cs
        print(f" {len(cs)} 根", end="")
        print("  | 4H ...", end="", flush=True)
        cs4 = fetch_candles(inst, "4H", 2000)
        h4[inst] = cs4
        print(f" {len(cs4)} 根")
        if cs:
            span = (cs[-1][0] - cs[0][0]) / 1000 / 86400
            if i == 1:
                print(f"      （首个品种覆盖 {span:.0f} 天）")

    report = {}

    # ---- 日线横截面动量 ----
    print("\n" + "-" * 100)
    print("【A】日线横截面动量  （多头部 k、空尾部 k，等权，每个调仓周期换仓一次）")
    print("-" * 100)
    for lb, hd, k in [(7, 7, 3), (14, 7, 3), (30, 14, 3), (30, 30, 3), (60, 30, 5), (90, 30, 5)]:
        r = backtest_xsec(daily, 24.0, lb, hd, k)
        if not r.get("n"):
            print(f"  lb={lb:>3}d hold={hd:>3}d k={k}  (样本不足)")
            continue
        flag = "✅显著正" if (r["mean"] > 0 and r["t"] > 2) else ("⚠️正向不显著" if r["mean"] > 0 else "❌为负")
        print(f"  lb={lb:>3}d hold={hd:>3}d k={k}  n={r['n']:>4}  净={r['mean']*100:+7.3f}%/期  "
              f"t={r['t']:+5.2f}  胜率={r['win']*100:4.1f}%  年化={r['annum']*100:+7.1f}%  {flag}")
        key = f"daily_lb{lb}_h{hd}_k{k}"
        report[key] = {kk: vv for kk, vv in r.items() if kk != "details"}

    # ---- 4H 横截面动量 ----
    print("\n" + "-" * 100)
    print("【B】4H 横截面动量（更高换手，检验摩擦是否能被吃掉）")
    print("-" * 100)
    for lb, hd, k in [(6, 6, 3), (18, 6, 3), (42, 18, 3), (42, 42, 3), (126, 42, 5)]:
        r = backtest_xsec(h4, 4.0, lb, hd, k)
        if not r.get("n"):
            print(f"  lb={lb:>3}根 hold={hd:>3}根 k={k}  (样本不足)")
            continue
        flag = "✅显著正" if (r["mean"] > 0 and r["t"] > 2) else ("⚠️正向不显著" if r["mean"] > 0 else "❌为负")
        print(f"  lb={lb:>3}根({lb*4/24:.0f}d) hold={hd:>3}根({hd*4/24:.0f}d) k={k}  n={r['n']:>4}  "
              f"净={r['mean']*100:+7.3f}%/期  t={r['t']:+5.2f}  胜率={r['win']*100:4.1f}%  {flag}")
        key = f"h4_lb{lb}_h{hd}_k{k}"
        report[key] = {kk: vv for kk, vv in r.items() if kk != "details"}

    # ---- 单品种对照 ----
    print("\n" + "-" * 100)
    print("【C】单品种 TSMOM 对照（同一币池、同一周期，检验横截面是否真带来增益）")
    print("-" * 100)
    for lb, hd in [(14, 7), (30, 30)]:
        res = backtest_single(daily, 24.0, lb, hd)
        if res:
            means = [v["mean"] for v in res.values()]
            avg = sum(means) / len(means)
            pos = sum(1 for v in res.values() if v["mean"] > 0)
            print(f"  日线 lb={lb}d hold={hd}d : {pos}/{len(res)} 个品种净期望为正, "
                  f"平均净={avg*100:+.3f}%/期")
            report[f"single_daily_lb{lb}_h{hd}"] = {"avg": avg, "pos": pos, "tot": len(res)}

    # ---- 最优组合的逐期序列 ----
    best_key, best_t = None, -99
    for k, v in report.items():
        if isinstance(v, dict) and v.get("t") is not None and v.get("t") > best_t:
            best_t, best_key = v["t"], k
    print("\n" + "-" * 100)
    print(f"【D】最高 t 值组合: {best_key}  (t={best_t:+.2f})")
    print("-" * 100)

    with open("/tmp/xsec_result.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print("\n[已写入 /tmp/xsec_result.json]")


if __name__ == "__main__":
    main()
