"""职责：agents 层的依赖容器（AgentContext）：把 settings / 两个 store / 事件 / 路径 / LLM 工厂 / 检索器一次性注入各 agent。
输入：Settings 与已建好的 store、事件总线。
输出：`AgentContext` 实例；`llm(role)` 按角色返回可用的结构化客户端（dry-run 时返回 FakeLLMClient）。

副作用：无（构造期不发起网络/IO；LLM 客户端惰性创建）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from ..core.config import Settings
from ..core.events import Events
from ..io import artifacts as io
from ..validators.registry import GateContext


def _default_paths(task_id: str) -> io.TaskPaths:
    """默认路径工厂（读全局 Settings）。无副作用。"""
    return io.paths(task_id)


@dataclass
class AgentContext:
    """agents 共享依赖。所有 handler 通过它取服务，避免全局单例。"""

    settings: Settings
    store: Any
    assets: Any
    events: Events
    path_fn: Callable[[str], io.TaskPaths] = _default_paths
    llm_factory: Callable[[str], Any] | None = None
    cards_repo: Any | None = None
    milvus_repo: Any | None = None
    image_client: Any | None = None
    video_client: Any | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ── 服务访问 ───────────────────────────────────────────
    def paths(self, task_id: str) -> io.TaskPaths:
        """取任务产物目录（已 ensure）。副作用：mkdir。"""
        return self.path_fn(task_id).ensure()

    def llm(self, role: str) -> Any:
        """取某 role 的结构化客户端；未注入工厂时按配置构造真实客户端。无副作用。"""
        if self.llm_factory is not None:
            return self.llm_factory(role)
        from ..llm.client import LLMClient

        return LLMClient.from_role(role, self.settings)

    def gate_context(self, task_id: str, revision: int) -> GateContext:
        """从磁盘产物装配 GateContext（gate 阶段的唯一输入）。副作用：读磁盘。"""
        paths = self.paths(task_id)
        shots_raw = io.read_json(paths.shots_json, default={}) or {}
        packs_raw = io.read_json(paths.packs_json, default={}) or {}
        chars_raw = io.read_json(paths.characters_json, default={}) or {}
        blocks_raw = io.read_json(paths.blocks_json, default={}) or {}
        scenes_raw = io.read_json(paths.scenes_json, default={}) or {}
        cons_raw = io.read_json(paths.constraints_json, default={}) or {}
        cards_raw = io.read_json(paths.cards_used_json, default={}) or {}
        episodes_text = _concat_episodes(paths)
        from ..contracts.artifacts import (
            Block,
            Character,
            ConstraintSpec,
            PromptPack,
            Scene,
            SegmentChapter,
            Shot,
        )

        return GateContext(
            task_id=task_id,
            revision=revision,
            shots=[Shot.model_validate(x) for x in shots_raw.get("shots", [])],
            packs=[PromptPack.model_validate(x) for x in packs_raw.get("packs", [])],
            episodes_text=episodes_text,
            blocks=[Block.model_validate(x) for x in blocks_raw.get("blocks", [])],
            characters=[Character.model_validate(x) for x in chars_raw.get("characters", [])],
            chapters=[SegmentChapter.model_validate(x) for x in cons_raw.get("chapters", [])],
            constraints=[ConstraintSpec.model_validate(x) for x in cons_raw.get("constraints", [])],
            cards=list(cards_raw.get("cards", [])),
            scenes=[Scene.model_validate(x) for x in scenes_raw.get("scenes", [])],
            payoff_tags=dict(cons_raw.get("payoff_tags", {})),
            ref_tag_dist=dict(cons_raw.get("ref_tag_dist", {})),
            settings=self.settings,
        )

    def load_characters(self, task_id: str) -> list[Any]:
        """读 characters.json（供 prompt_pack / storyboard 使用）。副作用：读磁盘。"""
        from ..contracts.artifacts import Character

        raw = io.read_json(self.paths(task_id).characters_json, default={}) or {}
        try:
            return [Character.model_validate(x) for x in raw.get("characters", [])]
        except Exception as exc:  # noqa: BLE001 —— 脏数据要暴露但不炸整条链路
            logger.error("characters.json invalid: {}", exc)
            return []

    def load_shots(self, task_id: str) -> list[Any]:
        """读 shots.json。副作用：读磁盘。"""
        from ..contracts.artifacts import Shot

        raw = io.read_json(self.paths(task_id).shots_json, default={}) or {}
        return [Shot.model_validate(x) for x in raw.get("shots", [])]

    def load_packs(self, task_id: str) -> list[Any]:
        """读 packs.json。副作用：读磁盘。"""
        from ..contracts.artifacts import PromptPack

        raw = io.read_json(self.paths(task_id).packs_json, default={}) or {}
        return [PromptPack.model_validate(x) for x in raw.get("packs", [])]

    def load_constraints(self, task_id: str) -> list[Any]:
        """读 constraints.json 里的 ConstraintSpec 列表。副作用：读磁盘。"""
        from ..contracts.artifacts import ConstraintSpec

        raw = io.read_json(self.paths(task_id).constraints_json, default={}) or {}
        return [ConstraintSpec.model_validate(x) for x in raw.get("constraints", [])]


def _concat_episodes(paths: io.TaskPaths) -> str:
    """把 script/episodes/ep*.md 拼成全文（forbidden_setting 的输入）。副作用：读磁盘。"""
    if not paths.episodes.exists():
        return ""
    parts: list[str] = []
    for file in sorted(paths.episodes.glob("ep*.md")):
        parts.append(io.read_text(file))
    return "\n\n".join(parts)
