"""职责：`--dry-run` 与无 Key 环境下的确定性内容生成器（FakeLLMClient），产出**能通过闸门**的剧本/分镜/提示词/标签。
输入：agents 传入的 system prompt 与 user prompt（其中含 ```json 结构化输入块）。
输出：与真实 LLM 同形状的结构化对象（Outline / Shot / PromptPack / SegmentNote / ConstraintSpecDraft / PayoffTagItem / 剧本 md）。

副作用：无网络、无磁盘、无随机数（同一输入必得同一输出，保证可复现）。
约束：类名必须带 Fake 前缀（§0.3）；只允许在 dry-run / 无 Key 路径实例化。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..contracts.artifacts import (
    Block,
    CharacterDraft,
    Claim,
    ConstraintSpecDraft,
    EpisodeOutline,
    Outline,
    PayoffTagItem,
    PromptPack,
    SegmentNote,
    Shot,
)
from ..contracts.enums import CAMERAS, SLOT_KEYS, PAYOFF_TAGS
from ..contracts.ids import character_id as make_character_id
from ..core.config import Settings, get_settings

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# ── 固定的角色/台词池（确定性）──────────────────────────────
CHARACTERS: tuple[dict[str, str], ...] = (
    {
        "display_name": "林川",
        "anchor_text": "二十八岁男，短寸头，深灰外卖骑手制服，左腕一道旧疤，神情疲惫但眼神倔强",
        "role": "protagonist",
        "catchphrase": "这单我必须送完。",
        "vocal_habit": "句末常带「必须」",
    },
    {
        "display_name": "王姐",
        "anchor_text": "四十五岁女，低盘发，藏蓝社区制服，左臂红袖章，站姿笔直语气强硬",
        "role": "antagonist",
        "catchphrase": "别拿规矩压我。",
        "vocal_habit": "爱用反问句",
    },
)

LINES: tuple[str, ...] = (
    "这单我必须送完。",
    "系统超时算谁的？",
    "别拿规矩压我。",
    "楼道灯又坏了。",
    "你先看订单。",
    "雨停了再说。",
)

ACTIONS: tuple[str, ...] = (
    "雨水砸在铁皮雨棚上",
    "手机屏幕忽然亮起",
    "楼道声控灯闪了两下",
    "电动车后轮陷进积水",
    "纸箱边缘被雨水泡软",
    "铁门在风里来回撞",
    "远处警灯扫过墙面",
    "塑料袋里露出半张单子",
)

LIGHTING: tuple[str, ...] = ("冷调路灯", "楼道惨白灯", "雨夜霓虹反光", "手机屏幕补光")
LOCATIONS: tuple[tuple[str, str], ...] = (
    ("城中村天台", "夜"),
    ("老小区楼道", "深夜"),
    ("街边便利店门口", "雨夜"),
)

# 参照分布的爽点标签桶（固定，供 payoff_density 对照）
REF_TAGS: tuple[str, ...] = ("打脸", "逆袭", "悬念钩", "护短")


def _ctx_from_prompt(user: str) -> dict[str, Any]:
    """从 user prompt 里抽取 ```json 结构化输入块（多个时后者覆盖前者）。无副作用。"""
    merged: dict[str, Any] = {}
    for raw in _JSON_BLOCK.findall(user or ""):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            merged.update(obj)
    return merged


def _clamp(value: int, low: int, high: int) -> int:
    """整数夹取。无副作用。"""
    return max(low, min(high, value))


def _char_pairs(ctx: dict[str, Any]) -> list[tuple[str, str]]:
    """从输入里取出 [(character_id, anchor_text)]，兼容 Character / CharacterDraft 两种形状。无副作用。"""
    out: list[tuple[str, str]] = []
    for c in ctx.get("characters") or []:
        if not isinstance(c, dict):
            continue
        va = c.get("visual_anchor") or {}
        cid = str(c.get("character_id") or va.get("character_id") or "")
        anchor = str(c.get("anchor_text") or va.get("anchor_text") or "")
        if cid or anchor:
            out.append((cid, anchor))
    return out


class FakeLLMClient:
    """dry-run 用的确定性 LLM 替身：按 key 分派到本地生成器。"""

    name = "fake-llm"

    def __init__(self, settings: Settings | None = None, role: str = "script") -> None:
        self.settings = settings or get_settings()
        self.role = role
        self.calls: list[tuple[str, str | None]] = []

    # ── 对外接口（与 LLMClient 同形）────────────────────────
    @property
    def configured(self) -> bool:
        """Fake 永远可用。无副作用。"""
        return True

    async def agenerate_object(self, system: str, user: str, model_cls, key: str | None = None) -> Any:
        """按 key 生成结构化对象。副作用：内存记录调用。"""
        self.calls.append((self.role, key))
        ctx = _ctx_from_prompt(user)
        name = key or self.role
        handler = {
            "reference": self._notes,
            "constraint": self._constraints,
            "script.outline": self._outline,
            "script.characters": self._characters,
            "script.episode": self._episode_payload,
            "storyboard": self._shots,
            "prompt_pack": self._packs,
            "review": self._review_hints,
            "tagger": self._tags,
        }.get(name)
        if handler is None:
            return _coerce(model_cls, [] if _is_list(model_cls) else {})
        return _coerce(model_cls, handler(ctx))

    async def agenerate_text(self, system: str, user: str) -> str:
        """生成剧本 md 文本（episodes 阶段的文本形态）。副作用：内存记录调用。"""
        self.calls.append((self.role, "text"))
        ctx = _ctx_from_prompt(user)
        return self._episode_md(ctx)

    # ── 各阶段生成器 ────────────────────────────────────────
    def _notes(self, ctx: dict[str, Any]) -> list[dict]:
        """五个段位各一条归纳（含证据 id）。无副作用。"""
        cards = ctx.get("cards") or []
        by_slot: dict[str, list[str]] = {}
        for c in cards:
            by_slot.setdefault(str(c.get("slot_key")), []).append(str(c.get("card_id")))
        texts = {
            "premise": "题材前提集中在「底层职业 + 强不公 + 即时反击」，主角动机必须在首集 60 秒内交代清楚。",
            "structure": "结构上采用冷开场 + 三分钟一次小反转；每集结尾留一个未解决动作或悬念台词。",
            "emotion": "情绪曲线先抑后扬：压抑 40 秒、反转 20 秒、留钩 15 秒，避免连续两镜同强度。",
            "platform_voice": "平台语感偏短句与口语，单句不超过 18 字，多用反问与命令句制造压迫感。",
            "risk": "与《开局送外卖》相似的爆款结构集中在开局强冲突，需披露相似点并规避真实品牌贬损。",
        }
        out: list[dict] = []
        for slot in SLOT_KEYS:
            out.append(
                {
                    "slot_key": slot,
                    "text": texts[slot],
                    "basis_ids": by_slot.get(slot, [])[:3],
                }
            )
        return out

    def _constraints(self, ctx: dict[str, Any]) -> list[dict]:
        """LLM 侧约束草稿（只补平台语感与情绪两类，其余来自 Python 基线）。无副作用。"""
        cards = ctx.get("cards") or []
        refs = [str(c.get("card_id")) for c in cards][:4] or ["config:platform_voice"]
        return [
            {
                "slot_key": "platform_voice",
                "source_refs": refs,
                "kind": "numeric",
                "hard": True,
                "value": 18,
                "unit": "char",
                "validator_key": "line_length_max",
                "judges_self_report": False,
                "fail_action": "reject_and_regenerate",
            },
            {
                "slot_key": "emotion",
                "source_refs": refs,
                "kind": "numeric",
                "hard": False,
                "value": 0.55,
                "unit": "l1",
                "validator_key": "payoff_density",
                "judges_self_report": True,
                "fail_action": "warn_and_continue",
            },
            {
                "slot_key": "risk",
                "source_refs": refs,
                "kind": "enum",
                "hard": True,
                "value": ["resolved"],
                "validator_key": "episode_ending_unresolved",
                "judges_self_report": True,
                "fail_action": "reject_and_regenerate",
            },
        ]

    def _outline(self, ctx: dict[str, Any]) -> dict:
        """按 episode_count 生成大纲，钩子逐集升级。无副作用。"""
        n = int(ctx.get("episode_count") or self.settings.topic_default_episodes)
        n = max(1, n)
        topic = str(ctx.get("topic") or "外卖员逆袭")
        episodes = []
        for i in range(1, n + 1):
            episodes.append(
                {
                    "episode": i,
                    "title": f"第{i}单：{('超时' if i % 2 else '反杀')}",
                    "goal": f"第{i}集完成一次小目标并甩出更大悬念：{topic}",
                    "beats": [
                        "冷开场：一个具体损失数字",
                        "施压：规则方下最后通牒",
                        "反击：主角亮出一个证据",
                        "留钩：新的代价出现",
                    ],
                    "hook": "未解决动作 + 一句反问",
                }
            )
        return {
            "project": str(ctx.get("project") or "dry_run"),
            "topic": topic,
            "logline": f"{topic}：一个被规则压住的人，用一条订单掀翻整条利益链。",
            "episodes": episodes,
        }

    def _characters(self, ctx: dict[str, Any]) -> list[dict]:
        """返回两个固定角色（锚文本 ≥20 字、口头禅唯一）。无副作用。"""
        return [dict(c) for c in CHARACTERS]

    def _episode_payload(self, ctx: dict[str, Any]) -> dict:
        """单集结构（供 md 渲染）：返回 {"episode","title","scenes"}。无副作用。"""
        return self._episode_structure(ctx)

    def _episode_md(self, ctx: dict[str, Any]) -> str:
        """渲染单集 markdown（与 blocks 解析器严格对齐）。无副作用。"""
        from ..agents.script.format import render_episode

        struct = self._episode_structure(ctx)
        scenes = [
            {
                "scene": sc["scene"],
                "location": sc["location"],
                "time_of_day": sc["tod"],
                "anchor": sc["anchor"],
                "characters": sc["characters"],
                "beats": sc["beats"],
            }
            for sc in struct["scenes"]
        ]
        return render_episode(struct["episode"], struct["title"], scenes)

    def _episode_structure(self, ctx: dict[str, Any]) -> dict:
        """生成固定的三场九拍结构（保证 blocks 数落在镜数预算内）。无副作用。"""
        ep = int(ctx.get("episode") or 1)
        chars = ctx.get("characters") or []
        pairs = _char_pairs(ctx)
        ids = [p[0] for p in pairs if p[0]] or [
            make_character_id(c["display_name"]) for c in CHARACTERS
        ]
        a, b = ids[0], ids[-1]
        beats: list[tuple[str, str, str]] = [
            ("动作", "", f"雨水砸在铁皮雨棚上，{a}低头看屏幕。"),
            ("台词", a, LINES[0]),
            ("动作", "", "楼道声控灯闪了两下又灭。"),
            ("台词", b, LINES[2]),
            ("动作", "", "纸箱边缘被雨水泡软，单子露出半张。"),
            ("台词", a, LINES[4]),
            ("动作", "", "远处警灯扫过墙面。"),
            ("台词", b, LINES[1]),
            ("动作", "", "铁门在风里来回撞，镜头停在门缝。"),
        ]
        scenes = []
        for i in range(3):
            loc, tod = LOCATIONS[i]
            scenes.append(
                {
                    "scene": i + 1,
                    "location": loc,
                    "tod": tod,
                    "anchor": f"{loc}，{tod}，潮湿反光",
                    "characters": [a, b],
                    "beats": beats[i * 3 : (i + 1) * 3],
                }
            )
        title = f"第{ep}单：超时"
        return {"episode": ep, "title": title, "scenes": scenes}

    def _shots(self, ctx: dict[str, Any]) -> list[dict]:
        """由 blocks 生成镜头：时长落档位、每集总时长落区间、末镜留钩。无副作用。"""
        ep = int(ctx.get("episode") or 1)
        blocks: list[Block] = [Block.model_validate(b) for b in (ctx.get("blocks") or [])]
        pairs = _char_pairs(ctx)
        ids = [p[0] for p in pairs if p[0]] or [
            make_character_id(c["display_name"]) for c in CHARACTERS
        ]
        ladder = tuple(self.settings.shot_ladder)
        low, high = self.settings.episode_duration_range
        min_shots, max_shots = self.settings.min_shot_per_episode, self.settings.max_shot_per_episode
        target = int(self.settings.episode_duration_target_sec)

        n = _clamp(len(blocks) or min_shots, min_shots, max_shots)
        durations = self._durations(n, target, low, high, ladder)
        if not blocks:
            blocks = [
                Block(block_id=f"b{ep:02d}{i + 1:03d}", episode=ep, scene=1, seq=i + 1,
                      summary="空镜", text="空镜", characters=[])
                for i in range(n)
            ]
        shots: list[dict] = []
        for i in range(n):
            block = blocks[i % len(blocks)]
            cid = ids[i % len(ids)] if ids else ""
            is_last = i == n - 1
            has_line = i % 2 == 1
            shots.append(
                {
                    "shot_id": "",
                    "source_block_ids": [block.block_id],
                    "episode": ep,
                    "scene": block.scene,
                    "duration_sec": durations[i],
                    "camera": CAMERAS[i % len(CAMERAS)],
                    "action": ACTIONS[i % len(ACTIONS)],
                    "line": (LINES[i % len(LINES)] if has_line else None),
                    "dialogue_speaker": (cid if has_line else None),
                    "closing_state": ("unresolved_action" if is_last else "resolved"),
                    "characters": [cid] if cid else [],
                    "is_turn": (ep == 1 and i == 2),
                    "quality_tier": "fast",
                    "lighting": LIGHTING[i % len(LIGHTING)],
                    "scene_anchor": f"{LOCATIONS[block.scene % len(LOCATIONS)][0]}，{LOCATIONS[block.scene % len(LOCATIONS)][1]}",
                }
            )
        return shots

    def _durations(self, n: int, target: int, low: int, high: int, ladder: tuple[float, ...]) -> list[float]:
        """在档位内凑出落在 [low,high] 的每集总时长（确定性）。无副作用。"""
        short, long_ = ladder[0], ladder[-1]
        if len(ladder) == 1:
            return [short] * n
        step = long_ - short
        k = _clamp(int(round((target - n * short) / step)), 0, n) if step else 0
        while k < n and k * long_ + (n - k) * short < low:
            k += 1
        while k > 0 and k * long_ + (n - k) * short > high:
            k -= 1
        return [long_] * k + [short] * (n - k)

    def _packs(self, ctx: dict[str, Any]) -> list[dict]:
        """按 §9.1 模板生成提示词包（image_prompt 含五标记，字符锚为子串）。无副作用。"""
        shots = [Shot.model_validate(s) for s in (ctx.get("shots") or [])]
        anchors = dict(_char_pairs(ctx))
        style = self.settings.style_suffix
        packs: list[dict] = []
        for i, shot in enumerate(shots):
            char_part = "；".join(a for c, a in anchors.items() if c in shot.characters and a) or "无角色空镜"
            packs.append(
                {
                    "shot_id": shot.shot_id,
                    "image_prompt": (
                        f"[CHAR] {char_part}\n[SCENE] {shot.scene_anchor}\n[CAMERA] {shot.camera}\n"
                        f"[ACTION] {shot.action}\n[LIGHT] {shot.lighting}\n[STYLE] {style}"
                    ),
                    "motion_prompt": f"{shot.camera}；{shot.action}",
                    "tts_text": shot.line,
                    "duration_sec": shot.duration_sec,
                    "tier": shot.quality_tier,
                    "resolution": self.settings.video_resolution,
                    "prev_shot_id": shots[i - 1].shot_id if i else None,
                }
            )
        return packs

    def _review_hints(self, ctx: dict[str, Any]) -> list[str]:
        """人工提示语（不参与判定）。无副作用。"""
        return [
            "第 1 集反转在第 3 镜出现，注意观众是否跟得上人物关系。",
            "雨夜场景连续 5 镜偏暗，检查首帧亮度是否够。",
            "王姐的台词偏命令句，注意别让观众反感过头。",
        ]

    def _tags(self, ctx: dict[str, Any]) -> list[dict]:
        """给每镜打爽点标签（封闭枚举，固定桶）。无副作用。"""
        shots = ctx.get("shots") or []
        out: list[dict] = []
        for i, s in enumerate(shots):
            tag = REF_TAGS[i % len(REF_TAGS)]
            out.append({"shot_id": str(s.get("shot_id")), "tags": [tag] if tag in PAYOFF_TAGS else []})
        return out


def _is_list(model_cls) -> bool:
    """判断目标类型是否为 list[...]。无副作用。"""
    return getattr(model_cls, "__origin__", None) is list or str(model_cls).startswith("list[")


def _coerce(model_cls, payload: Any):
    """把 payload 装进 model_cls（列表或单对象）。无副作用。"""
    import pydantic

    if _is_list(model_cls):
        inner = model_cls.__args__[0]
        return [inner.model_validate(x) if isinstance(x, dict) else x for x in (payload or [])]
    if isinstance(model_cls, type) and issubclass(model_cls, pydantic.BaseModel):
        return model_cls.model_validate(payload or {})
    return payload


def fake_cards() -> list[dict]:
    """dry-run 用的参照卡夹具（12 张，覆盖五个段位，含爽点标签分布）。无副作用。"""
    rows: list[dict] = []
    specs = [
        ("premise", "底层职业开局", "以具体职业动作开场，30 秒内交代主角处境", None, None),
        ("premise", "损失数字先行", "开场直接给一个可量化损失", 1.0, "个/集"),
        ("structure", "三分钟一小反转", "每 180 秒内必须有一次信息反转", 180.0, "秒"),
        ("structure", "集末未解决动作", "每集最后一镜留动作或台词钩子", 1.0, "次/集"),
        ("structure", "首集反转位", "第一次亮底牌不晚于第 2 集", 2.0, "集"),
        ("emotion", "先抑后扬曲线", "压抑段与反击段时长比约 2:1", 2.0, "倍"),
        ("emotion", "爽点密度", "每集爽点标签 3-5 个", 4.0, "个/集"),
        ("platform_voice", "短句口语", "单句台词不超过 18 字", 18.0, "字"),
        ("platform_voice", "反问压迫感", "反派每集至少 1 句反问", 1.0, "句/集"),
        ("risk", "相似爆款披露", "对标爆款需显式披露相似点与差异点", None, None),
        ("risk", "违禁词零命中", "不得出现血腥、色情、赌博教程等表述", 0.0, "次"),
        ("risk", "真实品牌规避", "不出现真实品牌名与贬损表述", 0.0, "次"),
    ]
    for i, (slot, title, assertion, num, unit) in enumerate(specs, start=1):
        rows.append(
            {
                "card_id": f"ref_{i:03d}",
                "slot_key": slot,
                "title": title,
                "platform": "douyin",
                "assertion": assertion,
                "numeric_value": num,
                "numeric_unit": unit,
                "payload": {"tags": list(REF_TAGS) if slot == "emotion" else []},
                "annotator": "dry-run",
                "source_url": f"https://example.local/ref/{i}",
                "annotated_at": "2025-01-01",
            }
        )
    return rows


def fake_tag_distribution() -> dict[str, float]:
    """dry-run 的参照爽点分布（与 REF_TAGS 对应）。无副作用。"""
    return {"打脸": 0.3, "逆袭": 0.3, "悬念钩": 0.25, "护短": 0.15}
