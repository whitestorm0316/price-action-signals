# OKX 价格行为学交易信号生成器

从 OKX V5 **公开 API** 拉取 K 线数据，检测 **Pinbar** 和 **吞没（Engulfing）** 两种价格行为形态，
生成 TradingView Webhook 兼容的 JSON 信号并打印到控制台（当前**未**接入真实 Webhook）。

> ⚠️ **重要前提：本项目的形态策略已完成完整独立复核，结论是"没有任何配置可交易"。**
> **第八轮进一步给出了统一解释**——不是"参数没调好"，而是**价格行为学的前提本身不成立**。
> 请先读结论再决定是否使用：
>
> | 文档 | 内容 | 何时读 |
> |---|---|---|
> | [`PRICE_ACTION_SYNTHESIS.md`](PRICE_ACTION_SYNTHESIS.md) | **★ 价格行为学统一陈述**（第八轮，七步证据链） | **想知道"为什么不行、什么才行"** |
> | [`STRATEGY_CONCLUSIONS.md`](STRATEGY_CONCLUSIONS.md) | **形态策略结论摘要**（可独立阅读，5 分钟） | 想看形态策略 |
> | [`CARRY_REPORT.md`](CARRY_REPORT.md) | 资金费 carry 验证报告（第七轮，含可执行方案） | 想看另一条独立路线 |
> | [`DIRECTION_REPORT.md`](DIRECTION_REPORT.md) | 下一步方向评估（三道墙 + 4 个新方向的实测） | 想知道往哪走 |
> | [`STRATEGY_DIAGNOSIS.md`](STRATEGY_DIAGNOSIS.md) | 完整诊断（6 轮实验 + 8 项统计检验） | 想看证据链 |
>
> **三句话总结**：
> 1. **形态策略**：15m 毛期望**劣于随机入场**；ETH 4H 最好的配置**方向为正但统计不显著**（t=1.84、CI 含 0）；BTC 同配置**显著为负**。→ **不要投入资金。**
> 2. **为什么不行**（第八轮）：4H 上价格**近似鞅**（方差比 1.036/0.983、|MFE|/|MAE|=1.003）；用匹配随机入场检验，**形态 α = −0.0065R、p≈0.83（两品种完全一致）**。形态的真实作用是**把局部趋势漂移归零**（顺势 α=−0.266、逆势 α=+0.185，方向相反幅度对称），而非预测方向。**唯一被数据支持的成分是"止损宽度"**——它是风险管理，与形态无关。
> 3. **资金费 carry**：ETH/BTC 毛费率 3.65 年稳定为正（+7.29%/+7.23%，13 季 0 负）。**低频版（60~90 天换仓）净 6.0~6.5%、回撤 <0.7%，可用**；但**无法加杠杆**，且相对 USDT 理财（4.80%）超额仅 +1.2~1.7pp/年。
>
> **✅ 第八轮结论已编码进代码（可直接用参数调）**：`--atr-mult`（止损宽度）、
> `--signal-mode trend`（纯趋势、不用形态）、`--preset trend_wide`。
> **默认值均为"不介入"**，故不传时行为与改造前**逐字节一致**。
> 详见下方 [`trend_wide` 预设](#trend_wide-预设纯趋势--宽止损) 与
> [止损宽度](#止损宽度--atr-mult第八轮唯一被数据支持的成分)。
>
> **❓「最优参数是什么？」的完整回答见 [`OPTIMAL_PARAMS.md`](OPTIMAL_PARAMS.md)** ——
> 简短版：**不存在可交易的"最优参数"**（信号层 α≈0 全是噪音；止损层有单调改善但无可择优的唯一值）。

## 技术栈

- Python 3.10+
- `httpx` 请求 OKX 公开行情接口（无需 API Key）
- 无前端、无图表渲染

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. 运行（默认 BTC-USDT-SWAP，5 分钟线）
./.venv/bin/python main.py

# 3. 指定交易对与周期
./.venv/bin/python main.py --inst ETH-USDT-SWAP --bar 15m

# 4. 易读格式输出（调试用）
./.venv/bin/python main.py --pretty

# 5. 离线演示（内置模拟数据，不请求网络）
./.venv/bin/python main.py --mock

# 6. OKX 被墙时，走本地代理（Clash/V2Ray 等）
./.venv/bin/python main.py --proxy http://127.0.0.1:7890

# 7. 使用自定义数据端点（公司内网镜像 / 自建聚合服务）
./.venv/bin/python main.py --host https://your-mirror.example.com/api/v5

# 8. 扫描最近 N 根 K 线的历史信号，验证数据链路与形态检测
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --scan 100

# 9. 对最近 N 根 K 线的信号做回测：跟踪每笔信号后续走势，统计成功/失败/胜率
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --backtest 300

# 10. 启用量能确认：放量信号加权、缩量信号减权，并过滤"缩量且盈亏比<2"的信号
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --volume

# 11. 突破入场：等价格真越过形态极值才进场（复盘显示可过滤假信号）
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --entry-mode breakout

# 12. 同形态最小间隔 8 根（形态会聚集，后续多为追单，间隔过滤可降频）
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --min-gap 8

# 13. 放宽止损到 1.5×ATR（第八轮唯一被数据支持的成分：f 从 0.060R 降到 0.036R）
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --bar 4H --atr-mult 1.5 --backtest 300

# 14. 纯趋势模式：完全不看形态，只用均线斜率决定方向（第八轮对照实验的落地）
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --signal-mode trend --trend 20 --atr-mult 1.5

# 15. 直接用 trend_wide 预设（= 上面那条的组合，**未通过独立验证**）
./.venv/bin/python main.py --preset trend_wide --proxy http://127.0.0.1:7890 --backtest 300
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--inst` | `BTC-USDT-SWAP` | OKX 交易对 |
| `--bar` | `5m` | K 线周期（1m/3m/5m/15m/30m/1H/4H/1D 等） |
| `--limit` | `300` | 拉取 K 线条数（OKX 单次上限 300） |
| `--pretty` | 关 | 易读格式打印 JSON |
| `--mock` | 关 | 使用内置模拟数据（离线验证） |
| `--proxy` | 无 | HTTP 代理地址，OKX 被墙时使用 |
| `--host` | 无 | 自定义数据端点前缀 |
| `--timeout` | `10` | 单次请求超时秒数 |
| `--trend` | `0` | 启用均线趋势过滤（周期 N，如 50）；0=不启用 |
| `--ma-type` | `sma` | 趋势均线类型：`sma` / `ema` |
| `--scan` | `0` | 扫描最近 N 根 K 线的历史信号（默认 0=仅检测最新一根） |
| `--backtest` | `0` | 对最近 N 根 K 线上的信号做回测（WIN/LOSS/OPEN + 胜率） |
| `--volume` | 关 | 启用量能确认（放量加权 / 缩量降级 / 弱量低盈亏比过滤），默认关闭 |
| `--entry-mode` | `close` | 入场方式：`close`=形态收盘价（原有行为）；`breakout`=等价格越过形态极值才进场 |
| `--signal-mode` | `pattern` | 信号来源：`pattern`=形态触发（原有行为）；`trend`=纯趋势（只看均线斜率，**不使用任何形态**） |
| `--atr-mult` | `0` | 止损改为 N×ATR(14)（`0`=结构止损，原有行为）。第八轮唯一被数据支持的成分 |
| `--min-gap` | `0` | 同形态最小间隔根数（`pinbar`/吞没 分类计算），用于降频；`0`/`1`=不过滤 |
| `--preset` | 无 | 参数预设：`legacy`=原有默认；`candidate`=复盘实测最好的候选（**不可交易**）；`trend_wide`=纯趋势+宽止损（**不可交易**）；`optimal` 为 `candidate` 的历史别名 |

### 参数预设

`--preset` 只填充**你没显式指定**的参数（命令行显式值优先）：

```bash
# 套用复盘实测最好的候选（4H + 突破入场 + 结构止损 + 关闭趋势过滤 + 间隔40）
./.venv/bin/python main.py --preset candidate --proxy http://127.0.0.1:1087 --backtest 300

# 只想借用预设的交易对与周期，其余自己定
./.venv/bin/python main.py --preset candidate --bar 1H      # 周期用 1H，其余取预设
```

> ⚠️ **`candidate` 不可交易**。复盘中它"方向为正但统计不显著"
> （t=1.84、Bootstrap 95%CI 含 0），计入滑点与资金费后优势进一步衰减，
> 在 BTC 上甚至显著为负（t=−3.63）。**仅供研究，不要据此投入资金。**
> 选择该预设时程序会在 stderr 打印醒目告警。详见 `STRATEGY_CONCLUSIONS.md`。

**关于命名**：该预设早期叫 `optimal`（"最优"）。改名是因为**名字在误导**——
它字面承诺最优，实际结论是"不可交易"；虽已配套告警，但名称本身就在暗示可依赖。
现名 `candidate`（候选）**不再许诺任何优越性**。`optimal` 作为**输入别名**保留，行为等价。

**关于间隔取值（`min_gap=40`）**：该值取自实测最优，而非随手选的：

| 间隔 | 基准（仅手续费） | 全叠加摩擦后 | 衰减 |
|---|---|---|---|
| 8（旧值） | +0.065R / t=1.38 | +0.024R / t=0.51 | **削 63%** ⚠️ |
| 20 | +0.040R / t=0.62 | +0.000R / t=0.00 | 归零 |
| **40（现值）** | **+0.161R / t=1.84** ← 全表最高 | **+0.123R / t=1.41** | 削 24% |

g=40 点估计最高、抗成本也最强；g=8 反而是对成本最脆弱的一档。
> ⚠️ 但请注意：**g=40 的 t 仍 < 2、CI 仍含 0**。调这一档**不会**让策略变得可交易，
> 只是让预设更忠于它声称的含义。

> 注意：CLI 的"原有行为"基线是**趋势过滤关闭**（`--trend 0`），
> 因此不指定 `--trend` 不会触发告警；这与仪表盘（默认开启趋势过滤）不同。

### `trend_wide` 预设：纯趋势 + 宽止损

第八轮结论指向一个**抛开形态**的配置：趋势方向用 **MA20 斜率**判定（阈值 `0.2 × ATR / price`，
超过才认趋势，否则视为横盘不发信号），止损放宽到 **1.5 × ATR(14)**，盈亏比 2:1。

```bash
./.venv/bin/python main.py --preset trend_wide --proxy http://127.0.0.1:7890 --backtest 300
# 等价于（可逐项覆盖）：
./.venv/bin/python main.py --signal-mode trend --trend 20 --atr-mult 1.5 --bar 4H --backtest 300
```

> ⚠️ **`trend_wide` 同样不可交易，而且理由比 `candidate` 更微妙**：
> 它确实是八个预设里**唯一净期望为正且 t > 1.9** 的（BTC +0.0398R / t=+1.93），
> 但与"**同方向、同止损宽度**的随机入场"相比，α = −0.019（z=−0.22）——
> 也就是说**那个正 t 来自市场自身的漂移，不是靠趋势判定的功劳**。
> 去掉形态让策略"更像正期望"，但这属于**误差方向的巧合**，不是可复制的优势。

> ⚠️ **其中的 `atr_mult=1.5` 是「保守取值」，不是「最优值」**。止损宽度扫描显示两品种的
> argmax **不一致**：ETH 单调升至**网格边界 k=3.0**（+0.0833R / t=+2.76，k=1.5 只有 +0.0182R），
> BTC 峰值在 **k=2.0**（+0.0550R / t=+1.81，k=3.0 已转负 −0.0517R）。两品种不一致 +
> ETH 落在网格边界（"闸门 5 子检查"警告的陷阱）+ 形态源与随机源曲线**形状一致**（纯机械的成本效应）
> ⇒ **不存在可择优的唯一 k**。完整分析见 [`OPTIMAL_PARAMS.md`](OPTIMAL_PARAMS.md)。

### 止损宽度（`--atr-mult`）：第八轮唯一被数据支持的成分

这是文档里唯一一个**被数据正面支持**的改动，但它支持的是**风险管理**而非方向预测：

| 止损口径 | f（费率占 R 比例，中位） | 说明 |
|---|---|---|
| 结构止损（影线/两 K 极值外侧） | **0.060R** | 原有默认（`--atr-mult 0`） |
| `1.5 × ATR(14)` | **0.036R** | 手续费吃掉的比例降 40% |

`f` 越小，同样的毛期望扣掉成本后剩得越多。BTC 净期望因此从 −0.0718R 升到 +0.0375R。

> 但**必须说清楚**：把"形态源"换成"完全随机源"重跑同一条扫描曲线，
> **形状一致**（见 `probe_pa_stopwidth.py`）。所以这个改善**来自成本，不是来自信息**——
> 宽止损不预测方向，只是让手续费少啃掉一点 R。

**取更远的一侧**：ATR 止损若比结构止损**更贴近入场**，程序会保留结构止损（取离入场更远者），
避免把风险缩到形态边界之内。`--atr-mult 0`（默认）时完全不启用 ATR，与改造前逐字节一致。

### 环境变量覆盖

所有参数都可用 `PA_*` 环境变量覆盖（优先级低于命令行显式值、高于预设）：

| 环境变量 | 对应参数 |
|----------|----------|
| `PA_PRESET` | 预设名（`legacy` / `candidate` / `trend_wide`；`optimal` 为历史别名） |
| `PA_TICKER` / `PA_INTERVAL` | 交易对 / K 线周期 |
| `PA_ENTRY_MODE` | 入场方式（`close` / `breakout`） |
| `PA_ATR_MULT` | 止损 ATR 倍数（0 = 结构止损） |
| `PA_SIGNAL_MODE` | 信号来源（`pattern` / `trend`） |
| `PA_TREND_PERIOD` | 均线周期（0 = 关闭趋势过滤） |
| `PA_MA_TYPE` | 均线类型（`sma` / `ema`） |
| `PA_VOLUME_FILTER` | 量能过滤（1/0） |
| `PA_MIN_GAP` | 同形态最小间隔（根） |
| `PA_RR` | 盈亏比 |

统一配置层见 `strategy_config.py`（含预设定义、未验证配置识别与告警）。

**优先级**（低 → 高）：内置默认值 → 预设值 → `PA_*` 环境变量 → 命令行显式值。

```bash
# 环境变量对 CLI 同样生效（不必每次敲长参数）
PA_ATR_MULT=1.5 PA_SIGNAL_MODE=trend ./.venv/bin/python main.py --bar 4H --trend 20

# 命令行显式值仍然胜出（这里 atr_mult 用 3 而非环境变量的 1.5）
PA_ATR_MULT=1.5 ./.venv/bin/python main.py --atr-mult 3
```

## 信号输出格式

触发信号时打印紧凑 JSON，字段与 TradingView Webhook 约定一致：

```json
{"action":"BUY","ticker":"BTC-USDT-SWAP","price":99.9,"sl":96.7015,"tp":106.297,"strategy":"bullish_pinbar"}
```

| 字段 | 说明 |
|------|------|
| `action` | `BUY` / `SELL` |
| `ticker` | 交易对 |
| `price` | 入场价（形态收盘价） |
| `sl` | 止损价 |
| `tp` | 目标价（固定 2:1 盈亏比，tp = 入场 ± 2×risk） |
| `strategy` | 形态名称：`bullish_pinbar` / `bearish_pinbar` / `bullish_engulfing` / `bearish_engulfing`；`--signal-mode trend` 时为 `trend_follow` |

> 附加字段 `risk`、`rr` 便于排查，不参与 TradingView 必需字段。

启用量能后（`--volume`），信号会额外包含量能确认字段（见下方"量能确认"）：
```json
{"action":"BUY","ticker":"BTC-USDT-SWAP","price":102.0,"sl":97.3998,"tp":111.2005,
 "strategy":"bullish_engulfing","risk":4.6003,"rr":2.0,
 "volume_ratio":5.0,"volume_signal":"strong","volume_confirm":"放量吞没"}
```

## 关于"未检测到信号"

脚本**默认只检测最新一根 K 线**是否构成 Pinbar/吞没。绝大多数时候，最新一根
恰好构成标准形态的概率不高，因此打印"未检测到价格行为信号"是**正常现象**，
不代表脚本或数据有问题。

若想确认数据链路与形态检测工作正常，用 `--scan N` 扫描最近 N 根 K 线的历史信号，
例如 `--scan 100` 会在最近 100 根里统计出若干 Pinbar / 吞没信号。

## 信号回测（判断信号质量）

用 `--backtest N` 可验证历史信号"如果真按它交易"的结局：对扫描到的每个信号，
跟踪其**之后**的价格走势，判断是止盈、止损还是仍在持仓。

```bash
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --backtest 300
```

输出示例（节选）：
```
[1789479300000] bearish_engulfing action=SELL entry=76979.7 sl=77227.1 tp=76485   rr=2.0  ->  ✅ 止盈成功
[1789469400000] bearish_engulfing action=SELL entry=76950.8 sl=77114.2 tp=76624.1 rr=2.0  ->  ❌ 止损触发
[1789554900000] bullish_pinbar    action=BUY  entry=75971   sl=75855.2 tp=76202.5 rr=2.0  ->  ⏳ 持仓中
—— 回测最近 299 根 5m K 线：共 104 个信号，止盈 31 / 止损 63 / 持仓中 10，已平仓胜率 33.0% ——
```

判定规则（对 BUY 信号，SELL 对称）：
- ✅ **止盈**：后续某根 `high ≥ tp`（且未先触 sl）
- ❌ **止损**：后续某根 `low ≤ sl`（若同一根同时触及两者，按保守计为止损）
- ⏳ **持仓中**：到数据末尾仍未触及任一价位

> 回测仅用 OKX 公开历史 K 线做**事后**统计，不模拟滑点/手续费，结果偏理想。
> 胜率低（如 33%）但配合 2:1 盈亏比，期望值约为 33%×2 − 67%×1 ≈ 0，
> 说明该简单形态在所选周期下单独使用并无显著正期望，属于信号质量评估信息。

## 量能确认（可选）

量能**不作为独立的买入/卖出信号**，只作为现有价格行为信号（Pinbar/吞没）的
**确认或降级**条件，默认关闭，用 `--volume` 开关启用：

```bash
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --volume
```

**量能比率** = 当前 K 线成交量 ÷ 过去 20 根 K 线平均成交量（不含当前，无前视偏差）。

**信号加权 / 降级**：

| 量能比率 | 状态 | 效果 |
|---------|------|------|
| ≥ 1.2 | 放量 | 信号**加权**（确认）：标记"放量Pinbar" / "放量吞没" |
| < 0.8 | 缩量 | 信号**减权**（降级）：标记"缩量Pinbar" / "缩量吞没" |
| 0.8 ~ 1.2 / 无法计算 | 中性 | 不加权不减权 |

**过滤规则**：若 `volume_signal` 为 `weak`（缩量）**且** 盈亏比 < 2，过滤掉该信号
（弱量 + 低盈亏比 = 不值得冒险）。

**输出字段**（启用量能后附加在信号 JSON 中）：

| 字段 | 说明 |
|------|------|
| `volume_ratio` | 量能比率（数值） |
| `volume_signal` | `strong` / `weak` / `neutral` |
| `volume_confirm` | 形态量能标签："放量Pinbar" / "缩量Pinbar" / "放量吞没" / "缩量吞没"（中性时省略） |

> 不传 `--volume` 时，信号 JSON 与之前完全一致，不输出任何量能字段（量能可选）。
> 注意：当前入场采用**固定 2:1 盈亏比**，因此"盈亏比 < 2"在固定盈亏比下不会触发；
> 该过滤逻辑保留，待盈亏比改为动态时即自动生效。

## 趋势过滤

用均线判断大趋势，**只顺势交易**：上升趋势（收盘 > 均线）只保留 BUY，
下降趋势（收盘 < 均线）只保留 SELL，过滤掉逆势信号。

```bash
# 启用 50 周期 SMA 趋势过滤
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --trend 50

# 指定 EMA 均线
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --trend 50 --ma-type ema

# 回测中对比趋势过滤的效果
./.venv/bin/python main.py --proxy http://127.0.0.1:7890 --bar 1H --backtest 100 --trend 50
```

过滤规则：
- 均线只使用**信号所在 K 线（含）之前**的数据计算，**无前视偏差**
- 信号处收盘 > 均线 → 上升趋势，只留 BUY
- 信号处收盘 < 均线 → 下降趋势，只留 SELL
- 数据不足无法算均线时，保留信号（不误杀）

### 实测效果（BTC 真实行情回测）

| 周期 | 趋势过滤 | 信号数 | 已平仓胜率 |
|------|---------|--------|-----------|
| 5m | 无 | 103 | 33.7% |
| 5m | SMA50 | 60 | 26.8% ⬇️ |
| 1H | 无 | 44 | 31.6% |
| 1H | SMA20 | 25 | **40.0%** ⬆️ |
| 1H | SMA50 | 21 | **41.2%** ⬆️ |

> **结论**：趋势过滤在 **1H 等较大周期**上显著提升胜率（31.6% → 40%+），
> 但在 **5m 等高噪声周期**上反而降低胜率（33.7% → 26.8%）。
> 原因是均线趋势过滤依赖"价格相对有序运动"，周期越小噪声越大、过滤越容易误杀有效信号。
> 建议在较大周期上使用趋势过滤，并可用 `--backtest` 自行对比不同 `--bar` / `--trend` 参数。

## 形态判定规则

**Pinbar（针形线）**
- 影线长度 ≥ 实体长度的 **2 倍**
- 看涨：下影线长，收盘价位于 K 线高点的顶部 **1/3** 区域
- 看跌：上影线长，收盘价位于 K 线低点的底部 **1/3** 区域

**吞没（Engulfing）**
- 当前 K 线实体**完全覆盖**前一根 K 线实体，且方向相反
- 看涨吞没：前一根阴线被当前阳线实体覆盖
- 看跌吞没：前一根阳线被当前阴线实体覆盖

## 入场 / 止损 / 目标

| 项 | 规则 |
|----|------|
| 入场价 | `close`（默认）：形态收盘价；`breakout`：形态极值（看涨取最高价、看跌取最低价） |
| 止损价 | Pinbar：影线**极值外侧**；吞没：两根 K 线**极值外侧**；`--atr-mult N` 启用时改用 N×ATR(14)（取更远侧） |
| 目标价 | 固定 **2:1**（Reward = 2 × Risk），可用 `--rr` 调整 |

> `--signal-mode trend` 时入场价恒为**当根收盘价**，止损恒为 **N×ATR（默认 1.5）**，
> 不使用任何形态极值（因为它不检测形态）。

### 入场方式（`--entry-mode`）

| 取值 | 含义 | 实盘下单 |
|------|------|----------|
| `close`（默认） | 形态一出现即按收盘价入场 | 限价单 |
| `breakout` | 等价格**真越过形态极值**才进场 | **需触发单**（当前自动交易仅实现限价单） |

复盘显示 `breakout` 可过滤"形态出现但没跟风"的假信号，但它把入场价推高、
风险同步放大，且实盘需要触发单支持——属于**未通过独立验证**的改动。

### 同形态最小间隔（`--min-gap`）

形态会**连续聚集**出现（同一位置反复给出同类信号），后续信号往往是"追单"、质量更差。
`--min-gap N` 让同类形态（`pinbar` / 吞没 各自计）之间至少相隔 N 根 K 线：

```bash
./.venv/bin/python main.py --proxy http://127.0.0.1:1087 --min-gap 8 --backtest 300
```

0 与 1 等价（都不过滤），复盘中有效区间约 **8~40**，越大约降频但样本越少。

## 接入 Webhook（第二阶段）

第一阶段的信号引擎把 JSON 打印到控制台。要真正自动下单，可搭配第二阶段的
**Webhook 信号执行器**（`webhook_server/`）：它接收 TradingView Alert 发来的
`{action, ticker, price, sl, tp, strategy}`，并自动在 **OKX 模拟盘**下限价单 +
止盈止损。

```bash
export OKX_API_KEY=xxx OKX_API_SECRET=xxx OKX_PASSPHRASE=xxx
./.venv/bin/uvicorn webhook_server.app:app --host 0.0.0.0 --port 9000
```

详情见 [webhook_server/README.md](webhook_server/README.md)（含 OKX 接口规格、签名方式、
TradingView 配置、模拟盘安全说明）。

## Web 仪表盘（第三阶段）

一个 Flask + lightweight-charts 的单页面仪表盘，展示 K 线与价格行为信号，并提供手动下单入口
（当前下单为 mock）。复用第一阶段检测逻辑。

```bash
export OKX_PROXY=http://127.0.0.1:1087     # 拉 K 线走代理（OKX 被墙时）
./.venv/bin/python -m web_dashboard.app
# 浏览器打开 http://127.0.0.1:8000
```

详情见 [web_dashboard/README.md](web_dashboard/README.md)。

### 参数与安全闸门

仪表盘工具栏新增两个控件：**入场**（`收盘价` / `突破极值`）与**间隔**（同形态最小间隔根数）；
回测弹窗也有对应的 `入场` / `间隔` 项。默认值分别为 `close` 与 `0`，**即原有行为不变**。

自动交易使用**未验证配置**时的保护：

| 偏离类型 | 例子 | 处理 |
|----------|------|------|
| **阻断类** | `entry_mode=breakout`（需触发单）、`candidate` 预设 | 启动前必须勾选确认，否则服务端返回 409 拒绝 |
| **仅提示类** | 单独关闭趋势过滤 | 只在告警横幅提示，不阻断启动 |

告警横幅在自动交易弹窗内显示（红框），并在启动时写入运行日志（可追溯）。
"仅提示类"不阻断是有意设计——若任何微调都弹确认，用户会习惯性点过，反而失去警示作用。

> ⚠️ `breakout` 入场在实盘中需要**触发单**，而当前自动交易只实现了限价单。
> 因此用 `breakout` 跑自动交易时，实际会立刻按形态极值挂限价单，
> 与"等突破"的语义**不完全一致**。要真正实盘使用，需先补齐触发单下单通道。

### 余额不足时的行为（重要）

信号表格里若出现**信号数量正常、但整列都是「余额不足」**，这**不是**参数失效，而是
**账户余额开不出 OKX 的最小下单量**。两者必须区分开：

| 路径 | `require_affordable` | 行为 |
|------|---------------------|------|
| **展示**（`/api/signals`、表格、自动交易日志） | `False` | **保留**信号，打 `affordable=False` 标记 + `required_balance`，把原因说清楚 |
| **下单**（`/api/order`、自动交易执行） | `True` | **丢弃**开不出的信号，绝不建议/提交开不出的仓 |

> **为什么要这样分**：早期展示路径也用 `True`（丢弃），结果是余额小时**信号表格永远为空**，
> 用户会误判为"策略没信号 / 参数选了没生效"，而真相是余额不足。展示要给人**原因**，
> 下单才要**严格**。

界面上会给出：顶部黄色提示条（含每笔预算、最小下单名义价值、所需余额）、汇总里的
`可下单信号 0 / N` 指标、以及每行的「开不出」徽章。自动交易日志同样区分
"**本轮无信号**"（真的没有形态）与"**有 N 个信号但余额不足全部开不出**"。

以 BTC 为例：`minSz = 0.01 张`，1 张 = `ctVal = 0.01 BTC`，
价格 84,709 时最小下单名义 = `0.01 × 0.01 × 84709 ≈ 8.47 USDT`；
而余额 8.36 USDT 时每笔预算仅 `8.36 × 3 ÷ 10 = 2.51 USDT` → 开不出，
所需余额 `≈ 8.47 × 10 ÷ 3 ≈ 28.24 USDT`。

## 测试

```bash
# 第一阶段：形态 + 趋势 + 量能
./.venv/bin/python -m unittest test_patterns test_trend test_volume -v
# 策略配置层 + 新增参数（入场方式 / 最小间隔 / 信号来源 / ATR 止损 / 安全闸门）
./.venv/bin/python -m unittest test_strategy_config -v
# 第二阶段：webhook 执行器（mock 交易所，不连真实网络）
./.venv/bin/python -m unittest webhook_server.test_okx_trading -v
# 第三阶段：Web 仪表盘接口
./.venv/bin/python -m unittest web_dashboard.test_app -v
```

## 目录结构

```
price-action-signals/
├── okx_client.py        # OKX 公开 API 客户端（K 线拉取，含镜像重试）
├── patterns.py          # Pinbar / 吞没 形态检测 + 入场/止损/目标计算 + 纯趋势信号
├── trend.py             # 均线趋势判断与过滤（SMA/EMA，含斜率判定，无前视偏差）
├── atr.py               # ATR(14) 计算（SMA 口径，无前视）—— 宽止损的输入
├── volume.py            # 量能确认（量能比率 / 加权降级 / 弱量低盈亏比过滤）
├── strategy_config.py   # 策略参数预设 + 未验证配置识别与告警 + 同形态间隔过滤
├── signals.py           # Pattern -> TradingView JSON 信号序列化
├── main.py              # 主入口（CLI）
├── revalidate_eth4h.py       # 独立复核①：八项检验（区间稳健性/分季度/随机入场/Bootstrap）
├── revalidate_walkforward.py # 独立复核②：扩张窗口 walk-forward + 三项自动判定
├── revalidate_frictions.py   # 独立复核③：滑点与资金费敏感性阶梯
├── probe_pa_principles.py    # 第八轮：第一性原理（位置/趋势状态/强度 三维度分层）
├── probe_pa_unified.py       # 第八轮★：鞅检验 + 匹配随机入场 α + 漂移分解 + MFE/MAE
├── probe_pa_synthesis.py     # 第八轮：漂移分层 + 纯趋势对照（不含形态）+ 波动率切换
├── probe_pa_decompose.py     # 第八轮：2×2 分解（形态 × 止损口径）
├── probe_pa_stopwidth.py     # 第八轮：止损宽度扫描 k=0.5~3.0（形态源 vs 随机源）
├── probe_funding.py          # 方向评估：资金费历史可利用性（carry）
├── probe_momentum.py         # 方向评估：时序动量 TSMOM 多周期网格
├── probe_xsec.py             # 方向评估：多品种横截面动量
├── test_patterns.py     # 形态检测单元测试（纯算法，无需网络）
├── test_trend.py        # 趋势过滤单元测试
├── test_volume.py       # 量能确认单元测试
├── test_strategy_config.py  # 配置层 + 新增参数 + 自动交易安全闸门测试
├── _env_guard.py        # 测试辅助：临时设置/清除 PA_* 环境变量（退出必复原）
├── requirements.txt     # 依赖
├── README.md
├── webhook_server/      # 第二阶段：Webhook 信号执行器（OKX 模拟盘下单）
│   ├── app.py           #   FastAPI 服务：接收信号 -> 下单 -> 记录
│   ├── okx_trading.py   #   OKX 模拟盘客户端：签名 + 限价单 + attachAlgoOrds
    ├── storage.py       #   本地 SQLite 订单记录
    ├── test_okx_trading.py
    ├── requirements.txt
    └── README.md
└── web_dashboard/        # 第三阶段：Web 信号仪表盘（Flask + lightweight-charts）
    ├── app.py            #   Flask 后端：/api/klines, /api/signals, /api/order(mock)
    ├── templates/
    │   └── index.html    #   单页前端（lightweight-charts CDN）
    ├── test_app.py       #   接口单元测试
    └── README.md
```


> **注意**：OKX 域名（`www.okx.com` / `aws.okx.com`）在部分网络（尤其中国大陆）会被 **DNS 污染**，
> 导致 `nodename nor servname provided` 或 `502` 之类的连接错误。
> 脚本已内置三端点镜像切换、系统代理读取、指数退避重试，并输出友好的诊断信息。
> 若仍无法访问，按诊断提示依次尝试：
> 1. `--proxy http://127.0.0.1:7897`（**替换为你的代理端口**；本机当前可用的是 `7897`，
>    旧的 `1087` 已停用。先用 `lsof -nP -iTCP -sTCP:LISTEN | grep -E '789[0-9]'` 确认实际监听端口）
> 2. `--host <可访问的镜像端点>`
> 3. `--mock` 离线演示
