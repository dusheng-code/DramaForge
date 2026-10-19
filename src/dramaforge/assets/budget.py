"""职责：预算账本（§10.2）：spent / reserved / 单集上限的唯一判定处，触顶时给出降级动作。
输入：AssetStore 的聚合查询、Settings 的金额上限、任务 cost_cap_cny。
输出：BudgetSnapshot 与 allow / episode_full / trace 结论；触顶标记供 pump 置 BUDGET_HOLD。

副作用：只读库；写日志。禁止在此提交 provider 请求。
"""

from __future__ import annotations

from loguru import logger

from ..contracts.assets import BudgetSnapshot
from ..core.config import Settings


class BudgetLedger:
    """预算账本：所有花钱动作前必须经过 allow()。"""

    def __init__(self, store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self._exceeded_emitted: set[str] = set()

    def cap_for(self, task_cap_cny: float | None) -> float:
        """任务有效上限 = min(MAX_VIDEO_COST_CNY, task.cost_cap_cny)。无副作用。"""
        ceiling = float(self.settings.max_video_cost_cny)
        if task_cap_cny is None or float(task_cap_cny) <= 0:
            return ceiling
        return min(ceiling, float(task_cap_cny))

    async def snapshot(self, task_id: str, task_cap_cny: float | None) -> BudgetSnapshot:
        """读取账本快照（spent 含 REJECTED/ORPHANED；reserved 只算当前 revision 在途）。副作用：读库。"""
        snap = await self.store.budget_snapshot(
            task_id,
            cap_cny=self.cap_for(task_cap_cny),
            episode_cap_cny=float(self.settings.max_episode_cost_cny),
        )
        return snap

    async def allow(
        self,
        task_id: str,
        episode: int,
        next_est_cny: float,
        task_cap_cny: float | None = None,
    ) -> bool:
        """判定是否允许提交下一笔（总量 + 单集双闸）。副作用：读库。"""
        snap = await self.snapshot(task_id, task_cap_cny)
        ok = snap.allow(float(next_est_cny), int(episode))
        if not ok:
            logger.warning(
                "budget denied: task={} ep={} next={} spent={} reserved={} cap={} ep_cap={}",
                task_id,
                episode,
                next_est_cny,
                snap.spent_cny,
                snap.reserved_cny,
                snap.cap_cny,
                snap.episode_cap_cny,
            )
        return ok

    def once(self, key: str) -> bool:
        """BUDGET_EXCEEDED 每任务只发一次的开关。副作用：内存标记。"""
        if key in self._exceeded_emitted:
            return False
        self._exceeded_emitted.add(key)
        return True

    def reset(self) -> None:
        """清空去重标记（测试与追加预算后使用）。副作用：内存清理。"""
        self._exceeded_emitted.clear()


def estimate_for(settings: Settings, kind: str, tier: str, duration_sec: float = 0.0) -> float:
    """估算单条资产花费（quote 与实际提交前都用它）。无副作用。"""
    from ..providers.pricing import est_keyframe_cost, est_video_cost

    if kind == "video":
        return est_video_cost(tier, duration_sec, settings)
    return est_keyframe_cost(settings)
