"""职责：全系统唯一的磁盘读写入口与唯一的路径构造处（§0.5）：`paths()` 产出的 TaskPaths 提供所有产物路径。
输入：task_id、产物内容（JSON / 文本 / 字节）、Settings。
输出：写入磁盘的原子文件；或读取到的 dict / str / bytes。所有写操作先写临时文件再 replace。

副作用：创建目录、写文件、读文件。业务代码禁止自行 `open()` 或拼接路径字符串。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import Settings, get_settings

SUBDIRS: tuple[str, ...] = (
    "reference",
    "script",
    "script/episodes",
    "storyboard",
    "prompt_pack",
    "assets",
    "assets/anchors",
    "assets/keyframes",
    "assets/videos",
    "review",
    "state",
    "final",
    "log",
)


@dataclass(frozen=True)
class TaskPaths:
    """一个任务的产物目录视图（§15）。所有属性都是 Path，不做 IO。"""

    task_id: str
    root: Path

    # ── 目录 ───────────────────────────────────────────────
    @property
    def reference(self) -> Path:
        """reference/ 目录。无副作用。"""
        return self.root / "reference"

    @property
    def script(self) -> Path:
        """script/ 目录。无副作用。"""
        return self.root / "script"

    @property
    def episodes(self) -> Path:
        """script/episodes/ 目录。无副作用。"""
        return self.script / "episodes"

    @property
    def storyboard(self) -> Path:
        """storyboard/ 目录。无副作用。"""
        return self.root / "storyboard"

    @property
    def prompt_pack(self) -> Path:
        """prompt_pack/ 目录。无副作用。"""
        return self.root / "prompt_pack"

    @property
    def assets(self) -> Path:
        """assets/ 目录。无副作用。"""
        return self.root / "assets"

    @property
    def anchors(self) -> Path:
        """assets/anchors/ 目录。无副作用。"""
        return self.assets / "anchors"

    @property
    def keyframes(self) -> Path:
        """assets/keyframes/ 目录。无副作用。"""
        return self.assets / "keyframes"

    @property
    def videos(self) -> Path:
        """assets/videos/ 目录。无副作用。"""
        return self.assets / "videos"

    @property
    def review(self) -> Path:
        """review/ 目录。无副作用。"""
        return self.root / "review"

    @property
    def state(self) -> Path:
        """state/ 目录。无副作用。"""
        return self.root / "state"

    @property
    def final(self) -> Path:
        """final/ 目录。无副作用。"""
        return self.root / "final"

    @property
    def log(self) -> Path:
        """log/ 目录。无副作用。"""
        return self.root / "log"

    # ── reference ─────────────────────────────────────────
    @property
    def cards_used_json(self) -> Path:
        """reference/cards_used.json。无副作用。"""
        return self.reference / "cards_used.json"

    @property
    def rank_stats_json(self) -> Path:
        """reference/rank_stats.json。无副作用。"""
        return self.reference / "rank_stats.json"

    @property
    def search_snapshot_json(self) -> Path:
        """reference/search_snapshot.json。无副作用。"""
        return self.reference / "search_snapshot.json"

    @property
    def constraints_json(self) -> Path:
        """reference/constraints.json。无副作用。"""
        return self.reference / "constraints.json"

    # ── script ────────────────────────────────────────────
    @property
    def outline_md(self) -> Path:
        """script/outline.md。无副作用。"""
        return self.script / "outline.md"

    def episode_md(self, episode: int) -> Path:
        """script/episodes/ep{N}.md（N 补零两位）。无副作用。"""
        return self.episodes / f"ep{episode:02d}.md"

    @property
    def characters_json(self) -> Path:
        """script/characters.json。无副作用。"""
        return self.script / "characters.json"

    @property
    def blocks_json(self) -> Path:
        """script/blocks.json。无副作用。"""
        return self.script / "blocks.json"

    @property
    def segment_chapters_md(self) -> Path:
        """script/segment_chapters.md。无副作用。"""
        return self.script / "segment_chapters.md"

    # ── storyboard / prompt_pack ──────────────────────────
    @property
    def scenes_json(self) -> Path:
        """storyboard/scenes.json。无副作用。"""
        return self.storyboard / "scenes.json"

    @property
    def shots_json(self) -> Path:
        """storyboard/shots.json。无副作用。"""
        return self.storyboard / "shots.json"

    @property
    def packs_json(self) -> Path:
        """prompt_pack/packs.json。无副作用。"""
        return self.prompt_pack / "packs.json"

    # ── review ────────────────────────────────────────────
    @property
    def constraint_report_json(self) -> Path:
        """review/constraint_report.json。无副作用。"""
        return self.review / "constraint_report.json"

    @property
    def violations_json(self) -> Path:
        """review/violations.json。无副作用。"""
        return self.review / "violations.json"

    @property
    def cost_quote_json(self) -> Path:
        """review/cost_quote.json。无副作用。"""
        return self.review / "cost_quote.json"

    @property
    def pending_human_json(self) -> Path:
        """review/pending_human.json。无副作用。"""
        return self.review / "pending_human.json"

    @property
    def resolutions_json(self) -> Path:
        """review/resolutions.json。无副作用。"""
        return self.review / "resolutions.json"

    # ── assets ────────────────────────────────────────────
    def anchor_png(self, character_id: str) -> Path:
        """assets/anchors/{character_id}.png。无副作用。"""
        return self.anchors / f"{character_id}.png"

    def keyframe_png(self, shot_id: str) -> Path:
        """assets/keyframes/{shot_id}.png（shot_id 中的 ':' 换成 '_'）。无副作用。"""
        return self.keyframes / f"{_safe(shot_id)}.png"

    def video_mp4(self, shot_id: str) -> Path:
        """assets/videos/{shot_id}.mp4（shot_id 中的 ':' 换成 '_'）。无副作用。"""
        return self.videos / f"{_safe(shot_id)}.mp4"

    # ── state / final / log ───────────────────────────────
    @property
    def stage_snapshot_json(self) -> Path:
        """state/stage_snapshot.json。无副作用。"""
        return self.state / "stage_snapshot.json"

    @property
    def asset_snapshot_json(self) -> Path:
        """state/asset_snapshot.json。无副作用。"""
        return self.state / "asset_snapshot.json"

    @property
    def drama_package_md(self) -> Path:
        """final/drama_package.md。无副作用。"""
        return self.final / "drama_package.md"

    @property
    def final_shots_json(self) -> Path:
        """final/shots.json。无副作用。"""
        return self.final / "shots.json"

    @property
    def asset_report_json(self) -> Path:
        """final/asset_report.json。无副作用。"""
        return self.final / "asset_report.json"

    def stage_log(self, stage: str) -> Path:
        """log/{stage}.log。无副作用。"""
        return self.log / f"{_safe(stage)}.log"

    def ensure(self) -> "TaskPaths":
        """创建全部产物子目录。副作用：mkdir。"""
        for sub in SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        return self


def _safe(name: str) -> str:
    """把 id 里的路径分隔/冒号替换成下划线，避免逃逸产物目录。无副作用。"""
    return name.replace(":", "_").replace("\\", "_").replace("/", "_")


def paths(task_id: str | None = None, settings: Settings | None = None) -> TaskPaths:
    """唯一路径入口：task_id 为空时指 RUNTIME_DIR 根，否则指 var/{task_id}。无副作用。"""
    s = settings or get_settings()
    root = s.runtime_path if not task_id else s.runtime_path / task_id
    return TaskPaths(task_id=task_id or "", root=root)


# ── 读写原语（业务代码只用这些）──────────────────────────────
def ensure_dir(path: Path) -> Path:
    """确保父目录存在。副作用：mkdir。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, obj: Any) -> Path:
    """原子写 JSON（ensure_ascii=False，缩进 2）。副作用：写磁盘。"""
    ensure_dir(path)
    payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return _atomic_write(path, payload.encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    """读 JSON；文件不存在或损坏时返回 default。副作用：读磁盘。"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return default
    except json.JSONDecodeError:
        return default


def write_text(path: Path, text: str) -> Path:
    """原子写文本（UTF-8）。副作用：写磁盘。"""
    ensure_dir(path)
    return _atomic_write(path, text.encode("utf-8"))


def read_text(path: Path, default: str = "") -> str:
    """读文本；不存在返回 default。副作用：读磁盘。"""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return default


def save_bytes(path: Path, data: bytes) -> Path:
    """原子写二进制（下载落盘的唯一入口）。副作用：写磁盘。"""
    ensure_dir(path)
    return _atomic_write(path, data)


def read_bytes(path: Path) -> bytes:
    """读二进制；不存在抛 FileNotFoundError。副作用：读磁盘。"""
    return path.read_bytes()


def exists(path: Path) -> bool:
    """判断产物是否存在且非空（0 字节视为缺失，对应 §6.5 output_path 校验）。无副作用。"""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def output_ok(path_str: str | None) -> bool:
    """校验 stage_state.output_path 是否仍指向有效产物（可能多个，用 os.pathsep 分隔）。无副作用。"""
    if not path_str:
        return False
    parts = [p for p in path_str.split(os.pathsep) if p]
    return bool(parts) and all(exists(Path(p)) for p in parts)


def remove(path: Path | str) -> bool:
    """删除单个产物文件（打回重抽时用）；不存在返回 False。副作用：删磁盘。"""
    target = Path(path)
    try:
        target.unlink()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return False


def existing_or_none(path_str: str | None) -> Path | None:
    """把落库的路径还原成仍在磁盘上的 Path；不存在返回 None。无副作用。"""
    if not path_str:
        return None
    target = Path(path_str)
    return target if exists(target) else None


def join_output(*paths_in: Path | str) -> str:
    """把多个产物路径拼成 output_path 列（os.pathsep 分隔），便于多产物阶段落库。无副作用。"""
    return os.pathsep.join(str(p) for p in paths_in)


def _atomic_write(path: Path, data: bytes) -> Path:
    """同目录临时文件 + os.replace 的原子写。副作用：写磁盘。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
