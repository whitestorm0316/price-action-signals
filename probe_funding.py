"""方向评估实证 A：资金费（funding rate）的可利用性。

问题：资金费是否包含可持续、可利用的信息？
  (a) 它是否长期单边偏移（carry 收益的来源）？
  (b) 它是否有持续性（可用过去预测未来）？
  (c) 它的量级相对于手续费/滑点是否足够大？

数据：OKX /api/v5/public/funding-rate-history（8h 一次），公开接口。
"""
from __future__ import annotations

import json
import math
import os
import time

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
BASE = "https://www.okx.com/api/v5/public/funding-rate-history"
PAGE = 100  # 该端点单页上限


def fetch_all(inst_id: str, max_pages: int = 40) -> list[dict]:
    out: list[dict] = []
    after = None
    with httpx.Client(proxy=PROXY, timeout=20.0) as c:
        for i in range(max_pages):
            params = {"instId": inst_id, "limit": PAGE}
            if after:
                params["after"] = after
            r = c.get(BASE, params=params)
            r.raise_for_status()
            d = r.json()
            if d.get("code") != "0":
                print(f"  [warn] {inst_id} page{i} code={d.get('code')} {d.get('msg')}")
                break
            rows = d.get("data") or []
            if not rows:
                break
            out.extend(rows)
            after = rows[-1]["fundingTime"]  # 向前翻页
            time.sleep(0.25)
    return out


def summarize(inst_id: str) -> dict:
    rows = fetch_all(inst_id)
    if not rows:
        return {}
    recs = sorted(
        ({"ts": int(r["fundingTime"]), "rate": float(r.get("realizedRate") or r["fundingRate"])}
         for r in rows),
        key=lambda x: x["ts"],
    )
    rates = [r["rate"] for r in recs]
    n = len(rates)
    mean = sum(rates) / n
    srt = sorted(rates)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    var = sum((x - mean) ** 2 for x in rates) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    pos = sum(1 for x in rates if x > 0) / n
    neg = sum(1 for x in rates if x < 0) / n
    zero = sum(1 for x in rates if x == 0) / n

    # 一阶自相关（持续性）
    def autocorr(series: list[float], lag: int = 1) -> float:
        m = sum(series) / len(series)
        num = sum((series[i] - m) * (series[i - lag] - m) for i in range(lag, len(series)))
        den = sum((x - m) ** 2 for x in series)
        return num / den if den else 0.0

    ac1 = autocorr(rates, 1)
    ac2 = autocorr(rates, 2)
    ac7 = autocorr(rates, 7)   # 7×8h ≈ 2.33 天
    ac21 = autocorr(rates, 21)  # 7 天

    # 年化（8h 一次 → 每天 3 次 → 1095 次/年）
    annum = mean * 3 * 365
    span_days = (recs[-1]["ts"] - recs[0]["ts"]) / 1000 / 86400

    # 关键对照：往返手续费 0.070%。持仓 k 天需付资金费 ≈ mean*3*k
    # 多少次 8h 结算才等于一次往返手续费？
    settlements_per_roundtrip = (0.00070 / mean) if mean else float("inf")

    return {
        "instId": inst_id, "n": n, "span_days": span_days,
        "mean_8h": mean, "median_8h": median, "sd_8h": sd,
        "pct_pos": pos, "pct_neg": neg, "pct_zero": zero,
        "annum_simple": annum,
        "ac1": ac1, "ac2": ac2, "ac7": ac7, "ac21": ac21,
        "settlements_per_roundtrip": settlements_per_roundtrip,
        "first": time.strftime("%Y-%m-%d", time.gmtime(recs[0]["ts"] / 1000)),
        "last": time.strftime("%Y-%m-%d", time.gmtime(recs[-1]["ts"] / 1000)),
    }


def main() -> None:
    results = []
    for inst in ["ETH-USDT-SWAP", "BTC-USDT-SWAP"]:
        s = summarize(inst)
        if s:
            results.append(s)

    print("=" * 92)
    print("资金费历史（OKX 公开接口 · realizedRate · 8h 一结算）")
    print("=" * 92)
    for s in results:
        print(f"\n【{s['instId']}】  样本 {s['n']} 期 · 覆盖 {s['span_days']:.0f} 天 "
              f"({s['first']} ~ {s['last']})")
        print(f"  单期均值 {s['mean_8h']*100:+.5f}%   中位数 {s['median_8h']*100:+.5f}%   "
              f"标准差 {s['sd_8h']*100:.5f}%")
        print(f"  正: {s['pct_pos']*100:5.1f}%   负: {s['pct_neg']*100:5.1f}%   "
              f"零: {s['pct_zero']*100:4.1f}%")
        print(f"  → 简单年化（多头持续承担的期望）: {s['annum_simple']*100:+.2f}% / 年")
        print(f"  持续性 自相关: lag1={s['ac1']:+.4f}  lag2={s['ac2']:+.4f}  "
              f"lag7(2.3天)={s['ac7']:+.4f}  lag21(7天)={s['ac21']:+.4f}")
        cf = s["settlements_per_roundtrip"]
        if cf != float("inf"):
            print(f"  量级对照: 单期均值是往返手续费(0.070%)的 1/{abs(0.00070/s['mean_8h']):.1f}"
                  f" → 持仓 {cf:.0f} 期(≈{cf/3:.1f}天)的资金费 = 一次往返手续费")

    with open("/tmp/funding_result.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n[已写入 /tmp/funding_result.json]")


if __name__ == "__main__":
    main()
