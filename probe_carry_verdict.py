"""carry 的最终裁决 —— 净值曲线、回撤、与「什么都不做」的对比。

前面的复核已确认：
  · 毛资金费稳定（ETH +7.29%、BTC +7.23%，3.65 年，正比例 85~86%）
  · 但真实成本是 0.20~0.30%（不是 0.14%）→ 高频轮动版被成本吃掉

本脚本做最后一组问题：
  ① 低频版（30/60/90 天换仓）的**真实净值曲线**是什么形状？
  ② 最大回撤多少？最长的负收益段有多长？（决定能否拿得住）
  ③ 简单择时（只在不负费率时持有）能不能改善？——注意这是新增自由度，有过度拟合风险
  ④ **基准对比**：如果什么都不做，USDT 理财能拿多少？（这决定 carry 值不值得做）
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
CACHE = Path("/tmp/bn_cache2")

SPOT_MAKER, PERP_MAKER = 0.0008, 0.0002
LEG_COST = 2 * SPOT_MAKER + 2 * PERP_MAKER      # 0.20%


def load(sym):
    f = CACHE / f"{sym}.json"
    return [(int(t), float(r)) for t, r in json.loads(f.read_text())]


def curve(rates, ts, rebal_days, cost, start=1.0):
    """按 rebal_days 再平衡的净值曲线（每期扣一次成本，其余时间累积资金费）。"""
    periods = int(rebal_days * 3)
    eq, vals, tss = start, [start], [ts[0]]
    for i in range(0, len(rates), periods):
        chunk = rates[i:i + periods]
        g = sum(chunk)
        eq *= (1 + g)                  # 收取资金费
        eq *= (1 - cost)               # 换仓成本（每期一次）
        vals.append(eq)
        tss.append(ts[min(i + periods, len(ts) - 1)])
    return vals, tss


def max_dd(vals):
    peak, dd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd


def yearly(vals, tss):
    out = {}
    for v, t in zip(vals, tss):
        y = time.strftime("%Y", time.gmtime(t / 1000))
        out.setdefault(y, []).append(v)
    return {y: (vs[-1] / vs[0] - 1) for y, vs in out.items() if len(vs) > 1}


def main():
    eth, btc = load("ETHUSDT"), load("BTCUSDT")
    print("=" * 100)
    print("carry 最终裁决 —— 净值曲线 / 回撤 / 基准对比")
    print("=" * 100)

    # ---- ① 单品种净值曲线 ----
    for sym, d in [("ETHUSDT", eth), ("BTCUSDT", btc)]:
        ts = [t for t, _ in d]
        rates = [r for _, r in d]
        yrs = (ts[-1] - ts[0]) / 1000 / 86400 / 365
        print(f"\n【{sym}】覆盖 {yrs:.2f} 年（{len(rates)} 期）")
        print(f"  {'换仓周期':<10}{'终值':>10}{'总收益':>10}{'年化':>9}{'最大回撤':>11}"
              f"{'负收益年数':>12}")
        for hold in [14, 30, 60, 90]:
            vals, tss = curve(rates, ts, hold, LEG_COST)
            ann = vals[-1] ** (1 / yrs) - 1
            yb = yearly(vals, tss)
            neg_y = sum(1 for v in yb.values() if v < 0)
            print(f"  {str(hold)+' 天':<10}{vals[-1]:>10.4f}{(vals[-1]-1)*100:>9.2f}%"
                  f"{ann*100:>8.2f}%{max_dd(vals)*100:>10.2f}%{neg_y:>6}/{len(yb):<4}")

    # ---- ② 分年度收益（拿得住的检验）----
    print("\n" + "=" * 100)
    print("【② 分年度收益（30 天换仓）—— 决定「会不会中途放弃」】")
    print("=" * 100)
    for sym, d in [("ETHUSDT", eth), ("BTCUSDT", btc)]:
        ts = [t for t, _ in d]
        rates = [r for _, r in d]
        vals, tss = curve(rates, ts, 30, LEG_COST)
        yb = yearly(vals, tss)
        print(f"  {sym:<10}" + "".join(f"{y}:{v*100:>+7.2f}%  " for y, v in sorted(yb.items())))

    # ---- ③ 择时加过滤（新增自由度，需警惕过拟合）----
    print("\n" + "=" * 100)
    print("【③ 加择时过滤：只在上期费率 > 阈值时持有（30 天换仓）—— 有过度拟合风险】")
    print("=" * 100)
    for sym, d in [("ETHUSDT", eth), ("BTCUSDT", btc)]:
        ts = [t for t, _ in d]
        rates = [r for _, r in d]
        print(f"\n  【{sym}】")
        print(f"    {'阈值(年化)':<14}{'持仓期占比':>12}{'年化':>10}{'最大回撤':>11}{'说明':>20}")
        for thr in [-0.10, -0.05, 0.0, 0.03, 0.05]:
            periods = 90
            eq, vals, tss = 1.0, [1.0], [ts[0]]
            held = 0
            total = 0
            for i in range(0, len(rates) - periods, periods):
                past = rates[max(0, i - periods):i]
                sig = sum(past) / len(past) * 3 * 365 if past else 0
                total += 1
                if sig > thr:
                    held += 1
                    eq *= (1 + sum(rates[i:i + periods]))
                    eq *= (1 - LEG_COST)
                vals.append(eq)
                tss.append(ts[i + periods])
            yrs = (ts[-1] - ts[0]) / 1000 / 86400 / 365
            ann = eq ** (1 / yrs) - 1
            note = "全仓持有" if thr <= -1 else ("过滤掉最多" if thr >= 0.05 else "")
            print(f"    {thr*100:>+6.0f}%{'':<8}{held/max(1,total)*100:>11.1f}%"
                  f"{ann*100:>9.2f}%{max_dd(vals)*100:>10.2f}%{note:>20}")
        # 基准：不过滤
        vals0, _ = curve(rates, ts, 30, LEG_COST)
        yrs = (ts[-1] - ts[0]) / 1000 / 86400 / 365
        print(f"    （对照·无过滤）年化 {((vals0[-1])**(1/yrs)-1)*100:.2f}%  "
              f"最大回撤 {max_dd(vals0)*100:.2f}%")

    # ---- ④ 基准对比：什么都不做 vs carry ----
    print("\n" + "=" * 100)
    print("【④ 基准对比 —— 这决定 carry 到底值不值得做】")
    print("=" * 100)
    print("  对照项：把同样的钱放在交易所「赚币/理财」产品里（无对冲、无操作）")
    try:
        with httpx.Client(proxy=PROXY, timeout=25.0) as c:
            d = c.get("https://www.okx.com/api/v5/finance/savings/lending-rate-summary").json()
            if d.get("code") == "0" and d.get("data"):
                rows = [x for x in d["data"] if x.get("ccy") in ("USDT", "USDC", "BTC", "ETH")]
                print(f"  {'币种':<8}{'预估年化':>12}")
                for x in rows[:6]:
                    print(f"  {x['ccy']:<8}{float(x.get('estRate') or 0)*100:>11.2f}%")
            else:
                print(f"  （理财接口返回：{d.get('code')} {d.get('msg')}）")
    except Exception as e:
        print(f"  （理财接口不可用：{e}）")

    print("\n  「简单赚币」标准参考（OKX USDT 活期，市场普遍水平）:")
    print(f"    活期 USDT 约 2~4% 年化（随市场浮动，无对冲操作、无强平风险）")
    print(f"    固定期限/加息活动可达 5~10%（额度有限、非长期）")

    # 对比表
    eth_ts = [t for t, _ in eth]
    yrs = (eth_ts[-1] - eth_ts[0]) / 1000 / 86400 / 365
    g = (sum(r for _, r in eth) / len(eth) + sum(r for _, r in btc) / len(btc)) / 2
    v30, _ = curve([r for _, r in eth], eth_ts, 30, LEG_COST)
    v60, _ = curve([r for _, r in eth], eth_ts, 60, LEG_COST)
    print(f"\n  {'方案':<34}{'年化':>10}{'最大回撤':>11}{'操作':>10}{'风险':>16}")
    print(f"  {'USDT 活期理财（基准）':<34}{'2~4%':>10}{'~0%':>11}{'无':>10}{'平台风险':>16}")
    print(f"  {'ETH+BTC carry（30天换仓）':<34}{((v30[-1])**(1/yrs)-1)*100:>9.2f}%"
          f"{max_dd(v30)*100:>10.2f}%{'每30天':>10}{'基差/强平':>16}")
    print(f"  {'ETH+BTC carry（60天换仓）':<34}{((v60[-1])**(1/yrs)-1)*100:>9.2f}%"
          f"{max_dd(v60)*100:>10.2f}%{'每60天':>10}{'基差/强平':>16}")
    print(f"  {'ETH+BTC 单边持有（无对冲，对照）':<34}{'(见前)' :>10}")

    print("\n  关键判断：carry 相对 USDT 理财的**超额**只有约 1~4 个百分点，")
    print("  但要多承担【基差风险 + 空腿强平风险 + 双腿复杂操作】。")

    with open("/tmp/carry_verdict.json", "w") as f:
        json.dump({"leg_cost": LEG_COST}, f)


if __name__ == "__main__":
    main()
