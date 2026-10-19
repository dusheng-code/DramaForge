"""职责：资产计价的唯一口径（§10.1/§10.4）：单价读取、预估成本、期望抽卡次数、provider 回执覆盖规则；纯函数、无 IO。
输入：Settings（TIER_PRICE / IMAGE_PRICE_CNY / MAX_ATTEMPT_PER_SHOT）、档位、时长、期望抽卡次数、历史通过率、provider 回执金额。
输出：UnitPrice 标量（float）或 dict[str, float]；缺配置时抛 PricingError 而不是静默返回 0。

副作用：无。纯函数模块，不读磁盘、不发网络、不写日志。
禁止：在此硬编码单价（§0.4 外部事实一律走配置注入）；单价只能来自 TIER_PRICE / IMAGE_PRICE_CNY。
"""

from __future__ import annotations

import math

from ..core.config import Settings

# §10.1 期望抽卡次数口径：est_cost = 单价 × 时长 × 期望抽卡次数；
# 期望抽卡次数 = 1 / 历史通过率，clamp 到 [1, MAX_ATTEMPT_PER_SHOT]。
# 通过率默认 0.75（§10.1 未给初始值，取经验值；M9 真跑后用 asset_report.pass_rate 回填）。
DEFAULT_PASS_RATE: float = 0.75

# 与 §11 MAX_ATTEMPT_PER_SHOT 默认值一致。本函数签名不带 settings（规格 §10.4 口径），
# 故用模块常量承载上限；改 §11 默认值时必须同步改这里。
# AMBIGUITY(§10.1 vs §11)：规格未说明 expected_attempts 的上限取自配置还是常量，此处取常量并注明同步点。
MAX_ATTEMPT_PER_SHOT: int = 3

# 金额保留位数（§10.1 报价精度）
COST_DECIMALS: int = 3


class PricingError(RuntimeError):
    """计价配置缺失或非法（缺档位单价、单价非正、时长非法）。"""


def _round_cost(value: float) -> float:
    """金额统一保留 COST_DECIMALS 位。无副作用。"""
    return round(float(value), COST_DECIMALS)


def _finite(value: float) -> bool:
    """是否有限实数（过滤 nan / inf）。无副作用。"""
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def parse_tier_prices(raw: str) -> dict[str, float]:
    """`"fast:0.225,hero:0.400"` → {"fast": 0.225, "hero": 0.4}；无冒号/空键/非数值/负数项跳过。

    支持中英文逗号与首尾空白。无副作用（纯解析）。
    """
    out: dict[str, float] = {}
    if not raw:
        return out
    for item in raw.replace("，", ",").split(","):
        item = item.strip()
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        key = key.strip()
        if not key:
            continue
        try:
            price = float(value.strip())
        except ValueError:
            continue
        if not _finite(price) or price < 0:
            continue
        out[key] = price
    return out


def _tier_table(settings: Settings) -> dict[str, float]:
    """优先用 settings.tier_price（已解析属性），不可用时回退解析 TIER_PRICE 原文。无副作用。"""
    table = getattr(settings, "tier_price", None)
    if isinstance(table, dict) and table:
        return {str(k): float(v) for k, v in table.items()}
    return parse_tier_prices(str(getattr(settings, "tier_price_raw", "") or ""))


def video_unit_price(tier: str, settings: Settings) -> float:
    """按档位读 TIER_PRICE 单价（元/秒）；档位缺失或单价非正抛 PricingError。无副作用。

    缺档位是配置缺陷而不是 0 元，必须显式失败，避免静默低估报价（§14.5 单价仍待核实）。
    """
    key = (tier or "").strip()
    table = _tier_table(settings)
    if key not in table:
        raise PricingError(
            f"TIER_PRICE 缺少档位 {key!r}：当前已配置 {sorted(table)}。"
            "请在 .env 的 TIER_PRICE 中补齐（形如 fast:0.225,hero:0.400），"
            "单价属 §14.5 待核实项，未核实前不得硬编码。"
        )
    price = float(table[key])
    if not _finite(price) or price <= 0:
        raise PricingError(
            f"TIER_PRICE 中档位 {key!r} 的单价非法（{table[key]!r}）：必须 > 0。"
            "请到 §14.5 核实真实单价后回填 .env。"
        )
    return price


