"""方向评估实证 B：动量（momentum）的可利用性。

问题：价格自身的趋势/动量是否包含可利用的期望？
按三个维度检验，并**全部按净期望（含摩擦）评判**：
  1. 时间序列动量 TSMOM（Moskowitz-Ooi-Pedersen 2012 的经典形态）
  2. 多周期 lookback × 持有期 网格
  3. 高频 vs 低频：摩擦占比如何吞掉动量

严格避免前视：信号在 bar t 收盘产生，在 bar t+1 开盘成交。
摩擦口径：往返 0.070%（taker+maker）+ 滑点 0.03% = 0.10%（保守）。
资金费按真实口径（多头付、空头收），取 OKX 实测均值。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

CACHE = Path("/Users/chenqifeng/code/price-action-signals/.cache")
FRICTION = 0.0010          # 往返手续费 + 滑点
FUNDING_PER_DAY = {        # 实测：多头每天付（正资金费），空头收
    "ETH-USDT-SWAP": 0.0000358 * 3,
    "BTC-USDT-SWAP": 0.0000497 * 3,
}


def load(inst: str, bar: str) -> list[list[float]]:
    p = CACHE / f"candles_{inst}_{bar}.json"
    raw = json.loads(p.read_text())
    # 缓存为 [ts, o, h, l, c, vol] 或 [ts, o, h, l, c, ...]；统一取前 5 位
    out = []
    for r in raw:
        out.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])])
    out.sort(key=lambda x: x[0])
    return out


def tsmom(candles, lookback: int, hold: int, bar_hours: float,
          funding_per_day: float) -> dict:
    """时间序列动量：过去 lookback 根收益为正则做多，为负则做空；持有 hold 根。

    入场 = 信号后一根的开盘价；出场 = 持有期后一根的开盘价。
    """
    n = len(candles)
    trades = []
    t = lookback + 1
    while t + hold < n:
        past_ret = candles[t - 1][4] / candles[t - 1 - lookback][4] - 1.0
        if past_ret == 0:
            t += hold
            continue
        side = 1 if past_ret > 0 else -1
        entry = candles[t][1]          # 下一根开盘
        exit_ = candles[t + hold][1]   # 持有 hold 根后的开盘
        gross = side * (exit_ / entry - 1.0)
        # 资金费：持仓 hold 根 → 天数
        days = hold * bar_hours / 24.0
        fund = -side * funding_per_day * days   # 多头付、空头收
        net = gross - FRICTION + fund
        trades.append({"gross": gross, "net": net, "side": side, "days": days})
        t += hold

    if not trades:
        return {"n": 0}
    nets = [x["net"] for x in trades]
    grosses = [x["gross"] for x in trades]
    m = sum(nets) / len(nets)
    mg = sum(grosses) / len(grosses)
    sd = math.sqrt(sum((x - m) ** 2 for x in nets) / (len(nets) - 1)) if len(nets) > 1 else 0
    se = sd / math.sqrt(len(nets)) if len(nets) > 1 else float("inf")
    t_stat = m / se if se and se != float("inf") and se > 0 else 0.0
    win = sum(1 for x in trades if x["net"] > 0) / len(trades)
    return {
        "n": len(trades), "gross": mg, "net": m, "sd": sd, "se": se,
        "t": t_stat, "win": win,
        "annum": m * (365.0 / (sum(x["days"] for x in trades) / len(trades))) if trades else 0,
    }


def fmt(label: str, r: dict) -> str:
    if not r.get("n"):
        return f"{label:<26} (无交易)"
    flag = "✅" if (r["net"] > 0 and r["t"] > 2) else ("⚠️ " if r["net"] > 0 else "❌")
    return (f"{label:<26} n={r['n']:>5}  毛={r['gross']*100:+7.3f}%  "
            f"净={r['net']*100:+7.3f}%  t={r['t']:+5.2f}  胜率={r['win']*100:4.1f}%  {flag}")


def run_all(inst: str) -> None:
    print("\n" + "=" * 104)
    print(f"【{inst}】时间序列动量 TSMOM（信号@收盘 → 次根开盘入场 → 持有 N 根开盘平仓）")
    print(f"  摩擦口径: 往返 {FRICTION*100:.3f}%（含手续费 0.070% + 滑点 0.030%）"
          f" | 资金费: 多头付 {FUNDING_PER_DAY[inst]*100:.4f}%/天、空头收（实测均值）")
    print("=" * 104)

    bars = [("15m", 0.25), ("1H", 1.0), ("4H", 4.0)]
    for bar, hours in bars:
        try:
            cs = load(inst, bar)
        except FileNotFoundError:
            continue
        n_days = (cs[-1][0] - cs[0][0]) / 1000 / 86400
        print(f"\n  ── {bar}（{len(cs)} 根 · 覆盖 {n_days:.0f} 天）──")

        # lookback / hold 用「根数」表达，同时打印折算的天数
        grids = {
            "15m": [(4, 4), (16, 16), (96, 96), (288, 96)],
            "1H":  [(6, 6), (24, 24), (72, 72), (168, 72)],
            "4H":  [(2, 2), (6, 6), (18, 18), (42, 18)],
        }[bar]
        for lb, hd in grids:
            r = tsmom(cs, lb, hd, hours, FUNDING_PER_DAY[inst])
            label = f"lb={lb}根({lb*hours/24:.1f}天) hold={hd}根({hd*hours/24:.1f}天)"
            print("    " + fmt(label, r))


def main() -> None:
    for inst in ["ETH-USDT-SWAP", "BTC-USDT-SWAP"]:
        run_all(inst)
    print()


if __name__ == "__main__":
    main()
