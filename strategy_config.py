"""策略参数预设与统一配置层。

背景
----
本项目经历了一轮完整的策略复盘（结论见 `STRATEGY_CONCLUSIONS.md`）。
随后第八轮又做了一次"信号源体检"（结论见 `PRICE_ACTION_SYNTHESIS.md`），
用七步证据链把结论推进到了**机制层面**：

  1. **价格在 4H 上近似鞅**：方差比 1.036 / 0.983（z=+1.03 / −0.44）、
     自相关 |ρ| ≤ 0.03、|MFE|/|MAE| = **1.003**（两品种一致）。
  2. **形态不含方向信息**：用"同时间+同方向+同止损宽度+同盈亏比"的匹配随机入场
     做对照，ETH/BTC 的 α **都是 −0.0065R、p ≈ 0.83**。
  3. **形态的真实作用是「把局部趋势漂移归零」**，而不是预测方向：
     顺势 α = −0.266（z=−5.77）、逆势 α = +0.185（z=+4.56），方向相反、幅度对称。
  4. 因此 **「形态 + 趋势过滤」自我抵消**——形态会消掉你想保留的漂移。
  5. **唯一被数据支持的成分是「止损宽度」**：把止损从结构极值（f 中位 0.060R）
     放宽到 1.5×ATR（f 中位 0.036R），BTC 净期望由 −0.0718R 升到 +0.0375R。
     但该改善**来自成本而非信息**（形态源与随机源曲线形状一致）。
  6. **纯趋势本身也无显著 α**：ETH α=−0.062、BTC α=−0.019。

因此本模块定义三个预设：

  * ``legacy``（**默认**）—— 项目原有行为，逐字节不变。
    注意：它是"**已验证**"，但内容是"**已验证无效**"（15m 形态毛期望劣于随机入场），
    所以它**不是**一个安全选项，只是唯一被诚实证伪过的选项。
  * ``candidate`` —— 复盘中表现最好的形态候选。**未通过独立验证，不建议直接实盘。**
    第八轮进一步证明它**不含方向信息**（α ≈ 0），其表面表现来自止损口径。
  * ``trend_wide`` —— 第八轮认定的"**唯一还有正 t 的配置**"：
    纯趋势方向（MA20 斜率）+ **1.5×ATR 宽止损** + RR=2，且**不使用任何形态**。
    但必须强调：**它同样未通过独立验证**——BTC 净期望 +0.0398R（t=+1.93），
    然而与"同向随机入场"相比 α = −0.019（z=−0.22），
    即**那个正 t 来自市场漂移，不是去掉了形态的功劳**。

关于命名
--------
``candidate`` 早期叫 ``optimal``（"最优"）。该名字**有误导性**：
它字面承诺最优，而实测结论是"**不可交易**"（见上）。
虽已配套告警，但名称本身就在暗示可以依赖，故改名为 ``candidate``（候选），
**不再许诺任何优越性**。``optimal`` 仍作为输入别名保留（不破坏既有脚本/环境变量）。

设计原则
--------
1. **默认值绝不改变现有行为。** 不显式选择时一律使用 ``legacy``。
2. **所有参数都可覆盖**（环境变量优先于预设），做实验无需改代码。
3. 选择 ``candidate`` / ``trend_wide`` 时，若用于**自动交易（真金）**必须显式确认，
   见 :func:`needs_confirmation` 与 :func:`warning_lines`。

环境变量
--------
===========================  ==========================================
``PA_PRESET``                预设名（legacy / candidate / trend_wide）
``PA_TICKER``                交易对
``PA_INTERVAL``              K 线周期
``PA_ENTRY_MODE``            入场方式（close / breakout）
``PA_ATR_MULT``              止损 ATR 倍数（0 = 结构止损）
``PA_SIGNAL_MODE``           信号来源（pattern = 形态驱动 / trend = 纯趋势）
``PA_TREND_PERIOD``          均线周期（0 = 关闭趋势过滤）
``PA_MA_TYPE``               均线类型（sma / ema）
``PA_VOLUME_FILTER``         量能过滤（1/0）
``PA_MIN_GAP``               同形态最小间隔（根）
``PA_RR``                    盈亏比
===========================  ==========================================
"""
from __future__ import annotations

