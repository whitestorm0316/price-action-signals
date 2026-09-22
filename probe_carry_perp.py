"""唯一能加杠杆的 carry 变体：永续-永续（双腿都是合约，只需保证金）。

逻辑：现货腿必须全额付款 → delta 中性 carry 被锁死在 ≈1x。
      若两腿都用永续（空高费率币 + 多低费率币），则只需保证金 → 可加 3~5x 杠杆。

但代价（必须诚实检验）：**不再是 delta 中性**。
      空 basket A、多 basket B → 承担 A 与 B 的**相对价格风险**，
      这部分是 cross-sectional 动量/反转，本次已实测（t=1.19，不显著）。

本脚本用真实价格 + 真实资金费，量化：
  ① 资金费价差本身有多大（毛 edge）
  ② 相对价格风险的波动有多大（会吞掉多少）
  ③ 加杠杆后是放大 edge 还是放大噪音
  ④ 最终 Sharpe 是否优于「1x delta 中性 carry」和「USDT 理财」
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics as st
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
FAPI = "https://fapi.binance.com/fapi/v1"
CACHE = Path("/tmp/bn_cache2")
KCACHE = Path("/tmp/bn_klines")
KCACHE.mkdir(exist_ok=True)

PERP_MAKER, PERP_TAKER = 0.0002, 0.0005
LEG_COST = 2 * PERP_MAKER + 2 * PERP_MAKER      # 两腿全挂单 0.08%

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
        "LINKUSDT", "LTCUSDT", "ADAUSDT", "AVAXUSDT", "BCHUSDT", "ARBUSDT",
        "UNIUSDT", "NEARUSDT", "SUIUSDT", "ENAUSDT", "TAOUSDT"]


def load_funding(sym):
    f = CACHE / f"{sym}.json"
    return [(int(t), float(r)) for t, r in json.loads(f.read_text())] if f.exists() else []


def load_klines(sym, interval="8h", limit=1500):
    f = KCACHE / f"{sym}_{interval}.json"
    if f.exists():
        try:
            d = json.loads(f.read_text())
            if len(d) > 500:
                return d
        except Exception:
            pass
    end = int(time.time() * 1000)
    out: dict[int, float] = {}
    cur = end
    with httpx.Client(proxy=PROXY, timeout=30.0) as c:
        for _ in range(12):
            try:
                rows = c.get(f"{FAPI}/klines", params={
                    "symbol": sym, "interval": interval,
                    "endTime": cur, "limit": 500}).json()
            except Exception:
                break
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                out[int(r[0])] = float(r[4])       # close
            if len(rows) < 500:
                break
            cur = min(int(r[0]) for r in rows) - 1
            time.sleep(0.12)
    res = sorted(out.items())
    f.write_text(json.dumps(res))
    return res


def main():
    print("=" * 108)
    print("永续-永续 carry（可加杠杆）—— 唯一能突破 1x 上限的变体")
    print("=" * 108)
    print(f"  两腿全挂单成本 {LEG_COST*100:.3f}%/次（比现货腿便宜 60%）")

    print(f"\n拉取 {len(SYMS)} 个品种的资金费 + 8h K 线 ...")
    fund, px = {}, {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {s: ex.submit(load_funding, s) for s in SYMS}
        kuts = {s: ex.submit(load_klines, s) for s in SYMS}
        for s in SYMS:
            fd = futs[s].result()
            if len(fd) > 2000:
                fund[s] = fd
            px[s] = kuts[s].result()
    syms = [s for s in SYMS if s in fund and len(px.get(s) or []) > 500]
    print(f"  有效品种: {len(syms)}")

    # 8h 网格对齐
    G = 8 * 3600 * 1000
    fgrid, pgrid = {}, {}
    all_ts = set()
    for s in syms:
        fm = {t // G * G: r for t, r in fund[s]}
        pm = {t // G * G: c for t, c in px[s]}
        fgrid[s], pgrid[s] = fm, pm
        all_ts |= (set(fm) & set(pm))
    ts = sorted(all_ts)
    print(f"  共同时间网格: {len(ts)} 期 = {len(ts)/3/365:.2f} 年")

    # ---- ① 资金费价差有多大 ----
    print("\n【① 资金费横截面价差（毛 edge 的来源）】")
    spreads = []
    for i in range(len(ts)):
        vals = sorted(fgrid[s].get(ts[i]) for s in syms if fgrid[s].get(ts[i]) is not None)
        vals = [v for v in vals if v is not None]
        if len(vals) >= 6:
            k = max(1, len(vals) // 5)
            spreads.append(sum(vals[-k:]) / k - sum(vals[:k]) / k)
    print(f"  高费率组 − 低费率组（各取 20%）平均价差: {st.mean(spreads)*100:.4f}%/8h "
          f"= 年化 {st.mean(spreads)*3*365*100:.2f}%")
    print(f"  价差标准差: {st.stdev(spreads)*100:.4f}%   "
          f"中位 {st.median(spreads)*100:.4f}%")

    # ---- ② 相对价格风险有多大 ----
    print("\n【② 相对价格风险（永续-永续不是 delta 中性，必须承担）】")
    pspread = []
    for i in range(1, len(ts)):
        vals = []
        for s in syms:
            a, b = pgrid[s].get(ts[i]), pgrid[s].get(ts[i - 1])
            if a and b:
                vals.append((a / b - 1, fgrid[s].get(ts[i - 1]) or 0))
        if len(vals) >= 6:
            vals.sort(key=lambda x: x[1])
            k = max(1, len(vals) // 5)
            hi = sum(v[0] for v in vals[-k:]) / k
            lo = sum(v[0] for v in vals[:k]) / k
            pspread.append(hi - lo)
    print(f"  高费率组 − 低费率组 的 8h 价格收益差: 均值 {st.mean(pspread)*100:+.4f}%  "
          f"标准差 {st.stdev(pspread)*100:.4f}%")
    print(f"  → 价格噪音是资金费价差的 {st.stdev(pspread)/st.mean(spreads):.1f} 倍")
    print(f"  → 这意味着**单期 Sharpe 极低**，需要很长时间/很多期才能积累")

    # ---- ③ 实际回测（不同 k / 周期 / 杠杆）----
    print("\n【③ 回测：永续-永续 carry（空高费率 + 多低费率，等名义）】")
    print(f"  {'k':>3}{'周期':>7}{'杠杆':>6}{'期数':>7}{'净/期':>11}{'年化':>10}"
          f"{'波动':>10}{'Sharpe':>9}{'NW t(4)':>10}")
    results = []
    for k in [3, 5]:
        for rebal in [14, 30]:
            periods = rebal * 3
            rets = []
            i = periods
            while i + periods < len(ts):
                # 用过去 periods 期资金费排序
                sc = {}
                for s in syms:
                    vs = [fgrid[s].get(ts[j]) for j in range(i - periods, i)]
                    vs = [v for v in vs if v is not None]
                    if len(vs) >= periods * 0.7:
                        sc[s] = sum(vs) / len(vs)
                if len(sc) < 2 * k + 2:
                    i += periods
                    continue
                rk = sorted(sc, key=lambda s: sc[s])
                shorts, longs = rk[-k:], rk[:k]
                fg, pg = 0.0, 0.0
                for s in shorts:
                    fg += sum(v for v in (fgrid[s].get(ts[j])
                                          for j in range(i, i + periods)) if v is not None)
                    a, b = pgrid[s].get(ts[i]), pgrid[s].get(ts[i + periods])
                    if a and b:
                        pg -= (b / a - 1)          # 空头：价格跌则赚
                for s in longs:
                    fg -= sum(v for v in (fgrid[s].get(ts[j])
                                          for j in range(i, i + periods)) if v is not None)
                    a, b = pgrid[s].get(ts[i]), pgrid[s].get(ts[i + periods])
                    if a and b:
                        pg += (b / a - 1)          # 多头
                ret = (fg + pg) / k - LEG_COST
                rets.append(ret)
                i += periods
            if len(rets) < 8:
                continue
            n = len(rets)
            m = st.mean(rets)
            sd = st.stdev(rets)
            sharpe = m / sd * math.sqrt(365 / rebal) if sd else 0
            # NW t
            dev = [x - m for x in rets]
            g0 = sum(d * d for d in dev) / n
            s2 = g0
            for L in range(1, 5):
                s2 += 2 * (1 - L / 5) * sum(dev[j] * dev[j - L] for j in range(L, n)) / n
            nwt = m / math.sqrt(s2 / n) if s2 > 0 else 0
            print(f"  {k:>3}{rebal:>6}天{'1x':>6}{n:>7}{m*100:>10.4f}%"
                  f"{m*(365/rebal)*100:>9.2f}%{sd*100:>9.3f}%{sharpe:>9.2f}{nwt:>10.2f}")
            results.append({"k": k, "rebal": rebal, "n": n, "net": m,
                            "annum": m * 365 / rebal, "sharpe": sharpe, "nwt": nwt,
                            "sd": sd})

    # ---- ④ 杠杆的影响 ----
    print("\n【④ 加杠杆的影响（以最好的无杠杆配置放大）】")
    if results:
        best = max(results, key=lambda r: r["nwt"])
        print(f"  基准配置: k={best['k']} 周期={best['rebal']}天  "
              f"净年化 {best['annum']*100:.2f}%  波动 {best['sd']*100:.3f}%/期")
        print(f"  {'杠杆':>6}{'年化':>11}{'年波动':>11}{'Sharpe':>9}{'最大回撤估计':>14}"
              f"{'结论':>18}")
        for lev in [1, 2, 3, 5]:
            ann = best["annum"] * lev
            vol = best["sd"] * math.sqrt(365 / best["rebal"]) * lev
            # 回撤估计：按 beta=2.5 的布朗运动近似
            dd = vol * math.sqrt(best["n"] / (365 / best["rebal"]) ) * 0.7
            note = "可接受" if dd < 0.15 else ("偏高" if dd < 0.30 else "危险")
            print(f"  {str(lev)+'x':>6}{ann*100:>10.2f}%{vol*100:>10.2f}%"
                  f"{best['sharpe']:>9.2f}{-dd*100:>13.1f}%{note:>18}")

    print("\n" + "=" * 108)
    print("【⑤ 三种方案最终对照】")
    print("=" * 108)
    print(f"  {'方案':<34}{'年化':>10}{'波动':>10}{'Sharpe':>9}{'杠杆':>7}{'核心风险':>20}")
    print(f"  {'USDT 活期理财':<34}{'2~4.8%':>10}{'~0':>10}{'∞':>9}{'—':>7}{'平台风险':>20}")
    print(f"  {'delta中性carry(90天,现货腿)':<34}{'6.45%':>10}{'~0.4%':>10}{'~16':>9}"
          f"{'1x(锁死)':>7}{'基差/强平':>20}")
    if results:
        b = max(results, key=lambda r: r["nwt"])
        print(f"  {'永续-永续carry(k='+str(b['k'])+','+str(b['rebal'])+'天)':<34}"
              f"{b['annum']*100:>9.2f}%{b['sd']*math.sqrt(365/b['rebal'])*100:>9.2f}%"
              f"{b['sharpe']:>9.2f}{'3~5x':>7}{'相对价格':>20}")

    print("\n  注：永续-永续的「波动」含相对价格风险，不是 delta 中性 → 与现货腿 carry 不可直接比 Sharpe。")

    with open("/tmp/carry_perp.json", "w") as f:
        json.dump({"results": results,
                   "spread_mean": st.mean(spreads),
                   "price_sd": st.stdev(pspread)}, f, indent=2)
    print("\n[已写入 /tmp/carry_perp.json]")


if __name__ == "__main__":
    main()
