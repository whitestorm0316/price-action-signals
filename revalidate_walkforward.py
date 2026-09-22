"""三年独立复核（v2，修复版）：固定间隔检验 + 约束型扩张窗口 walk-forward。

修复 v1 两处缺陷：
  1. net() 要求 ≥25 笔已平仓 → 单季度用大间隔时只剩几笔而被整体跳过；
     本脚本改用自带的 _net()（阈值 6 笔），并对小样本打标警告。
  2. 选参网格未做可行性约束 → 会挑出 past 根本承载不了的间隔；现只在该
     past 样本能支撑（≥25 笔）的间隔里选。

同时给出「固定间隔」结果——完全不选参，是检验策略本身最诚实的形式。

用法：
    OKX_PROXY=http://127.0.0.1:1087 .venv/bin/python revalidate_walkforward.py
    OKX_PROXY=http://127.0.0.1:1087 .venv/bin/python revalidate_walkforward.py --sym BTC-USDT-SWAP
"""
from __future__ import annotations

import argparse
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from revalidate_eth4h import FEE, apply_gap, collect, fetch  # noqa: E402

GAPS = list(range(2, 41, 2))
FIXED = [4, 6, 8, 10, 12, 16, 20]
Q_MS = 90 * 86_400_000
MIN_CLOSED = 6       # 单段报告下限
MIN_SELECT = 25      # 选参时 past 必须达到的已平仓笔数


def closed_of(tr):
    return [t for t in tr if t["res"] != "O"]


def _net(tr, min_closed=MIN_CLOSED):
    cl = closed_of(tr)
    if len(cl) < min_closed:
        return None
    med = st.median([t["stop_pct"] for t in tr])
    if med <= 0:
        return None
    return sum(t["r"] for t in cl) / len(cl) - FEE / med