import os
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 预设定义
# ---------------------------------------------------------------------------
#: 项目原有默认行为。**不得修改这些取值**，否则会改变既有行为。
LEGACY: dict[str, Any] = {
    "ticker": "BTC-USDT-SWAP",
    "interval": "5m",
    "entry_mode": "close",     # 形态收盘价入场
    "signal_mode": "pattern",  # 形态触发（原有行为）
    "atr_mult": 0,             # 0 = 结构止损
    "trend_period": 50,        # 启用 50 周期均线趋势过滤
    "ma_type": "sma",
    "volume_filter": True,     # 启用 量能确认
    "min_gap": 0,              # 0 = 不做同形态间隔限制
    "rr": 2.0,
    "scan_window": 50,
    "limit": 100,
}

#: 复盘中表现最好的候选组合。**未通过独立验证（统计不显著），不要直接实盘。**
#:
#: 达到"实测最好"不等于"值得用"：该组合净期望 +0.161R 但 t=1.84、Bootstrap 95%CI 含 0，
#: 加入滑点与资金费后衰减至 +0.123R（t=1.41）；同配置在 BTC 上显著为负（t=−3.63）。
#: 故命名为 ``candidate``（候选）而非 ``optimal``，**不暗示它优于任何东西**。
#:
#: 第八轮补充：该组合（形态 + 突破入场 + 结构止损）在匹配随机入场下
#: **α ≈ 0**（p≈0.83）——它不含方向信息；其表面表现可由止损口径解释。
CANDIDATE: dict[str, Any] = {
    "ticker": "ETH-USDT-SWAP",
    "interval": "4H",
    "entry_mode": "breakout",  # 等价格真越过形态极值再进场
    "signal_mode": "pattern",  # 仍为形态触发（candidate 是形态候选）
    "atr_mult": 0,             # 结构止损（复盘中优于 ATR 口径）
    "trend_period": 0,         # 关闭趋势过滤（复盘中该维度方向不一致）
    "ma_type": "sma",
    "volume_filter": False,    # 复盘中量能维度无增益
    # 同形态最小间隔 40 根。**该值取自实测最优**：ETH 4H 基准 g=40 为 +0.161R / t=1.84
    # （全表最高），且抗成本最强（全叠加后仅削 24%）；对比 g=8 只有 +0.065R / t=1.38
    # 且**对成本最脆弱**（全叠加后削 63%），g=20 全叠加后正好归零。
    # ⚠️ 即便如此，g=40 的 t 仍 < 2、CI 仍含 0 —— 调这一档**不会**让策略变得可交易。
    "min_gap": 40,
    "rr": 2.0,
    "scan_window": 50,
    "limit": 300,
}

