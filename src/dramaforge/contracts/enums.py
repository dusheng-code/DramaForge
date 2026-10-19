"""职责：受控枚举与封闭常量清单的唯一声明处，任何模块不得就地另造字面量（§5.1）。
输入：无（模块级常量与类型别名）。
输出：Literal/Enum 类型与常量元组，供 contracts、core、validators、api 共同引用。

副作用：无。本模块只做声明，禁止在此放业务逻辑。
"""

from __future__ import annotations

from typing import Literal

# ── 段位 slot（五个受控业务面，顺序即展示顺序）────────────────
SlotKey = Literal["premise", "structure", "emotion", "platform_voice", "risk"]
SLOT_KEYS: tuple[SlotKey, ...] = ("premise", "structure", "emotion", "platform_voice", "risk")

SLOT_LABELS: dict[str, str] = {
    "premise": "立意/前提",
    "structure": "结构/节奏",
    "emotion": "情绪曲线",
    "platform_voice": "平台语感",
    "risk": "合规风险",
}

# ── 阶段（14 个，见 §6.1 依赖表）─────────────────────────────
Stage = Literal[
    "reference",
    "constraint",
    "outline",
    "episodes",
    "characters",
    "blocks",
    "storyboard",
    "prompt_pack",
    "gate",
    "quote",
    "anchor_image",
    "keyframe",
    "video",
    "assemble",
]
STAGES: tuple[Stage, ...] = (
    "reference",
    "constraint",
    "outline",
    "episodes",
    "characters",
    "blocks",
    "storyboard",
    "prompt_pack",
    "gate",
    "quote",
    "anchor_image",
    "keyframe",
    "video",
    "assemble",
)

# 逐 slot 展开的阶段（其余用 slot_key='*'）
SLOT_SCOPED_STAGES: tuple[Stage, ...] = ("gate",)

GLOBAL_SLOT = "*"

StageStatus = Literal["PENDING", "RUNNING", "PASSED", "FAILED", "NEEDS_HUMAN", "STALE"]
STAGE_STATUSES: tuple[StageStatus, ...] = (
    "PENDING",
    "RUNNING",
    "PASSED",
    "FAILED",
    "NEEDS_HUMAN",
    "STALE",
)

# §6.2 允许的 stage 迁移
# AMBIGUITY(§3.1 vs §6.2)：产物丢失需“标 STALE 重跑”，但 §6.2 只列 STALE→PENDING。
# 此处补 PASSED→STALE（仅 recovery 在 output_path 校验失败时写入）。
STAGE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PENDING": ("RUNNING", "NEEDS_HUMAN"),
    "RUNNING": ("PASSED", "FAILED", "NEEDS_HUMAN", "PENDING"),
    "NEEDS_HUMAN": ("PENDING",),
    "STALE": ("PENDING",),
    "FAILED": ("PENDING",),
    "PASSED": ("STALE",),
}

# ── 资产 ────────────────────────────────────────────────────
AssetKind = Literal["anchor_image", "keyframe", "video"]
ASSET_KINDS: tuple[AssetKind, ...] = ("anchor_image", "keyframe", "video")

AssetStatus = Literal[
    "PENDING",
    "SUBMITTED",
    "GENERATING",
    "REVIEW",
    "PASSED",
    "REJECTED",
    "ORPHANED",
    "BUDGET_HOLD",
]
ASSET_STATUSES: tuple[AssetStatus, ...] = (
    "PENDING",
    "SUBMITTED",
    "GENERATING",
    "REVIEW",
    "PASSED",
    "REJECTED",
    "ORPHANED",
    "BUDGET_HOLD",
)

# 计入 reserved 的在途状态（§10.2）
ASSET_RESERVED_STATUSES: tuple[AssetStatus, ...] = ("PENDING", "SUBMITTED", "GENERATING", "REVIEW")

# §6.2 允许的 asset 迁移
# AMBIGUITY(§6.2 vs §10.3)：§10.3 的 step() 需要“图筛闸未过 → release”，而 §6.2 未列
# SUBMITTED→PENDING。此处补该边并在 claim 层用 NOT EXISTS 前置图筛闸，release 仅作兜底。
ASSET_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PENDING": ("SUBMITTED", "BUDGET_HOLD", "ORPHANED", "REJECTED"),
    "SUBMITTED": ("GENERATING", "REVIEW", "REJECTED", "ORPHANED", "PENDING"),
    "GENERATING": ("REVIEW", "PENDING", "REJECTED", "ORPHANED"),
    "REVIEW": ("PASSED", "REJECTED", "ORPHANED", "BUDGET_HOLD"),
    "PASSED": ("ORPHANED",),
    "REJECTED": ("PENDING", "BUDGET_HOLD", "ORPHANED"),
    "BUDGET_HOLD": ("PENDING", "REJECTED"),
    "ORPHANED": (),
}

