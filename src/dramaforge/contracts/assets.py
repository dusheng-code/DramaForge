"""职责：provider 与资产层的请求/回执数据形状（§10.4），以及资产计价所需的标量模型。
输入：pump 组装的请求、provider 回执 JSON。
输出：VideoRequest / VideoOutcome / ImageRequest / ImageResult / BudgetSnapshot。

副作用：无。禁止在此发起网络请求（网络只在 providers/*.py 与 assets/*.py）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .enums import AssetKind, Resolution, Tier


class VideoRequest(BaseModel):
    """图生视频请求：image_ref 为首帧本地路径或 URL，字段名对齐 §10.4。"""

    shot_id: str = ""
    image_ref: str
    motion_prompt: str
    duration_sec: float
    resolution: Resolution = "768p"
    aspect: str = "16:9"
    seed: int | None = None
    tier: Tier = "fast"
    model: str = ""
    image_to_video: bool = True
    generate_audio: bool = False


class VideoOutcome(BaseModel):
    """poll/submit 回执：三态 done/failed/pending，cost_cny 以 provider 真实回执优先。"""

    done: bool = False
    failed: bool = False
    cost_cny: float = 0.0
    url: str | None = None
    error: str | None = None
    provider_task_id: str | None = None
    status: str | None = None
    raw: dict[str, Any] = {}


class ImageRequest(BaseModel):
    """图像请求：定妆图与首帧共用。"""

    kind: AssetKind = "anchor_image"
    shot_id: str = ""
    prompt: str
    ref_image: str | None = None
    resolution: Resolution = "768p"
    aspect: str = "16:9"
    model: str = ""
    seed: int | None = None


class ImageResult(BaseModel):
    """图像回执：path 为落盘前的结果地址（本地 pump 负责下载）。"""

    done: bool = True
    failed: bool = False
    url: str | None = None
    b64: str | None = None
    cost_cny: float = 0.0
    error: str | None = None
    raw: dict[str, Any] = {}


class BudgetSnapshot(BaseModel):
    """预算账本快照（§10.2），供 quote/预算闸/前端共用。"""

    task_id: str
    spent_cny: float = 0.0
    reserved_cny: float = 0.0
    cap_cny: float = 0.0
    episode_spent: dict[str, float] = {}
    episode_cap_cny: float = 0.0
    episode_hold: list[int] = []

    @property
    def remaining_cny(self) -> float:
        """剩余可用额度 = cap - spent - reserved（可为负）。无副作用。"""
        return self.cap_cny - self.spent_cny - self.reserved_cny

    def allow(self, next_est_cny: float, episode: int) -> bool:
        """判定是否允许提交下一笔；单集与总量同时受约束。无副作用。"""
        if self.spent_cny + self.reserved_cny + next_est_cny > self.cap_cny:
            return False
        if self.episode_cap_cny > 0:
            used = self.episode_spent.get(str(episode), 0.0)
            if used + next_est_cny > self.episode_cap_cny:
                return False
        return True