def image_unit_price(settings: Settings) -> float:
    """读 IMAGE_PRICE_CNY（元/张）；未配置或非正抛 PricingError。无副作用。

    §14 未核实图像真实单价前，该值只用于报价与预算预留，M8 必须与账单核对。
    """
    price = float(getattr(settings, "image_price_cny", 0.0) or 0.0)
    if not _finite(price) or price <= 0:
        raise PricingError(
            f"IMAGE_PRICE_CNY 未配置或非法（{getattr(settings, 'image_price_cny', None)!r}）："
            "请在 .env 填正数单价（M8 前需与 MiniMax 账单核对）。"
        )
    return price


def expected_attempts(pass_rate: float = DEFAULT_PASS_RATE) -> float:
    """把历史通过率折算成期望抽卡次数：1 / pass_rate，clamp 到 [1, MAX_ATTEMPT_PER_SHOT]（§10.1 期望抽卡次数口径）。无副作用。

    pass_rate ≤ 0 或非有限值 → 回退 DEFAULT_PASS_RATE；pass_rate ≥ 1 → 1.0。
    """
    rate = float(pass_rate)
    if not _finite(rate) or rate <= 0:
        rate = DEFAULT_PASS_RATE
    raw_attempts = 1.0 / rate
    clamped = min(max(raw_attempts, 1.0), float(MAX_ATTEMPT_PER_SHOT))
    return round(clamped, COST_DECIMALS)


def est_video_cost(
    tier: str,
    duration_sec: float,
    settings: Settings,
    expected_attempts: float = 1.0,
) -> float:
    """预估单条视频成本 = 单价 × 时长 × 期望抽卡次数，保留 3 位小数（§10.1）。无副作用。

    参数 `expected_attempts` 与同名函数遮蔽是规格给定签名；调用方一般传入
    `pricing.expected_attempts(pass_rate)`。次数 clamp 到 [1, MAX_ATTEMPT_PER_SHOT]。
    注意：本函数是"一次提交"的成本，重抽由调用方按 attempt 逐笔累加。
    """
    unit = video_unit_price(tier, settings)
    duration = float(duration_sec)
    if not _finite(duration) or duration <= 0:
        raise PricingError(f"duration_sec 非法（{duration_sec!r}）：必须为 > 0 的秒数。")
    attempts = float(expected_attempts)
    if not _finite(attempts):
        attempts = 1.0
    attempts = min(max(attempts, 1.0), float(MAX_ATTEMPT_PER_SHOT))
    return _round_cost(unit * duration * attempts)


def est_keyframe_cost(settings: Settings) -> float:
    """预估单张首帧成本 = IMAGE_PRICE_CNY × 期望抽卡次数（§10.1）。无副作用。"""
    return _round_cost(image_unit_price(settings) * expected_attempts())


def est_anchor_cost(settings: Settings) -> float:
    """预估单张定妆图成本 = IMAGE_PRICE_CNY × 期望抽卡次数（§10.1，与首帧同价同口径）。无副作用。"""
    return _round_cost(image_unit_price(settings) * expected_attempts())


def provider_reported_cost(outcome_cost: float, est_cost: float) -> float:
    """provider 回执 cost_cny 优先；回执为 0/缺失/非法时回退预估值（§10.4「可被 provider 回执 cost_cny 覆盖」）。无副作用。

    用于对账：真实计费 > 预估值时以真实为准，避免预算漏记。
    """
    reported = float(outcome_cost or 0.0)
    if _finite(reported) and reported > 0:
        return _round_cost(reported)
    fallback = float(est_cost or 0.0)
    if not _finite(fallback) or fallback < 0:
        fallback = 0.0
    return _round_cost(fallback)