#: 第八轮认定的"**唯一还有正 t 的配置**"：**纯趋势 + 宽止损，不用任何形态**。
#:
#: 规则：MA20 斜率判趋势（阈值 0.2×ATR/price），趋势向上做多 / 向下做空；
#: 止损 = 1.5 × ATR；RR = 2。
#:
#: ⚠️ **三重警告，务必读完再决定是否使用**：
#:   1. **它仍然未通过独立验证。** BTC 净期望 +0.0398R（t=+1.93）、ETH −0.0137R（t=−0.67）。
#:   2. **那个正 t 不是"去掉了形态"的功劳。** 与"同向随机入场"相比，
#:      BTC α = −0.0188（z=−0.22）——**优势来自市场漂移，不含择时信息**。
#:   3. **ETH 上为负。** 两品种不一致，说明它在 BTC 上的微弱正值很可能是
#:      "BTC 这三年涨得更好"的副产品（`DIRECTION_REPORT.md` 里 TSMOM t=1.70 是同一现象）。
#:
#: 它被纳入预设的唯一理由是：**它是价格行为学框架内最接近"可验证"的一个落点**，
#: 且诚实地展示了"宽止损 + 无形态"这个组合长什么样。**不代表推荐交易。**
#:
#: 4. **``atr_mult=1.5`` 是"保守取值"，不是最优值。** 止损宽度扫描（`probe_pa_stopwidth.py`）
#:    显示两品种 argmax **不一致**：ETH 峰值在网格边界 k=3.0（+0.0833R / t=+2.76）、
#:    BTC 峰值 k=2.0（+0.0550R / t=+1.81，k=3.0 已转负 −0.0517R）。因两品种不一致、
#:    ETH 落在网格边界（项目"闸门 5 子检查"明确警告的陷阱）、且形态源与随机源曲线形状一致
#:    （效应纯机械、来自省手续费）⇒ **不存在可择优的唯一 k**。此处取 1.5 是为了让 f 明显下降
#:    又不至于让超时率失控（k=3.0 时超时率已 3.5%）。**不要把它当成"最优参数"。**
TREND_WIDE: dict[str, Any] = {
    "ticker": "BTC-USDT-SWAP",
    "interval": "4H",
    "entry_mode": "close",     # 不使用形态入场（形态已在第八轮被证明无信息）
    "signal_mode": "trend",    # ★ 纯趋势模式：不发形态，只看 MA20 斜率
    "atr_mult": 1.5,           # ★ 核心：1.5×ATR 宽止损，f 由 0.065R 降到 0.035R
    "trend_period": 20,        # MA20 斜率判趋势（第八轮对照实验口径）
    "ma_type": "sma",
    "volume_filter": False,
    "min_gap": 0,
    "rr": 2.0,
    "scan_window": 50,
    "limit": 300,
}

#: 历史名称 ``optimal`` 的别名（保留以便既有脚本 / 环境变量继续可用）。
#: 它指向同一份配置，但请注意 ``CANDIDATE`` 才是权威名称。
OPTIMAL: dict[str, Any] = CANDIDATE

PRESETS: dict[str, dict[str, Any]] = {
    "legacy": LEGACY,
    "candidate": CANDIDATE,
    "trend_wide": TREND_WIDE,
}

#: 预设名别名 → 权威名。``optimal`` 是 ``candidate`` 的历史叫法。
PRESET_ALIASES: dict[str, str] = {
    "optimal": "candidate",
}

DEFAULT_PRESET = "legacy"

#: 曾被评为"优于 legacy"但**未通过独立验证**的参数取值集合。
#: 只要配置命中其中任一项，该配置就应视为"未验证"。
_UNVALIDATED_MARKERS = ("entry_mode=breakout", "preset=candidate", "preset=trend_wide")

ENV_MAP: dict[str, str] = {
    "PA_TICKER": "ticker",
    "PA_INTERVAL": "interval",
    "PA_ENTRY_MODE": "entry_mode",
    "PA_ATR_MULT": "atr_mult",
    "PA_SIGNAL_MODE": "signal_mode",
    "PA_TREND_PERIOD": "trend_period",
    "PA_MA_TYPE": "ma_type",
    "PA_VOLUME_FILTER": "volume_filter",
    "PA_MIN_GAP": "min_gap",
    "PA_RR": "rr",
    "PA_SCAN_WINDOW": "scan_window",
    "PA_LIMIT": "limit",
}

#: 允许的入场方式
ENTRY_MODES = ("close", "breakout")

#: 允许的信号来源模式
#:   pattern = 检测 Pinbar/吞没形态触发（原有行为）
#:   trend   = 纯趋势：不发形态，只看均线斜率决定方向（第八轮对照实验的落地）
SIGNAL_MODES = ("pattern", "trend")

