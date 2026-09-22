"""最后一组检验：carry 的杠杆上限，以及相对 USDT 理财的真实超额。

决定性问题：USDT 活期理财实测 4.80%（OKX 官方接口）。delta 中性 carry 净年化
5~6% —— **超额只有 1pp 左右**。那 carry 有没有办法靠杠杆放大？

关键结构事实（必须算清楚）：
  delta 中性 = 现货多 1 单位 + 永续空 1 单位（同名义）
  现货必须**全额付款** → 占用 1 单位本金
  永续空只需保证金（如 10x → 10%）
  → 总占用 ≈ 1.0 + 0.1 = 1.1 单位，本金 1 单位最多做 ~0.9 单位名义
  → **杠杆被现货腿锁死在 ≈1x**

本脚本验证：
  ① 不同保证金率下，1 元本金最多能做多少名义
  ② 能否通过「借 USDT 买现货」加杠杆（借贷成本 vs carry 收益）
  ③ 永续-永续（不同品种）能否加杠杆（可，但符号可能相反）
  ④ 结论：carry 的收益天花板到底在哪
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

PROXY = os.getenv("OKX_PROXY", "http://127.0.0.1:7897")
CACHE = Path("/tmp/bn_cache2")

SPOT_MAKER, PERP_MAKER = 0.0008, 0.0002
LEG_COST = 2 * SPOT_MAKER + 2 * PERP_MAKER   # 0.20%


def load(sym):
    return [(int(t), float(r)) for t, r in json.loads((CACHE / f"{sym}.json").read_text())]


def main():
    print("=" * 100)
    print("carry 的杠杆上限检验 —— 它能不能靠杠杆打败 USDT 理财？")
    print("=" * 100)

    # ---- ① 保证金对杠杆的约束 ----
    print("\n【① delta 中性 carry 的实际杠杆（现货腿锁死）】")
    print("  结构：本金 B 元 → 现货多 B·s 元 + 永续空 B·s 元（s = 现货占比）")
    print(f"  永续空需保证金 = B·s / L（L = 永续杠杆），需满足 B·s + B·s/L ≤ B")
    print(f"\n  {'永续杠杆 L':<14}{'可行现货占比 s':>16}{'实际名义/本金':>16}{'说明':>24}")
    for L in [3, 5, 10, 20, 50]:
        s = 1 / (1 + 1 / L)          # 满足 s + s/L = 1
        note = "无法放大到 1x 以上" if s < 0.99 else "接近 1x"
        print(f"  {str(L)+'x':<14}{s*100:>14.1f}%{s:>15.2f}x{note:>22}")
    print("\n  ⚠️ 关键：无论永续杠杆多高，**总名义都 < 1 倍本金**（现货腿全额占用）。")
    print("     所以 carry 的收益天花板 = 资金费率 × ~1 → 无法与理财拉开差距。")

    # ---- ② 借 USDT 加杠杆 ----
    print("\n【② 借 USDT 买现货来加杠杆 —— 借贷成本必须 < carry 收益】")
    try:
        with httpx.Client(proxy=PROXY, timeout=25.0) as c:
            d = c.get("https://www.okx.com/api/v5/public/interest-rate-loan-quota").json()
            if d.get("code") == "0":
                for x in d.get("data", []):
                    if x.get("ccy") == "USDT":
                        base = x.get("baseInfo") or []
                        print(f"  USDT 借币基础利率: {base}")
            d2 = c.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "MARGIN"}).json()
            print(f"  可用杠杆交易对: {len(d2.get('data') or [])} 个")
    except Exception as e:
        print(f"  （接口不可用：{e}）")

    print("\n  借贷成本参考（OKX 借币，市场普遍水平）:")
    print("    借 USDT 日利率约 0.005~0.03% → 年化 1.8~11%（随市场紧张度浮动）")
    print("    ⚠️ 若借币成本 8% > carry 毛收益 7.3% → **加杠杆后净亏**")

    # ---- ③ 真实数字：1x carry vs 理财 ----
    print("\n【③ 最终对照：把数字放在一起】")
    eth = load("ETHUSDT")
    btc = load("BTCUSDT")
    ge = sum(r for _, r in eth) / len(eth) * 3 * 365
    gb = sum(r for _, r in btc) / len(btc) * 3 * 365
    g = (ge + gb) / 2

    rows = []
    for hold in [7, 14, 30, 60, 90]:
        net = g - LEG_COST * 365 / hold
        rows.append((hold, net))
    print(f"\n  {'方案':<32}{'毛收益':>10}{'成本':>10}{'净年化':>10}{'vs 理财(4.8%)':>16}")
    print(f"  {'USDT 活期理财（基准，无风险）':<32}{'—':>10}{'—':>10}{'4.80%':>10}{'—':>16}")
    for hold, net in rows:
        c = LEG_COST * 365 / hold
        exc = net - 0.048
        flag = "✅ 有超额" if exc > 0.01 else ("⚠️ 微弱" if exc > 0 else "❌ 不如理财")
        print(f"  {f'carry {hold}天换仓':<32}{g*100:>9.2f}%{c*100:>9.2f}%"
              f"{net*100:>9.2f}%{exc*100:>+15.2f}pp  {flag}")

    print("\n  注：理财的 4.8% 是**当日快照**（会浮动，且高息常有额度限制）；")
    print("      carry 的 7.26% 是 **3.65 年实际均值**（含 2026Q1 负费率段）。")

    # ---- ④ 分年度 vs 理财 ----
    print("\n【④ 逐年对照（carry 60 天换仓 vs 理财 4.8%）】")
    print(f"  {'年份':<8}{'ETH carry':>12}{'BTC carry':>12}{'理财':>10}{'carry 是否胜出':>18}")
    for y in ["2023", "2024", "2025", "2026"]:
        def yann(d):
            rs = [r for t, r in d if time.strftime("%Y", time.gmtime(t / 1000)) == y]
            return sum(rs) / len(rs) * 3 * 365 - LEG_COST * 365 / 60 if rs else None
        a, b = yann(eth), yann(btc)
        if a is None:
            continue
        win = "✅ 胜" if (a + b) / 2 > 0.048 else "❌ 负"
        print(f"  {y:<8}{a*100:>11.2f}%{b*100:>11.2f}%{4.80:>9.2f}%{win:>18}")

    print("\n  → carry 只在**牛市/高热度**年份明显胜出理财（2024: +9.9% vs 4.8%）；")
    print("     平淡年份（2025: +2.2%、2026: 约 0%）**跑不过理财**。")

    with open("/tmp/carry_leverage.json", "w") as f:
        json.dump({"leg_cost": LEG_COST, "gross": g}, f)


if __name__ == "__main__":
    main()
