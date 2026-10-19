"""职责：校验器包的装配入口：import 各实现模块以触发 @register，并对上层暴露统一 API。
输入：无（导入即注册）。
输出：GateContext / run_all / build_report / registry 辅助函数。

副作用：导入期向注册表登记 18 个校验器；其余无副作用。禁止在此写业务判定。
"""

from __future__ import annotations

from . import numeric, risk, structural, text  # noqa: F401  (导入即注册)
from .registry import (
    GateContext,
    ValidatorMeta,
    all_keys,
    build_report,
    cost_impact_keys,
    get,
    make_violation,
    meta,
    needs_human_of,
    register,
    run_all,
    run_one,
    self_report_keys,
    slot_label,
    slot_order,
    value_as_pair,
    violations_of,
)

__all__ = [
    "GateContext",
    "ValidatorMeta",
    "all_keys",
    "build_report",
    "cost_impact_keys",
    "get",
    "make_violation",
    "meta",
    "needs_human_of",
    "register",
    "run_all",
    "run_one",
    "self_report_keys",
    "slot_label",
    "slot_order",
    "value_as_pair",
    "violations_of",
]