#: 预设名 → 一句话说明（供 CLI 帮助与仪表盘展示，避免各处重复硬编码）
PRESET_BLURB: dict[str, str] = {
    "legacy": "项目原有行为（15m 形态 + 趋势过滤）——已验证，但已验证无效",
    "candidate": "复盘中表现最好的形态候选（ETH 4H + 突破 + 间隔40）——统计不显著、α≈0",
    "trend_wide": "纯趋势 + 1.5×ATR 宽止损（不用形态）——唯一正 t，但优势来自漂移",
}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _coerce(key: str, raw: str) -> Any:
    """把环境变量字符串转成与预设同类型的值。

    注意 ``atr_mult`` 必须按 **float** 解析：``TREND_WIDE`` 用的是 ``1.5``，
    若按 int 会静默截断成 1（f 值差一倍），属危险的行为回归。
    """
    if key in ("min_gap", "trend_period", "scan_window", "limit"):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None
    if key in ("rr", "atr_mult"):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if key == "volume_filter":
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")
    return str(raw)



def env_overrides() -> dict[str, Any]:
    """读取 ``PA_*`` 环境变量，返回覆盖字典（无则空）。"""
    out: dict[str, Any] = {}
    for env_key, field in ENV_MAP.items():
        raw = os.getenv(env_key)
        if raw is None or raw == "":
            continue
        val = _coerce(field, raw)
        if val is not None:
            out[field] = val
    return out


def preset_name(explicit: str | None = None) -> str:
    """决定使用哪个预设：显式参数 > ``PA_PRESET`` > 默认 legacy。

    兼容历史名 ``optimal``（映射为 ``candidate``）。
    """
    name = explicit or os.getenv("PA_PRESET") or DEFAULT_PRESET
    name = PRESET_ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
    return name if name in PRESETS else DEFAULT_PRESET


