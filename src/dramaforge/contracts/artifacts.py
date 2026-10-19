"""职责：全部跨模块数据契约（Pydantic 模型）的唯一定义处，对应规格 §5.2 / §8 辅助模型。
输入：来自 LLM 的结构化对象、磁盘 JSON、数据库行、HTTP body。
输出：校验后的模型实例；字段名与 §5 完全一致，不得自行增删。

副作用：无。本模块只做数据形状与轻校验，不做 IO、不判业务规则。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .enums import (
    AssetKind,
    Camera,
    ClosingState,
    ConstraintKind,
    FailAction,
    PayoffTag,
    Resolution,
    ResolveAction,
    SlotKey,
    Tier,
)


class Claim(BaseModel):
    """一条可核对断言。basis_ids 指向参照卡 card_id 或剧本 block_id。"""

    statement: str
    confidence: float = Field(ge=0, le=1)
    basis_ids: list[str] = []


class SegmentChapter(BaseModel):
    """每个 slot 一份章节，五份必须齐全；无证据时用 gaps 显式声明缺失。"""

    task_id: str
    slot_key: SlotKey
    role: Literal["drama"] = "drama"
    summary: str = Field(min_length=40, max_length=400)
    claims: list[Claim] = []
    gaps: list[str] = []
    spec_version: str = ""


class ConstraintSpec(BaseModel):
    """由参照卡推导的结构化约束；source_refs 为空者在落库前被丢弃。"""

    constraint_id: str
    task_id: str
    slot_key: SlotKey
    source_refs: list[str] = []
    kind: ConstraintKind
    hard: bool = False
    value: Any = None
    tolerance: float | None = None
    unit: str | None = None
    validator_key: str
    judges_self_report: bool = False
    cost_impact: bool = False
    fail_action: FailAction = "warn_and_continue"
    spec_version: str = ""


class ConstraintSpecDraft(BaseModel):
    """constraint role 的产出形状：ConstraintSpec 去掉 task_id / spec_version / cost_impact。"""

    constraint_id: str | None = None
    slot_key: SlotKey
    source_refs: list[str] = []
    kind: ConstraintKind
    hard: bool = False
    value: Any = None
    tolerance: float | None = None
    unit: str | None = None
    validator_key: str
    judges_self_report: bool = False
    fail_action: FailAction = "warn_and_continue"


class SegmentNote(BaseModel):
    """reference role 的产出：只归纳，不产约束。"""

    slot_key: SlotKey
    text: str
    basis_ids: list[str] = []


class VisualAnchor(BaseModel):
    """角色视觉锚：每镜 image_prompt 必须包含 anchor_text（character_consistency）。"""

    character_id: str
    display_name: str
    anchor_text: str = Field(min_length=20, max_length=120)
    ref_image_path: str | None = None
    wardrobe_change: list[dict] = []


class Character(BaseModel):
    """characters.json 的一行：视觉锚 + 语感指纹（voice_fingerprint_unique 依赖 catchphrase）。"""

    visual_anchor: VisualAnchor
    role: str = "supporting"
    catchphrase: str = ""
    vocal_habit: str = ""
    first_ep: int = 1

    @property
    def character_id(self) -> str:
        """角色 id 透传。无副作用。"""
        return self.visual_anchor.character_id

    @property
    def display_name(self) -> str:
        """角色显示名透传。无副作用。"""
        return self.visual_anchor.display_name

    @property
    def anchor_text(self) -> str:
        """主视觉锚文本透传。无副作用。"""
        return self.visual_anchor.anchor_text

    def anchor_for_episode(self, episode: int) -> str:
        """按 ep_start 命中替换锚，否则用主锚（wardrobe_continuity）。无副作用。"""
        best: tuple[int, str] | None = None
        for item in self.visual_anchor.wardrobe_change:
            try:
                ep_start = int(item.get("ep_start", 0))
            except (TypeError, ValueError):
                continue
            text = str(item.get("anchor_text") or "")
            if not text or ep_start > episode:
                continue
            if best is None or ep_start >= best[0]:
                best = (ep_start, text)
        return best[1] if best else self.visual_anchor.anchor_text


class CharacterDraft(BaseModel):
    """script role 产出的角色草稿；character_id 由 Python 按显示名归一化后补齐。"""

    display_name: str
    anchor_text: str = Field(min_length=20, max_length=120)
    role: str = "supporting"
    catchphrase: str = ""
    vocal_habit: str = ""
    wardrobe_change: list[dict] = []
    first_ep: int = 1


class Block(BaseModel):
    """剧本最小单元；shot.source_block_ids 引用它做证据链。"""

    block_id: str
    episode: int
    scene: int
    seq: int
    summary: str
    text: str
    characters: list[str] = []


class Scene(BaseModel):
    """场：storyboard 的空间/时间锚与镜头容器。"""

    scene_id: str
    episode: int
    scene: int
    location: str
    time_of_day: str
    scene_anchor: str
    characters: list[str] = []
    block_ids: list[str] = []


class EpisodeOutline(BaseModel):
    """单集大纲：goal 必须可兑现为画面。"""

    episode: int
    title: str
    goal: str
    beats: list[str] = []
    hook: str = ""


class Outline(BaseModel):
    """总大纲：episodes 数量须落在 episode_count_range 内。"""

    project: str
    topic: str
    logline: str
    episodes: list[EpisodeOutline] = []


class Shot(BaseModel):
    """一个镜头；duration_sec 必须落在 SHOT_DURATION_LADDER，quality_tier 由 §9.2 规则赋档。"""

    shot_id: str
    source_block_ids: list[str] = []
    episode: int
    scene: int
    duration_sec: float
    camera: Camera
    action: str
    line: str | None = None
    dialogue_speaker: str | None = None
    closing_state: ClosingState
    characters: list[str] = []
    is_turn: bool = False
    quality_tier: Tier
    lighting: str
    scene_anchor: str


class PromptPack(BaseModel):
    """喂给图像/视频/TTS 的提示词包；image_prompt 必须含五个标记。"""

    shot_id: str
    image_prompt: str
    motion_prompt: str
    tts_text: str | None = None
    duration_sec: float
    tier: Tier
    resolution: Resolution = "768p"
    prev_shot_id: str | None = None


class Violation(BaseModel):
    """校验器产出的违规项；gate 汇总后决定返工或挂人工。"""

    constraint_id: str
    validator_key: str
    slot_key: SlotKey
    expected: Any = None
    actual: Any = None
    shot_ids: list[str] = []
    severity: Literal["hard", "warn"] = "warn"
    needs_human: bool = False


class ConstraintVerdict(BaseModel):
    """constraint_report.json 的一行：每条约束的机读判定。"""

    constraint_id: str
    validator_key: str
    slot_key: SlotKey
    verdict: Literal["PASS", "FAIL", "NEEDS_HUMAN"] = "PASS"
    expected: Any = None
    actual: Any = None
    shot_ids: list[str] = []
    severity: Literal["hard", "warn"] = "warn"
    structural_only: bool = False


class ConstraintReport(BaseModel):
    """机读约束核对表：由 Python 生成，不经任何 LLM（§8）。"""

    task_id: str
    revision: int
    spec_version: str = ""
    verdicts: list[ConstraintVerdict] = []
    passed: bool = False
    hard_failures: int = 0
    needs_human: int = 0
    generated_at: str = ""


class AssetQuoteItem(BaseModel):
    """报价明细的一行：est_cost = unit_price × duration × expected_attempts（§10.1）。"""

    shot_id: str
    kind: AssetKind
    tier: Tier
    model: str
    resolution: Resolution
    duration_sec: float
    unit_price_cny: float
    expected_attempts: float = 1.0
    est_cost_cny: float = 0.0


class CostQuote(BaseModel):
    """review/cost_quote.json：报价闸的唯一凭据，未 approve 不得提交任何 provider 请求。"""

    task_id: str
    revision: int
    items: list[AssetQuoteItem] = []
    est_total_cny: float = 0.0
    image_total_cny: float = 0.0
    video_total_cny: float = 0.0
    per_episode_cny: dict[str, float] = {}
    cap_cny: float = 0.0
    within_cap: bool = True
    approved: bool = False
    spec_version: str = ""
    created_at: str = ""


class AssetShotReport(BaseModel):
    """asset_report.json 的 per_shot 行。"""

    shot_id: str
    kind: AssetKind
    revision: int = 0
    status: str = ""
    attempt: int = 0
    cost_cny: float = 0.0
    tier: Tier | None = None
    file_path: str | None = None


class AssetReport(BaseModel):
    """final/asset_report.json：总花费、逐镜明细、放弃清单与 attempt 直方图。"""

    task_id: str
    total_spent_cny: float = 0.0
    per_shot: list[AssetShotReport] = []
    rejected: list[AssetShotReport] = []
    abandoned: list[str] = []
    attempt_hist: dict[str, int] = {}
    pass_rate: float = 0.0
    generated_at: str = ""


class ResolutionRecord(BaseModel):
    """resolution 表的一行：人工决议留痕，target_id 可指 card_id:slot_key。"""

    task_id: str
    target_type: str
    target_id: str
    action: ResolveAction
    operator: str = "human"
    comment: str = ""


class TaskSpec(BaseModel):
    """POST /api/drama/tasks 的落库形状。"""

    task_id: str
    topic: str
    platform: str = "douyin"
    episode_count: int = 8
    episode_duration_target: int = 75
    strict_mode: bool = False
    cost_cap_cny: float = 500.0
    params: dict[str, Any] = {}
    status: str = "PENDING"


class TaskRow(BaseModel):
    """task 表读取形状。"""

    task_id: str
    topic: str
    platform: str | None = None
    episode_count: int | None = None
    episode_duration_target: int | None = None
    strict_mode: bool = False
    status: str = "PENDING"
    cost_cap_cny: float | None = None
    params: dict[str, Any] = {}


class StageRow(BaseModel):
    """stage_state 表的读取形状（带 revision 维度）。"""

    task_id: str
    slot_key: str
    stage: str
    revision: int = 0
    status: str = "PENDING"
    fail_reason: str | None = None
    output_path: str | None = None


class AssetRow(BaseModel):
    """asset_state 表的读取形状。"""

    task_id: str
    shot_id: str
    kind: AssetKind
    revision: int = 0
    status: str = "PENDING"
    attempt: int = 0
    episode: int = 0
    tier: Tier | None = None
    model: str | None = None
    resolution: str | None = None
    provider: str | None = None
    provider_task_id: str | None = None
    cost_cny: float = 0.0
    file_path: str | None = None
    est_cost_cny: float | None = None
    reject_reason: str | None = None


class PayoffTagItem(BaseModel):
    """tagger role 的产出：一条镜头文本命中的爽点标签。"""

    shot_id: str
    tags: list[PayoffTag] = []


class PendingHumanItem(BaseModel):
    """pending_human.json 的一行：前端 Review 页的列表项。"""

    kind: Literal["quote", "gate", "asset", "budget", "compliance"]
    target_id: str
    task_id: str
    title: str
    detail: str = ""
    actions: list[str] = []
    slot_key: str | None = None
    # AMBIGUITY(§5.2 未列)：前端 Review 页要展示报价明细，此处携带 `{target_ids, items, est_total_cny}`
    # 供 `POST /resolve` 直接回传；只带 id 与金额，不含正文（§12 事件载荷限制不适用于 HTTP 响应）。
    payload: dict[str, Any] = {}
    created_at: str = ""


class SystemEvent(BaseModel):
    """事件总线唯一载荷：只带 id/路径/计数，禁止携带正文或大字段（§12）。"""

    seq: int = 0
    type: str
    task_id: str
    stage: str | None = None
    slot_key: str | None = None
    revision: int | None = None
    path: str | None = None
    data: dict[str, Any] = {}
    created_at: str = ""
