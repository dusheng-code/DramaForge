"""职责：§8 七个 LLM role 的元数据表（system prompt 文件、输出模型、推荐温度、口径说明）与 `llm/prompts/*.md` 的加载。
输入：role 名或子用途 key（`script.outline` / `script.episode` / `script.characters`）、包内 prompt 文件名。
输出：RoleSpec、输出模型（Pydantic 模型类 / `list[Model]` / `str`）、prompt 正文。

副作用：读取包内 `prompts/*.md` 文件（结果 lru_cache 缓存）；不写磁盘、不发网络、不读环境变量。
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from ..contracts.artifacts import (
    CharacterDraft,
    ConstraintSpecDraft,
    Outline,
    PayoffTagItem,
    PromptPack,
    SegmentNote,
    Shot,
)
from .client import LLMError

PROMPT_DIR_NAME: str = "prompts"
PROMPT_SUFFIX: str = ".md"


@dataclass(frozen=True)
class RoleSpec:
    """单个 role（或其子用途）的调用契约：用哪份 system prompt、产出什么模型、推荐采样温度。"""

    role: str
    system_prompt_name: str
    output_model: Any = None
    temperature: float | None = None
    notes: str = ""
    sub_roles: Mapping[str, "RoleSpec"] = field(default_factory=dict)


# AMBIGUITY(§8 vs §5.2)：§8 表格写 script 的角色输出为 `list[VisualAnchor]`，但 contracts 中
# 面向 script role 的草稿形状是 `CharacterDraft`（character_id 由 Python 按显示名补齐）。
# 这里按 `CharacterDraft` 实现：VisualAnchor 的字段是它的子集，向后兼容，只多不少。
_SCRIPT_SUB_ROLES: dict[str, RoleSpec] = {
    "script.outline": RoleSpec(
        role="script.outline",
        system_prompt_name="script_outline",
        output_model=Outline,
        temperature=0.8,
        notes="总大纲：集数须落在 episode_count_range 内，goal 必须能兑现为画面。",
    ),
    "script.episode": RoleSpec(
        role="script.episode",
        system_prompt_name="script_episode",
        output_model=str,
        temperature=0.8,
        notes="单集剧本 markdown 正文（纯文本，非 JSON），供 blocks 解析。",
    ),
    "script.characters": RoleSpec(
        role="script.characters",
        system_prompt_name="script_characters",
        output_model=list[CharacterDraft],
        temperature=0.6,
        notes="角色草稿：anchor_text 20~120 字且后续每镜 image_prompt 必须包含它。",
    ),
}

ROLE_SPECS: dict[str, RoleSpec] = {
    "reference": RoleSpec(
        role="reference",
        system_prompt_name="reference",
        output_model=list[SegmentNote],
        temperature=0.3,
        notes="只归纳参照卡与榜单统计，不产约束；无证据写进 gaps，basis_ids 必须引用 card_id。",
    ),
    "constraint": RoleSpec(
        role="constraint",
        system_prompt_name="constraint",
        output_model=list[ConstraintSpecDraft],
        temperature=0.2,
        notes="由参照卡统计推约束；无 source_refs 的项由 Python 在落库前丢弃。",
    ),
    "script": RoleSpec(
        role="script",
        system_prompt_name="script_outline",
        output_model=Outline,
        temperature=0.8,
        notes="三个子用途用 key 区分：script.outline / script.episode / script.characters。",
        sub_roles=_SCRIPT_SUB_ROLES,
    ),
    "storyboard": RoleSpec(
        role="storyboard",
        system_prompt_name="storyboard",
        output_model=list[Shot],
        temperature=0.6,
        notes="quality_tier 不由 LLM 决定（§9.2 确定性赋档），duration_sec 只能取 SHOT_DURATION_LADDER 档位。",
    ),
    "prompt_pack": RoleSpec(
        role="prompt_pack",
        system_prompt_name="prompt_pack",
        output_model=list[PromptPack],
        temperature=0.4,
        notes="纯模板拼接：LLM 只生成 motion_prompt 的动作句，image_prompt 的五个标记由模板保证。",
    ),
    "review": RoleSpec(
        role="review",
        system_prompt_name="review",
        output_model=list[str],
        temperature=0.5,
        notes="只产人工提示语（不参与判定），禁止改写剧本或提出自动通过/不通过结论。",
    ),
    "tagger": RoleSpec(
        role="tagger",
        system_prompt_name="tagger",
        output_model=list[PayoffTagItem],
        temperature=0.0,
        notes="输入仅 action + line；标签只允许 PAYOFF_TAGS 封闭枚举，只 warn 不阻断。",
    ),
}


def role_spec(role: str) -> RoleSpec:
    """按 role 名或子用途 key 取 RoleSpec；未知 role/子用途抛 KeyError。无副作用。"""
    base, _, sub = role.partition(".")
    spec = ROLE_SPECS.get(base)
    if spec is None:
        raise KeyError(f"未知 LLM role: {role}（允许：{', '.join(ROLE_SPECS)}）")
    if not sub:
        return spec
    sub_key = f"{base}.{sub}"
    sub_spec = spec.sub_roles.get(sub_key)
    if sub_spec is None:
        raise KeyError(f"role='{base}' 没有子用途 '{sub}'（允许：{', '.join(spec.sub_roles) or '无'}）")
    return sub_spec


def output_model(role: str) -> Any:
    """取该 role（含子用途）的输出模型：Pydantic 模型类、`list[Model]` 或 `str`。无副作用。"""
    return role_spec(role).output_model


def list_roles() -> tuple[str, ...]:
    """七个基础 role 名（顺序同 §8 表格）。无副作用。"""
    return tuple(ROLE_SPECS)


def load_system_prompt(name: str) -> str:
    """读 `dramaforge/llm/prompts/{name}.md` 作为 system prompt。

    副作用：读包内文件（首次读入后缓存）；文件缺失或名字非法时抛 LLMError。
    """
    return _read_prompt(name)


@lru_cache(maxsize=None)
def _read_prompt(name: str) -> str:
    """prompt 文件的实际读取与校验（lru_cache 缓存，避免每集重复读盘）。

    副作用：读包内文件；异常不被缓存，修好文件后再次调用即可恢复。
    """
    safe = (name or "").strip()
    if not safe or any(ch in safe for ch in ("/", "\\", "..", "\x00")):
        raise LLMError(f"非法 prompt 名：{name!r}（只允许 prompts/ 下的文件基名）")
    filename = f"{safe}{PROMPT_SUFFIX}"
    reader = _resource_reader()
    try:
        text = reader(filename)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
        raise LLMError(
            f"缺少 system prompt 文件 llm/{PROMPT_DIR_NAME}/{filename}：请补齐该文件后重试。"
        ) from exc
    if not text.strip():
        raise LLMError(f"system prompt 文件 llm/{PROMPT_DIR_NAME}/{filename} 为空。")
    return text


def _resource_reader():
    """返回「文件名 → 文本」的读取函数：优先 importlib.resources，回退文件系统。无副作用。"""

    def _read_text(filename: str) -> str:
        """按 importlib.resources 读包内 prompt 文本。副作用：读文件。"""
        return (
            importlib.resources.files("dramaforge.llm")
            .joinpath(PROMPT_DIR_NAME)
            .joinpath(filename)
            .read_text(encoding="utf-8")
        )

    try:
        importlib.resources.files("dramaforge.llm").joinpath(PROMPT_DIR_NAME)
    except (ModuleNotFoundError, TypeError):
        prompt_dir = Path(__file__).resolve().parent / PROMPT_DIR_NAME

        def _read_fs(filename: str) -> str:
            """命名空间包取不到资源时的文件系统回退。副作用：读文件。"""
            return (prompt_dir / filename).read_text(encoding="utf-8")

        return _read_fs
    return _read_text


def clear_prompt_cache() -> None:
    """清空 prompt 缓存（改 prompt 后热加载用）。副作用：清缓存。"""
    _read_prompt.cache_clear()
