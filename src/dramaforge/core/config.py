"""职责：全量配置的唯一入口（§11），把环境变量/`.env` 解析成类型化 Settings，并提供 role 级 LLM 配置与解析后的列表常量。
输入：环境变量、`.env` 文件（优先级：真实环境变量 > .env）。
输出：Settings 单例（get_settings）与各消费方需要的强类型值。

副作用：读取进程环境与 `.env` 文件；不写磁盘、不发网络请求。
禁止：把配置值硬编码进业务逻辑（§0.4）；本模块之外不得直接读 os.environ。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RoleLLMConfig(BaseModel):
    """单个 LLM role 的连接与采样参数（LLM_{ROLE}_*）。"""

    role: str
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 4000

    @property
    def configured(self) -> bool:
        """是否具备真实调用条件（key 与 base_url 均非空）。无副作用。"""
        return bool(self.api_key) and bool(self.base_url)


class Settings(BaseSettings):
    """§11 全量配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # ── 运行时与全局 ────────────────────────────────────────
    runtime_dir: str = "./var"
    strict_mode: bool = False
    topic_default_episodes: int = 8
    episode_duration_target_sec: int = 75
    episode_duration_range_sec: str = "60,90"
    min_shot_per_episode: int = 6
    max_shot_per_episode: int = 12
    shot_duration_ladder: str = "6,10"
    chars_per_sec: float = 4.5
    hero_max_ratio: float = 0.20
    max_revision_per_slot: int = 2
    max_attempt_per_shot: int = 3

    # ── 存储 ───────────────────────────────────────────────
    task_state_db: str = "mysql+aiomysql://root:root@127.0.0.1:3306/dramaforge"
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "drama_card"
    embed_model: str = "BGE-M3"
    embed_dim: int = 1024
    rrf_k: int = 60
    card_top_k: int = 8
    card_milvus_timeout_sec: float = 8.0

    # ── 图像 ───────────────────────────────────────────────
    image_provider: str = "minimax"
    image_model: str = "TBD_M8"
    image_concurrency: int = 2
    image_price_cny: float = 0.2
    image_resolution: str = "768p"
    image_aspect: str = "16:9"

    # ── 视频 ───────────────────────────────────────────────
    video_provider: str = "minimax"
    video_model_fast: str = "hailuo-2.3-fast"
    video_model_hero: str = "hailuo-2.3"
    video_resolution: str = "768p"
    video_aspect: str = "16:9"
    tier_price_raw: str = Field(default="fast:0.225,hero:0.400", alias="TIER_PRICE")
    image_to_video: bool = True
    generate_with_audio: bool = False

    # ── 资产泵 ─────────────────────────────────────────────
    asset_concurrency: int = 3
    poll_backoff: str = "20,40,80"
    poll_timeout_sec: int = 1200
    download_timeout_sec: int = 120

    # ── 预算 ───────────────────────────────────────────────
    max_video_cost_cny: float = 500.0
    max_episode_cost_cny: float = 65.0
    require_quote_approval: bool = True

    # ── 调试 ───────────────────────────────────────────────
    episode_limit: int = 0
    dry_run: bool = False

    # ── LLM role ──────────────────────────────────────────
    llm_role_keys: str = "reference,constraint,script,storyboard,prompt_pack,review,tagger"

    llm_reference_api_key: str = ""
    llm_reference_base_url: str = ""
    llm_reference_model: str = ""
    llm_reference_temperature: float = 0.3
    llm_reference_max_tokens: int = 4000

    llm_constraint_api_key: str = ""
    llm_constraint_base_url: str = ""
    llm_constraint_model: str = ""
    llm_constraint_temperature: float = 0.2
    llm_constraint_max_tokens: int = 4000

    llm_script_api_key: str = ""
    llm_script_base_url: str = ""
    llm_script_model: str = ""
    llm_script_temperature: float = 0.8
    llm_script_max_tokens: int = 8000

    llm_storyboard_api_key: str = ""
    llm_storyboard_base_url: str = ""
    llm_storyboard_model: str = ""
    llm_storyboard_temperature: float = 0.6
    llm_storyboard_max_tokens: int = 8000

    llm_prompt_pack_api_key: str = ""
    llm_prompt_pack_base_url: str = ""
    llm_prompt_pack_model: str = ""
    llm_prompt_pack_temperature: float = 0.4
    llm_prompt_pack_max_tokens: int = 4000

    llm_review_api_key: str = ""
    llm_review_base_url: str = ""
    llm_review_model: str = ""
    llm_review_temperature: float = 0.5
    llm_review_max_tokens: int = 2000

    llm_tagger_api_key: str = ""
    llm_tagger_base_url: str = ""
    llm_tagger_model: str = ""
    llm_tagger_temperature: float = 0.0
    llm_tagger_max_tokens: int = 1000

    # ── 外部检索 ───────────────────────────────────────────
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    anspire_api_key: str = ""
    anspire_base_url: str = "https://plugin.anspire.cn/api/ntsearch"
    search_timeout_sec: float = 20.0

    # ── MiniMax ───────────────────────────────────────────
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.chat"
    minimax_image_path: str = "/v1/image_generation"
    minimax_video_submit_path: str = "/v1/video_generation"
    minimax_video_query_path: str = "/query/video_generation"
    minimax_file_retrieve_path: str = "/v1/files/retrieve"
    minimax_group_id: str = ""

    # ── 可复现 ─────────────────────────────────────────────
    llm_seed: int = 20250101

    # ── 提示词与校验用常量（逗号分隔，允许为空）──────────────
    forbidden_terms: str = (
        "自杀,自残,血腥,碎尸,色情,裸露,吸毒,制毒,赌博,诈骗教程,"
        "未成年人恋爱,校园暴力细节,真实品牌贬损,领导人,民族仇恨"
    )
    action_verb_hints: str = (
        "推开,抓住,扑向,抡起,砸下,掀翻,拽住,掐住,抱住,踢开,拉过,走近,转身,"
        "抬头,低头,蹲下,起身,递出,接过,掀开,扯下,甩出,冲进,退回,扶起,按倒"
    )
    style_suffix: str = (
        "写实电影质感,35mm 胶片颗粒,自然肤色,柔和高光,浅景深,"
        "9:16 与 16:9 安全构图,无水印,无字幕"
    )

    # ── 解析后的派生常量（属性，只读）──────────────────────
    @property
    def episode_duration_range(self) -> tuple[int, int]:
        """EPISODE_DURATION_RANGE_SEC → (min, max)；缺失时用目标时长兜底。无副作用。"""
        parts = _split_numbers(self.episode_duration_range_sec)
        if len(parts) < 2:
            target = int(self.episode_duration_target_sec)
            return (target, target)
        low, high = int(parts[0]), int(parts[1])
        return (min(low, high), max(low, high))

    @property
    def shot_ladder(self) -> tuple[float, ...]:
        """SHOT_DURATION_LADDER → 时长档位元组（升序）。无副作用。"""
        vals = tuple(sorted(_split_numbers(self.shot_duration_ladder)))
        return vals or (6.0, 10.0)

    @property
    def per_shot_duration(self) -> float:
        """档位均值，用于返工重排时估算镜数。无副作用。"""
        ladder = self.shot_ladder
        return sum(ladder) / len(ladder)

    @property
    def tier_price(self) -> dict[str, float]:
        """TIER_PRICE → {"fast": x, "hero": y}。无副作用。"""
        return _parse_kv_floats(self.tier_price_raw)

    @property
    def poll_backoff_sec(self) -> tuple[int, ...]:
        """POLL_BACKOFF → 轮询退避序列（秒）。无副作用。"""
        vals = tuple(int(v) for v in _split_numbers(self.poll_backoff))
        return vals or (20, 40, 80)

    @property
    def role_keys(self) -> tuple[str, ...]:
        """LLM_ROLE_KEYS → 已配置的 role 名元组。无副作用。"""
        return _split_str(self.llm_role_keys) or (
            "reference",
            "constraint",
            "script",
            "storyboard",
            "prompt_pack",
            "review",
            "tagger",
        )

    @property
    def forbidden_term_list(self) -> tuple[str, ...]:
        """FORBIDDEN_TERMS → 违禁词元组。无副作用。"""
        return _split_str(self.forbidden_terms)

    @property
    def action_verb_list(self) -> tuple[str, ...]:
        """ACTION_VERB_HINTS → 动作动词提示元组。无副作用。"""
        return _split_str(self.action_verb_hints)

    @property
    def runtime_path(self) -> Path:
        """RUNTIME_DIR 的绝对路径（相对路径按进程 cwd 解析）。无副作用。"""
        p = Path(self.runtime_dir)
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    @property
    def web_dist_path(self) -> Path:
        """前端构建产物目录（<repo>/web/dist）；未构建时不存在。无副作用。"""
        return Path(__file__).resolve().parents[3] / "web" / "dist"

    def role_llm(self, role: str) -> RoleLLMConfig:
        """读取 LLM_{ROLE}_* 一组配置，缺项回退到空值（由 LLMClient 判定不可用）。无副作用。"""
        key = f"llm_{role}"
        return RoleLLMConfig(
            role=role,
            api_key=getattr(self, f"{key}_api_key", "") or "",
            base_url=getattr(self, f"{key}_base_url", "") or "",
            model=getattr(self, f"{key}_model", "") or "",
            temperature=float(getattr(self, f"{key}_temperature", 0.7) or 0.0),
            max_tokens=int(getattr(self, f"{key}_max_tokens", 4000) or 4000),
        )

    def video_model(self, tier: str) -> str:
        """按档位返回视频模型名（§11 VIDEO_MODEL_FAST/HERO）。无副作用。"""
        return self.video_model_hero if tier == "hero" else self.video_model_fast

    def video_price(self, tier: str) -> float:
        """按档位返回单位时长单价；缺配置时回退 0。无副作用。"""
        return self.tier_price.get(tier, 0.0)

    def overrides(self, data: dict[str, Any]) -> "Settings":
        """返回带覆盖项的新 Settings（用于测试与 --dry-run 注入），不修改单例。无副作用。"""
        return self.model_copy(update={k.lower(): v for k, v in data.items()})


def _split_str(raw: str) -> tuple[str, ...]:
    """逗号（中英文）分隔字符串 → 去空元组。无副作用。"""
    if not raw:
        return ()
    raw = raw.replace("，", ",")
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _split_numbers(raw: str) -> tuple[float, ...]:
    """逗号分隔数值字符串 → float 元组，非法项跳过。无副作用。"""
    out: list[float] = []
    for item in _split_str(raw):
        try:
            out.append(float(item))
        except ValueError:
            continue
    return tuple(out)


def _parse_kv_floats(raw: str) -> dict[str, float]:
    """`fast:0.225,hero:0.4` → dict[str, float]；非法项跳过。无副作用。"""
    out: dict[str, float] = {}
    for item in _split_str(raw):
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            continue
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内 Settings 单例（首次调用读环境）。副作用：读环境与 .env 文件。"""
    return Settings()


def reset_settings() -> None:
    """清空单例缓存，供测试注入环境变量后重载。副作用：清缓存。"""
    get_settings.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    """把环境变量解析成布尔（1/true/yes/on）。无副作用。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