def resolve(
    preset: str | None = None,
    *,
    use_env: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """合成最终配置。

    优先级（低 → 高）：预设默认值 → 环境变量 → 显式关键字参数。

    Args:
        preset: 预设名（legacy / candidate；``optimal`` 为兼容别名）；
            None 时读 ``PA_PRESET``，再退化为 legacy。
        use_env: 是否应用 ``PA_*`` 环境变量。
        **overrides: 显式覆盖项，值为 None 的忽略。

    Returns:
        配置字典（``preset`` 键为**权威名**，即 ``optimal`` 会被规范化为 ``candidate``）。
    """
    name = preset_name(preset)
    cfg = dict(PRESETS[name])
    cfg["preset"] = name
    if use_env:
        cfg.update(env_overrides())
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    return cfg


def normalize(cfg: dict[str, Any]) -> dict[str, Any]:
    """规范化并从**任意来源的配置字典**推导一致性字段。

    用于仪表盘：其配置字典由用户逐项设置，不一定带 ``preset`` 键。
    这里保证 ``preset`` 与关键字段互相自洽（例如选了 breakout 就标记为 candidate），
    并把历史别名 ``optimal`` 规范化为 ``candidate``。
    """
    out = dict(cfg)
    mode = str(out.get("entry_mode") or "close").strip().lower()
    out["entry_mode"] = mode if mode in ENTRY_MODES else "close"
    smode = str(out.get("signal_mode") or "pattern").strip().lower()
    out["signal_mode"] = smode if smode in SIGNAL_MODES else "pattern"
    try:
        out["min_gap"] = max(0, int(float(out.get("min_gap") or 0)))
    except (TypeError, ValueError):
        out["min_gap"] = 0
    out["atr_mult"] = _norm_atr_mult(out.get("atr_mult"))
    # 历史别名 → 权威名（否则 preset=optimal 会被误判为非法并退化成 legacy）
    cur = str(out.get("preset") or "").strip().lower()
    out["preset"] = PRESET_ALIASES.get(cur, cur)
    # preset 与字段自洽：signal_mode=trend 等价于 trend_wide 特征
    if out["preset"] not in PRESETS:
        if out["signal_mode"] == "trend":
            out["preset"] = "trend_wide"
        else:
            out["preset"] = "candidate" if out["entry_mode"] == "breakout" else "legacy"
    return out


def _norm_atr_mult(raw: Any) -> float:
    """规范化 ATR 倍数：非数值 / 负数一律归 0（= 结构止损，即原有行为）。

    返回 **float**（``TREND_WIDE`` 用 1.5，不能截断成 int）。
    """
    try:
        v = float(raw if raw is not None else 0)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


# ---------------------------------------------------------------------------
# 未验证配置的识别与告警
# ---------------------------------------------------------------------------
def unvalidated_reasons(cfg: dict[str, Any]) -> list[str]:
    """列出该配置为何偏离"已验证（原有）行为"，返回人类可读的原因列表。

    空列表 = 该配置没有命中任何未验证特征（即维持原有行为）。
    """
    c = normalize(cfg)
    reasons: list[str] = []
    if c.get("preset") == "candidate":
        reasons.append("使用了 candidate 预设（复盘中实测最好，但**不可交易**：统计不显著、α≈0）")
    if c.get("preset") == "trend_wide":
        reasons.append("使用了 trend_wide 预设（纯趋势 + 宽止损，**未通过独立验证**："
                       "BTC 正 t 来自市场漂移，α 不显著；ETH 上为负）")
    if c.get("signal_mode") == "trend":
        reasons.append("信号来源 = trend（**纯趋势模式，不使用任何形态**）——"
                       "第八轮证明形态无信息，但纯趋势同样不显著（未验证）")
    if c.get("entry_mode") == "breakout":
        reasons.append("入场方式 = breakout（突破入场），实盘需触发单支持")
    if c.get("atr_mult"):
        reasons.append(f"止损改用 ATR×{c['atr_mult']:g}（放宽止损以降低 f 成本；"
                       "第八轮证明这改变的是成本，不是信息含量）")
    if not c.get("trend_period"):
        reasons.append("已关闭趋势过滤（复盘中该维度方向不一致）")
    return reasons


def blocking_reasons(cfg: dict[str, Any]) -> list[str]:
    """需要**显式确认才能启动真金自动交易**的原因（比 :func:`unvalidated_reasons` 更窄）。

    区分两类偏离：
      * **阻断类**——引入新的机制或明确被标为"未验证"的配置：
        ``entry_mode=breakout``（需要触发单这条全新下单路径）、
        ``preset=candidate`` / ``preset=trend_wide``（未通过独立验证的预设）、
        ``atr_mult>0``（改变了止损定价机制）。
        这些必须显式确认，否则拒绝启动自动交易。
      * **仅提示类**——如单独关闭趋势过滤：只是过滤条件变化，
        走的是既有已验证的入场/止损路径，不阻断，仅在告警横幅中提示。

    保留这个区分是为了避免"告警疲劳"：若任何微调都弹确认，用户会习惯性点过。
    """
    c = normalize(cfg)
    reasons: list[str] = []
    if c.get("preset") == "candidate":
        reasons.append("使用了 candidate 预设（复盘中实测最好，但统计不显著、α≈0、不可交易）")
    if c.get("preset") == "trend_wide":
        reasons.append("使用了 trend_wide 预设（纯趋势 + 宽止损；未通过独立验证，"
                       "正 t 来自漂移而非择时信息）")
    if c.get("signal_mode") == "trend":
        reasons.append("信号来源 = trend（纯趋势模式，完全不使用形态——改动了下单触发的定义）")
    if c.get("entry_mode") == "breakout":
        reasons.append("入场方式 = breakout（突破入场），实盘需触发单支持")
    if c.get("atr_mult"):
        reasons.append(f"止损改用 ATR×{c['atr_mult']:g}（改动了止损定价机制）")
    return reasons


def is_unvalidated(cfg: dict[str, Any]) -> bool:
    """该配置是否偏离了已验证（原有）行为。"""
    return bool(unvalidated_reasons(cfg))


def needs_confirmation(cfg: dict[str, Any], *, live: bool = True) -> bool:
    """用于**自动交易**时是否需要显式确认。"""
    return live and bool(blocking_reasons(cfg))


def warning_lines_for(
    reasons: Iterable[str],
    *,
    blocking: Iterable[str] | None = None,
) -> list[str]:
    """按给定原因列表生成告警横幅（逐行）。``reasons`` 为空时返回空列表。

    阻断类原因标 ``⛔ [需确认]``，仅提示类标 ``· [仅提示]``，
    便于用户分辨严重程度。
    """
    rows_in = [str(r) for r in reasons]
    if not rows_in:
        return []
    blk = {str(b) for b in (blocking or [])}
    rows: list[str] = []
    for r in rows_in:
        is_blocking = r in blk
        mark = "  ⛔ [需确认] " if is_blocking else "  · [仅提示] "
        rows.append(f"{mark}{r}")
    return [
        "=" * 74,
        "⚠️  正在使用【未经独立验证】的策略参数 —— 请勿据此投入真金",
        "=" * 74,
        *rows,
        "",
        "  第八轮结论（PRICE_ACTION_SYNTHESIS.md · 七步证据链）：",
        "    · 4H 上价格近似**鞅**（方差比 1.036/0.983、|MFE|/|MAE| = 1.003）；",
        "    · 形态**不含方向信息**（匹配随机入场 α = −0.0065R、p ≈ 0.83）；",
        "    · 形态的真实作用是「**把局部趋势漂移归零**」（顺势 α=−0.266、逆势 α=+0.185），",
        "      故「形态 + 趋势过滤」**自我抵消**；",
        "    · 唯一被数据支持的成分是「**止损宽度**」（f 0.060R→0.036R），",
        "      但该改善**来自成本，不是信息**。",
        "",
        "  自动交易需显式确认后才会启动。",
        "=" * 74,
    ]


def warning_lines(cfg: dict[str, Any]) -> list[str]:
    """按配置生成告警横幅（逐行）。配置未偏离已验证行为时返回空列表。"""
    return warning_lines_for(unvalidated_reasons(cfg), blocking=blocking_reasons(cfg))


def summary(cfg: dict[str, Any]) -> str:
    """单行摘要，便于写日志。"""
    c = normalize(cfg)
    trend = c.get("trend_period") or 0
    am = c.get("atr_mult") or 0
    stop = f"ATR×{am:g}" if am else "结构"
    return (
        f"preset={c.get('preset')} {c.get('ticker')} {c.get('interval')} "
        f"entry={c.get('entry_mode')} stop={stop} "
        f"trend={trend or '关'} volume={'开' if c.get('volume_filter') else '关'} "
        f"min_gap={c.get('min_gap')} rr={c.get('rr')}"
    )


# ---------------------------------------------------------------------------
# 同形态最小间隔过滤
# ---------------------------------------------------------------------------
def pattern_kind(name: str) -> str:
    """形态名 → 类别（pinbar / engulfing），用于分组计算间隔。"""
    return "pinbar" if "pinbar" in str(name).lower() else "engulfing"


def filter_min_gap(
    items: Iterable[Any],
    gap: int,
    *,
    kind_of,
    order_of,
) -> list[Any]:
    """按形态**类别**保留最小间隔：同类信号之间至少相隔 ``gap`` 根 K 线。

    复盘发现形态会**连续聚集**，后续信号往往是"追单"，质量更差；
    因此对同类形态设置最小间隔可显著降频、提升单笔质量。

    Args:
        items: 待过滤项（如 (idx, pattern) 列表）。
        gap: 最小间隔（根）。``<=1`` 时原样返回（= 不改行为）。
        kind_of: 取形态类别的函数。
        order_of: 取时间序（K 线序号）的函数，须可比较。

    Returns:
        过滤后的列表，按 ``order_of`` 升序。
    """
    seq = list(items)
    if not gap or gap <= 1:
        return seq
    kept: list[Any] = []
    last: dict[str, int] = {}
    for it in sorted(seq, key=order_of):
        k = kind_of(it)
        o = order_of(it)
        if k not in last or o - last[k] >= gap:
            kept.append(it)
            last[k] = o
    return kept