# ── 制作档位与镜头语言 ───────────────────────────────────────
Tier = Literal["fast", "hero"]
TIERS: tuple[Tier, ...] = ("fast", "hero")

ClosingState = Literal["resolved", "cliffhanger_line", "unresolved_action", "reveal_tease"]
CLOSING_STATES: tuple[ClosingState, ...] = (
    "resolved",
    "cliffhanger_line",
    "unresolved_action",
    "reveal_tease",
)

Camera = Literal["固定", "推", "拉", "摇", "移", "跟", "手持"]
CAMERAS: tuple[Camera, ...] = ("固定", "推", "拉", "摇", "移", "跟", "手持")

FailAction = Literal["reject_and_regenerate", "warn_and_continue", "needs_human"]
FAIL_ACTIONS: tuple[FailAction, ...] = (
    "reject_and_regenerate",
    "warn_and_continue",
    "needs_human",
)

Resolution = Literal["768p", "1080p"]
RESOLUTIONS: tuple[Resolution, ...] = ("768p", "1080p")

ConstraintKind = Literal["numeric", "enum", "forbidden", "count", "structural"]
CONSTRAINT_KINDS: tuple[ConstraintKind, ...] = (
    "numeric",
    "enum",
    "forbidden",
    "count",
    "structural",
)

Severity = Literal["hard", "warn"]
Verdict = Literal["PASS", "FAIL", "NEEDS_HUMAN"]

# ── 爽点标签封闭枚举（§7 payoff_density）───────────────────
PayoffTag = Literal[
    "反杀",
    "打脸",
    "身份揭穿",
    "逆袭",
    "护短",
    "亮底牌",
    "误会解除",
    "复仇推进",
    "喜剧缓和",
    "悬念钩",
]
PAYOFF_TAGS: tuple[PayoffTag, ...] = (
    "反杀",
    "打脸",
    "身份揭穿",
    "逆袭",
    "护短",
    "亮底牌",
    "误会解除",
    "复仇推进",
    "喜剧缓和",
    "悬念钩",
)

# ── 任务级派生状态（§5.3 task.status 未给封闭集合，此处收口）──
TaskStatus = Literal[
    "PENDING",
    "RUNNING",
    "NEEDS_HUMAN",
    "AWAITING_HUMAN",
    "BUDGET_HOLD",
    "DONE",
    "FAILED",
]
TASK_TERMINAL_STATUSES: tuple[TaskStatus, ...] = ("DONE", "FAILED")

# ── 人工决议动作（§12）────────────────────────────────────
ResolveAction = Literal[
    "approve_quote",
    "reject_quote",
    "extend_budget",
    "pass_slot",
    "retry_shot",
    "skip_shot",
    "veto_keyframe",
    "approve_keyframe",
]
RESOLVE_ACTIONS: tuple[ResolveAction, ...] = (
    "approve_quote",
    "reject_quote",
    "extend_budget",
    "pass_slot",
    "retry_shot",
    "skip_shot",
    "veto_keyframe",
    "approve_keyframe",
)

# ── 事件类型（§12，仅推送）─────────────────────────────────
EventType = Literal[
    "STAGE_UPDATED",
    "CONSTRAINT_READY",
    "PROGRESS_MESSAGE",
    "COST_QUOTE_READY",
    "ASSET_REVIEW_REQUIRED",
    "NEEDS_HUMAN",
    "BUDGET_EXCEEDED",
    "TASK_FINISHED",
]
EVENT_TYPES: tuple[EventType, ...] = (
    "STAGE_UPDATED",
    "CONSTRAINT_READY",
    "PROGRESS_MESSAGE",
    "COST_QUOTE_READY",
    "ASSET_REVIEW_REQUIRED",
    "NEEDS_HUMAN",
    "BUDGET_EXCEEDED",
    "TASK_FINISHED",
)

# ── LLM role（§8）──────────────────────────────────────────
Role = Literal[
    "reference",
    "constraint",
    "script",
    "storyboard",
    "prompt_pack",
    "review",
    "tagger",
]
ROLES: tuple[Role, ...] = (
    "reference",
    "constraint",
    "script",
    "storyboard",
    "prompt_pack",
    "review",
    "tagger",
)

# ── 提示词五标记（§9.1）────────────────────────────────────
PROMPT_MARKERS: tuple[str, ...] = ("[CHAR]", "[SCENE]", "[CAMERA]", "[ACTION]", "[LIGHT]")
