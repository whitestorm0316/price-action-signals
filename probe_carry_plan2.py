"""当下可执行方案（修正版）—— OKX delta 中性 carry 选品与收益测算。

修正：OKX /public/funding-rate 必须**逐品种**查询（instId 为必填，不支持 instType 批量）。
     并发查询所有「现货∩永续」可对冲品种。

输出：
  ① 当期费率排名（可对冲品种）
  ② 币安 3.2 年历史做稳定性代理（哪些币费率**持续**高）
  ③ 推荐组合 + 不同本金下的预期收益
  ④ 风险清单
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
BN_CACHE = Path("/tmp/bn_cache2")
OKX = "https://www.okx.com/api/v5"


def bn_stats(base: str) -> dict | None:
    f = BN_CACHE / f"{base}USDT.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
    except Exception:
        return None
    if len(d) < 1000:
        return None
    rates = [float(r) for _, r in d]
    n = len(rates)
    q: dict[str, list[float]] = {}
    for t, r in d:
        tm = time.gmtime(int(t) / 1000)
        q.setdefault(f"{tm.tm_year}Q{(tm.tm_mon-1)//3+1}", []).append(float(r))
    neg_q = sum(1 for v in q.values() if sum(v) / len(v) < 0)
    return {
        "annum_3y": sum(rates) / n * 3 * 365,
        "pos_3y": sum(1 for r in rates if r > 0) / n,
        "annum_90d": sum(rates[-270:]) / min(270, n) * 3 * 365,
        "neg_q": neg_q, "tot_q": len(q),
        "worst_q": min(sum(v) / len(v) * 3 * 365 for v in q.values()),
    }


def main() -> None:
    print("=" * 106)
    print("当下可执行方案 —— OKX delta 中性 carry（现货多 + 永续空）")
    print("=" * 106)

    with httpx.Client(proxy=PROXY, timeout=30.0) as c:
        spot = c.get(f"{OKX}/market/tickers", params={"instType": "SPOT"}).json().get("data", [])
        swap = c.get(f"{OKX}/market/tickers", params={"instType": "SWAP"}).json().get("data", [])
        insts = c.get(f"{OKX}/public/instruments", params={"instType": "SWAP"}).json().get("data", [])

    spot_set = {x["instId"] for x in spot if x.get("last")}
    spot_px = {x["instId"]: float(x["last"]) for x in spot if x.get("last")}
    swap_px = {x["instId"]: float(x["last"]) for x in swap if x.get("last")}
    imap = {x["instId"]: x for x in insts}

    # 可对冲品种
    pairs = []
    for x in swap:
        iid = x.get("instId", "")
        if not iid.endswith("-USDT-SWAP"):
            continue
        base = iid.replace("-USDT-SWAP", "")
        if f"{base}-USDT" in spot_set:
            pairs.append((base, iid))
    print(f"\n现货 ∩ 永续 可对冲品种: {len(pairs)} 个")

    # 并发查询当期费率
    def get_fr(item):
        base, iid = item
        try:
            with httpx.Client(proxy=PROXY, timeout=15.0) as cc:
                d = cc.get(f"{OKX}/public/funding-rate", params={"instId": iid}).json()
                if d.get("code") == "0" and d.get("data"):
                    r = float(d["data"][0]["fundingRate"])
                    return {"base": base, "instId": iid, "fr": r, "annum": r * 3 * 365}
        except Exception:
            pass
        return None

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(get_fr, pairs):
            if res:
                out.append(res)
    print(f"成功取到当期费率: {len(out)} 个")
    pos = sum(1 for r in out if r["annum"] > 0)
    print(f"当期费率为正: {pos} ({pos/len(out)*100:.0f}%)  "
          f"全市场中位 {sorted(r['annum'] for r in out)[len(out)//2]*100:+.2f}%")

    # 附稳定性 + 规格
    for r in out:
        r["bn"] = bn_stats(r["base"])
        iid = r["instId"]
        r["ctVal"] = float(imap.get(iid, {}).get("ctVal") or 0)
        r["minSz"] = float(imap.get(iid, {}).get("minSz") or 0)
        r["px"] = swap_px.get(iid, 0)
        r["min_notional"] = r["px"] * r["ctVal"] * r["minSz"]
    out.sort(key=lambda x: x["annum"], reverse=True)

    print("\n" + "-" * 106)
    print("【A】当期费率最高 25 个（现货可对冲）")
    print("-" * 106)
    print(f"  {'币种':<10}{'当期年化':>10}{'3年费率':>10}{'3年正比':>9}{'负季度':>8}"
          f"{'最差季度':>10}{'最近90天':>10}{'最小名义':>11}")
    for r in out[:25]:
        bn = r["bn"]
        f = lambda k, fm="{:>10}" : (fm.format(bn[k]*100 if isinstance(bn[k], float) else bn[k])
                                     if bn else "       n/a")
        if bn:
            print(f"  {r['base']:<10}{r['annum']*100:>9.1f}%{bn['annum_3y']*100:>9.1f}%"
                  f"{bn['pos_3y']*100:>8.0f}%{bn['neg_q']:>4}/{bn['tot_q']:<3}"
                  f"{bn['worst_q']*100:>9.1f}%{bn['annum_90d']*100:>9.1f}%{r['min_notional']:>11,.2f}")
        else:
            print(f"  {r['base']:<10}{r['annum']*100:>9.1f}%{'n/a':>10}{'n/a':>9}"
                  f"{'n/a':>8}{'n/a':>10}{'n/a':>10}{r['min_notional']:>11,.2f}")

    print("\n" + "-" * 106)
    print("【B】推荐组合（当期 > 5% 且 3年正比例 > 80% 且 负季度 ≤ 1 且 有长历史）")
    print("-" * 106)
    picks = [r for r in out if r["annum"] > 0.05 and r["bn"]
             and r["bn"]["pos_3y"] > 0.80 and r["bn"]["neg_q"] <= 1]
    picks.sort(key=lambda x: x["annum"], reverse=True)
    if picks:
        print(f"  {'币种':<10}{'当期年化':>10}{'3年费率':>10}{'3年正比':>9}{'最差季度':>10}"
              f"{'最近90天':>10}{'最小名义':>11}")
        for r in picks[:10]:
            bn = r["bn"]
            print(f"  {r['base']:<10}{r['annum']*100:>9.1f}%{bn['annum_3y']*100:>9.1f}%"
                  f"{bn['pos_3y']*100:>8.0f}%{bn['worst_q']*100:>9.1f}%"
                  f"{bn['annum_90d']*100:>9.1f}%{r['min_notional']:>11,.2f}")
        top = picks[:10]
        avg_now = sum(r["annum"] for r in top) / len(top)
        avg_3y = sum(r["bn"]["annum_3y"] for r in top) / len(top)
        avg_90 = sum(r["bn"]["annum_90d"] for r in top) / len(top)
        print(f"\n  组合({len(top)}个)平均：当期 {avg_now*100:+.1f}%  "
              f"3年 {avg_3y*100:+.1f}%  最近90天 {avg_90*100:+.1f}%")
        print(f"  注意：3年均值含 2024 高费率年与 2026Q1 负费率月，是偏保守的估数")
    else:
        print("  无品种满足全部条件")

    print("\n" + "-" * 106)
    print("【C】收益测算（成本 0.14%/次双腿开平仓）")
    print("-" * 106)
    COST = 0.0014
    print(f"  {'方案':<34}{'费率年化':>11}{'成本年化':>11}{'净年化':>10}")
    for name, gross, hold in [
        ("ETH 单品种（30天换仓）", 0.0729, 30),
        ("BTC 单品种（30天换仓）", 0.0723, 30),
        ("ETH+BTC 等权（30天换仓）", 0.0726, 30),
        ("ETH+BTC 等权（60天换仓）", 0.0726, 60),
        ("轮动 top3（14天，实证 3.65年）", 0.0471 + COST * 365 / 14, 14),
    ]:
        cost = COST * 365 / hold
        print(f"  {name:<34}{gross*100:>10.2f}%{cost*100:>10.2f}%{(gross-cost)*100:>9.2f}%")

    print(f"\n  不同本金下的年收益（按净 5.5% 计；同时给出\"最少需要多少本金才能开双腿\"）:")
    print(f"    {'本金 USDT':>12}{'年收益':>11}{'月收益':>11}{'可开腿数':>10}{'说明':>16}")
    for bal in [50, 100, 500, 1000, 5000, 20000, 100000]:
        legs = int(bal / (2.72 * 2))
        note = "开不出" if legs < 1 else ("刚够1腿" if legs == 1 else f"{legs} 腿并行")
        print(f"    {bal:>12,}{bal*0.055:>11.2f}{bal*0.055/12:>11.2f}{legs:>10}{note:>16}")

    print("\n" + "-" * 106)
    print("【D】风险清单")
    print("-" * 106)
    for k, v in [
        ("负费率季节", "2026Q1 连续 3 个月为负（BTC 月均 -0.8~-2.2%、ETH 最低 -4.0%）→ 会有回撤段"),
        ("成本敏感", "成本翻倍(0.28%)后 NW t 从 2.97 降至 0.67、净年化仅 +1.06% → 必须挂单+低频换仓"),
        ("基差风险", "现货-永续价差随行情波动，极端时扩大 → 需留保证金"),
        ("强平风险", "空腿遇暴涨可能被强平 → 建议杠杆 ≤3x、留 50% 余量"),
        ("最小下单", "双腿都要满足 minSz，本金小无法建仓（见上表）"),
        ("统计口径", "资金费自相关 lag1≈0.55 → 必须看 Newey-West t（朴素 t 虚高约 40%）"),
        ("数据代理", "OKX 资金费仅回溯 96 天；3.2 年稳定性用币安数据代理"),
    ]:
        print(f"  · {k:<10} {v}")

    with open("/tmp/carry_plan.json", "w") as f:
        json.dump(out[:50], f, ensure_ascii=False, indent=2)
    print("\n[已写入 /tmp/carry_plan.json]")


if __name__ == "__main__":
    main()