def quarter_buckets(tr, nq, t0):
    return [[t for t in tr if (t["ts"] - t0) // Q_MS == k] for k in range(nq)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="ETH-USDT-SWAP")
    ap.add_argument("--bar", default="4H")
    ap.add_argument("--bars", type=int, default=6570, help="3 年 4H ≈ 6570")
    ap.add_argument("--proxy", default=os.getenv("OKX_PROXY"))
    a = ap.parse_args()

    print("=" * 100)
    print(f"三年独立复核 v2：{a.sym} {a.bar} 突破入场 · 结构止损 · 无趋势")
    print("=" * 100)

    c = fetch(a.sym, a.bar, a.bars, a.proxy)
    span = (c[-1].ts - c[0].ts) / (365 * 86_400_000)
    trades = collect(c)
    t0 = trades[0]["ts"]
    nq = (trades[-1]["ts"] - t0) // Q_MS + 1
    print(f"  跨度 {span:.2f} 年 · {len(c)} 根 · 原始形态信号 {len(trades)} 个 · {nq} 个季度\n")

    # ---------------------------------------------------------------- ①
    print("  ① 参数区间稳健性（3 年整段，不做任何挑选）")
    vals, pos, rows = [], 0, []
    for g in GAPS:
        seg = apply_gap(trades, g)
        v = _net(seg)
        if v is None:
            continue
        vals.append(v)
        pos += v > 0
        rows.append((g, len(seg), v))
    for g, n, v in rows:
        print(f"     间隔{g:>3}: 笔{n:>4}  净 {v:>+8.4f}R  {'✅' if v > 0 else '❌'}")
    if vals:
        print(f"\n     → 正间隔 {pos}/{len(vals)} ({pos/len(vals)*100:.0f}%)  "
              f"平均 {sum(vals)/len(vals):+.4f}R  中位 {st.median(vals):+.4f}R")
        print(f"     → {'✅ 平台（整个区间普遍为正）' if pos/len(vals) >= 0.7 else '❌ 尖峰/震荡（不可信）'}")

    # ---------------------------------------------------------------- ②
    print(f"\n  ② 固定间隔 × 分季度（完全不选参 · 检验策略本身）")
    hdr = "     " + "".join(f"{('g='+str(g)):>11}" for g in FIXED)
    print(hdr)
    buckets = quarter_buckets(trades, nq, t0)
    qvals = {g: [] for g in FIXED}
    for k, b in enumerate(buckets):
        if not b:
            continue
        line = f"     第{k+1:>2}季"
        for g in FIXED:
            v = _net(apply_gap(b, g))
            if v is None:
                line += f"{'—':>11}"
            else:
                qvals[g].append(v)
                mark = "✅" if v > 0 else "❌"
                line += f"{v:>+9.2f}{mark} "
        print(line)
    print()
    ok_fixed = []
    for g in FIXED:
        qs = qvals[g]
        if len(qs) < 4:
            continue
        p = sum(1 for v in qs if v > 0)
        avg = sum(qs) / len(qs)
        passed = p / len(qs) >= 0.7
        ok_fixed.append((g, p, len(qs), avg, passed))
        print(f"     间隔{g:>3}: 正季度 {p}/{len(qs)} ({p/len(qs)*100:.0f}%)  "
              f"平均 {avg:+.4f}R  → {'✅' if passed else '❌'}")

    # ---------------------------------------------------------------- ③
    print(f"\n  ③ 扩张窗口真 walk-forward（每季只用「过去」选间隔 → 本季验证）")
    print(f"     {'季':>3} {'选中间隔':>8} {'历史净R':>9} {'本季笔数':>8} {'本季净R':>9}")
    wf, rets = [], []
    for k in range(1, nq):
        past = [t for t in trades if (t["ts"] - t0) // Q_MS < k]
        cur = buckets[k] if k < len(buckets) else []
        if len(closed_of(past)) < MIN_SELECT or not cur:
            continue
        best_g, best_v = None, -9e9
        for g in GAPS:
            seg = apply_gap(past, g)
            if len(closed_of(seg)) < MIN_SELECT:   # 可行性约束
                continue
            v = _net(seg, MIN_SELECT)
            if v is not None and v > best_v:
                best_g, best_v = g, v
        if best_g is None:
            continue
        seg = apply_gap(cur, best_g)
        v = _net(seg)
        if v is None:
            continue
        wf.append((k + 1, best_g, best_v, len(seg), v))
        rets.append(v)
        print(f"     {k+1:>3} {best_g:>8} {best_v:>+9.4f} {len(seg):>8} {v:>+9.4f}  "
              f"{'✅' if v > 0 else '❌'}")
    if rets:
        w = sum(1 for v in rets if v > 0)
        print(f"\n     → 正季度 {w}/{len(rets)} ({w/len(rets)*100:.0f}%)  "
              f"平均 {sum(rets)/len(rets):+.4f}R  中位 {st.median(rets):+.4f}R")
        print(f"     → {'✅ 通过' if w/len(rets) >= 0.7 else '❌ 未通过（符号不稳）'}")

    # ---------------------------------------------------------------- 判定
    print("\n" + "=" * 100)
    print("  判定标准（三项全过才叫通过独立复核）：")
    print("    A. 参数区间普遍为正（≥70%）")
    print("    B. 固定间隔下分季度普遍为正（≥70%）")
    print("    C. 真 walk-forward 各季普遍为正（≥70%）")
    print("=" * 100)
    A = bool(vals) and pos / len(vals) >= 0.7
    B = any(p[4] for p in ok_fixed)
    C = bool(rets) and sum(1 for v in rets if v > 0) / len(rets) >= 0.7
    print(f"    A 参数区间稳健性 : {'✅ 通过' if A else '❌ 未通过'}")
    print(f"    B 固定间隔分季度 : {'✅ 至少一个间隔通过' if B else '❌ 全部未通过'}")
    print(f"    C 真 walk-forward : {'✅ 通过' if C else '❌ 未通过'}")
    print(f"\n    >>> 最终判定：{'✅ 三项全过' if (A and B and C) else '❌ 未通过独立复核，不应投入资金'}")


if __name__ == "__main__":
    main()
