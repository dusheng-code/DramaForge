"""职责：API 出入参的 pydantic 模型（与 contracts 解耦：这里只放 HTTP 形状，不承载业务状态）。
输入：HTTP 请求体 / 查询参数。
输出：可校验的请求模型与统一响应模型。

副作用：无（纯模型定义）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..contracts.enums import ResolveAction


class SubmitTaskRequest(BaseModel):
    """POST /api/drama/tasks 请求体（字段名与 §12 契约一致）。"""

    topic: str = Field(min_length=1, max_length=200)
    platform: str = "douyin"
    episode_count: int = Field(default=8, ge=1, le=100)
    duration_target: int = Field(default=75, ge=10, le=600)
    cost_cap_cny: float = Field(default=500.0, gt=0)
    strict_mode: bool = False
    overrides: dict[str, Any] = {}
    task_id: str | None = None


class SubmitTaskResponse(BaseModel):
    """提交任务的响应。"""

    task_id: str
    status: str = "PENDING"
    stages: int = 0


class ResolveRequest(BaseModel):
    """POST /resolve 请求体（人工决议）。"""

    action: ResolveAction
    target_ids: list[str] = []
    payload: dict[str, Any] = {}
    note: str = ""
    operator: str = "human"


class ResolveResponse(BaseModel):
    """人工决议的响应。"""

    task_id: str
    action: str
    revision: int
    detail: dict[str, Any] = {}


class TaskListResponse(BaseModel):
    """任务列表。"""

    tasks: list[dict[str, Any]] = []


class StageView(BaseModel):
    """阶段行的前端视图（含产物是否仍存在）。"""

    stage: str
    slot_key: str
    revision: int
    status: str
    fail_reason: str | None = None
    output_path: str | None = None
    output_ok: bool = False
    updated_at: str | None = None


class TaskDetailResponse(BaseModel):
    """任务详情 + 当前 revision 的阶段表。"""

    task: dict[str, Any]
    revision: int
    stages: list[StageView] = []
    pending_human: int = 0


class ConstraintsResponse(BaseModel):
    """约束与机读报告。"""

    task_id: str
    revision: int
    constraints: list[dict[str, Any]] = []
    report: dict[str, Any] = {}


class SegmentsResponse(BaseModel):
    """五段位章节。"""

    task_id: str
    slot_key: str
    chapter: dict[str, Any] | None = None
    slots: list[str] = []


class AssetsResponse(BaseModel):
    """资产行 + 报价 + 报告。"""

    task_id: str
    assets: list[dict[str, Any]] = []
    quote: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    budget: dict[str, Any] = {}


class PendingHumanResponse(BaseModel):
    """待人工列表。"""

    task_id: str
    items: list[dict[str, Any]] = []


class PackageResponse(BaseModel):
    """交付包（md 文本或 json）。"""

    task_id: str
    as_: Literal["md", "json"] = Field(default="md", alias="as")
    content: str = ""
    path: str = ""

    model_config = {"populate_by_name": True}
