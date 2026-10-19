"""职责：提示词模板的唯一实现处（§9.1）：五标记 image_prompt 与「只含运镜 + 单一动作变化」的 motion_prompt。
输入：Shot、角色视觉锚、STYLE_SUFFIX。
输出：PromptPack（模板权威，LLM 只允许贡献 motion_prompt 的动作句）。

副作用：无。纯字符串拼装；不做 IO、不调 LLM。
"""

from __future__ import annotations

import re

from ...contracts.artifacts import Character, PromptPack, Shot
from ...contracts.enums import PROMPT_MARKERS

# 「第三人称」与非运镜的违规痕迹：motion_prompt 里出现即清洗
_THIRD_PERSON = re.compile(r"[他她它]")
_ANCHOR_NOISE = re.compile(r"^[\s，。;；]+")


def build_image_prompt(
    anchors: list[str],
    scene_anchor: str,
    camera: str,
    action: str,
    lighting: str,
    style_suffix: str,
) -> str:
    """按 §9.1 组装五标记 image_prompt（[STYLE] 为附加标记）。无副作用。"""
    char_part = "；".join(a for a in anchors if a) or "无角色空镜"
    return (
        f"[CHAR] {char_part}\n"
        f"[SCENE] {scene_anchor}\n"
        f"[CAMERA] {camera}\n"
        f"[ACTION] {action}\n"
        f"[LIGHT] {lighting}\n"
        f"[STYLE] {style_suffix}"
    )


def build_motion_prompt(camera: str, action: str, llm_motion: str | None = None) -> str:
    """组装 motion_prompt = `{camera}；{action}`；LLM 版本会被清洗（去第三人称/去外貌描述）。

    无副作用。
    """
    base = f"{camera}；{action}"
    if not llm_motion:
        return base
    cleaned = sanitize_motion(llm_motion)
    if not cleaned:
        return base
    # LLM 若只写动作句，补上运镜，保证「运镜 + 单一动作变化」结构
    if camera and camera not in cleaned:
        return f"{camera}；{cleaned}"
    return cleaned


def sanitize_motion(text: str) -> str:
    """清洗 motion_prompt：去掉第三人称代词与外貌/RED 描述残留、压平空白。无副作用。"""
    if not text:
        return ""
    cleaned = _THIRD_PERSON.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _ANCHOR_NOISE.sub("", cleaned)
    return cleaned[:200]


def missing_markers(image_prompt: str) -> list[str]:
    """返回缺失的五标记（prompt_pack_complete 校验器的输入）。无副作用。"""
    return [m for m in PROMPT_MARKERS if m not in (image_prompt or "")]


def build_pack(
    shot: Shot,
    chars: dict[str, Character],
    style_suffix: str,
    resolution: str,
    llm_motion: str | None = None,
    prev_shot_id: str | None = None,
) -> PromptPack:
    """用模板为单个镜头生成 PromptPack（锚文本按集命中换装）。无副作用。"""
    anchors = [chars[c].anchor_for_episode(shot.episode) for c in shot.characters if c in chars]
    image_prompt = build_image_prompt(
        anchors=anchors,
        scene_anchor=shot.scene_anchor,
        camera=shot.camera,
        action=shot.action,
        lighting=shot.lighting,
        style_suffix=style_suffix,
    )
    return PromptPack(
        shot_id=shot.shot_id,
        image_prompt=image_prompt,
        motion_prompt=build_motion_prompt(shot.camera, shot.action, llm_motion),
        tts_text=shot.line,
        duration_sec=shot.duration_sec,
        tier=shot.quality_tier,
        resolution=resolution,  # type: ignore[arg-type]
        prev_shot_id=prev_shot_id,
    )
