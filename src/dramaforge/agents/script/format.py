"""职责：单集剧本 markdown 的唯一格式定义处（渲染 + 解析），保证 `episodes` 与 `blocks` 两个阶段可机读对齐。
输入：显式结构（场景/人物/动作/台词）或 LLM 产出的剧本 md 文本。
输出：markdown 文本；解析出的 Block / Scene 列表。

副作用：无（纯字符串处理）。格式约定：
    ## 第NN集 标题
    ### 场NN 地点 · 时间
    人物: c_x, c_y
    锚: 场锚文本
    - 动作: ...
    - 台词(c_x): ...
"""

from __future__ import annotations

import re

from ...contracts.artifacts import Block, Scene
from ...contracts.ids import block_id, scene_id

EPISODE_RE = re.compile(r"^#+\s*第\s*(?P<ep>\d+)\s*集\s*(?P<title>.*)$")
SCENE_RE = re.compile(r"^#+\s*场\s*(?P<scene>\d+)\s*(?P<loc>.*)$")
CAST_RE = re.compile(r"^人物\s*[:：]\s*(?P<ids>.*)$")
ANCHOR_RE = re.compile(r"^锚\s*[:：]\s*(?P<text>.*)$")
ACTION_RE = re.compile(r"^[-*]\s*动作\s*[:：]\s*(?P<text>.*)$")
LINE_RE = re.compile(r"^[-*]\s*台词\s*[（(](?P<who>[^）)]*)[）)]\s*[:：]\s*(?P<text>.*)$")

# 兼容格式 1: `- character_id：（动作）｜台词` 或 `- character_id: 台词`
ALT_LINE_RE = re.compile(r"^[-*]\s*(?P<who>[a-zA-Z0-9_\u4e00-\u9fa5]+)\s*[:：]\s*(?:[（(](?P<act>[^）)]*)[）)])?\s*[|｜]?\s*(?P<text>.*)$")
# 兼容格式 2: `character_id: 台词` 无列表符号
BARE_LINE_RE = re.compile(r"^(?P<who>[a-zA-Z0-9_\u4e00-\u9fa5]+)\s*[:：]\s*(?:[（(](?P<act>[^）)]*)[）)])?\s*[|｜]?\s*(?P<text>.*)$")

FALLBACK_ANCHOR_PREFIX = "场锚"


def render_episode(
    episode: int,
    title: str,
    scenes: list[dict],
) -> str:
    """把结构化场景渲染成约定格式的 markdown。无副作用。

    scenes 每项形如 {"scene": 1, "location": "城中村天台", "time_of_day": "夜",
                    "anchor": "...", "characters": ["c_a"], "beats": [("动作", "c_a", "推开铁门。")]}
    """
    lines = [f"## 第{episode:02d}集 {title}".rstrip(), ""]
    for sc in scenes:
        lines.append(f"### 场{int(sc.get('scene', 1)):02d} {sc.get('location', '')} · {sc.get('time_of_day', '')}".rstrip())
        cast = ", ".join(sc.get("characters", []))
        lines.append(f"人物: {cast}")
        lines.append(f"锚: {sc.get('anchor', '')}")
        lines.append("")
        for kind, who, text in sc.get("beats", []):
            if kind == "动作":
                lines.append(f"- 动作: {text}")
            else:
                lines.append(f"- 台词({who}): {text}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def parse_blocks(md_text: str, episode: int) -> tuple[list[Block], list[Scene]]:
    """解析单集 md 为 blocks 与 scenes（同一 block 归属唯一场）。无副作用。

    无法识别的行忽略；场景缺少锚时用「地点 · 时间」兜底，保证 scene_anchor 永不为空。
    """
    blocks: list[Block] = []
    scenes: list[Scene] = []
    cur_scene: dict | None = None
    seq_in_episode = 0
    scene_no = 0

    def flush_scene() -> None:
        if cur_scene is None:
            return
        scenes.append(
            Scene(
                scene_id=scene_id(episode, cur_scene["scene"]),
                episode=episode,
                scene=cur_scene["scene"],
                location=cur_scene["location"],
                time_of_day=cur_scene["time_of_day"],
                scene_anchor=cur_scene["anchor"],
                characters=list(cur_scene["characters"]),
                block_ids=list(cur_scene["block_ids"]),
            )
        )

    for raw in (md_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = SCENE_RE.match(line)
        if m:
            flush_scene()
            scene_no = int(m.group("scene"))
            raw_loc = (m.group("loc") or "").strip()
            # 拆分地点与时间（如 "内 · 高三教室 · 日"）
            parts = [p.strip() for p in re.split(r"[·|/]", raw_loc) if p.strip()]
            loc = parts[1] if len(parts) >= 2 and parts[0] in {"内", "外", "日", "夜"} else (parts[0] if parts else "默认场景")
            tod = parts[-1] if len(parts) >= 2 else "日"
            cur_scene = {
                "scene": scene_no,
                "location": loc,
                "time_of_day": tod,
                "anchor": f"{loc}，{tod}".strip("，"),
                "characters": [],
                "block_ids": [],
            }
            continue
        if EPISODE_RE.match(line):
            continue
        m = CAST_RE.match(line)
        if m and cur_scene is not None:
            ids = [x.strip() for x in re.split(r"[,，、\s]+", m.group("ids")) if x.strip()]
            cur_scene["characters"] = ids
            continue
        m = ANCHOR_RE.match(line)
        if m and cur_scene is not None:
            anchor = m.group("text").strip()
            if anchor:
                cur_scene["anchor"] = anchor
            continue
        m = ACTION_RE.match(line)
        if m and cur_scene is not None:
            seq_in_episode += 1
            text = m.group("text").strip()
            bid = block_id(episode, seq_in_episode)
            blocks.append(
                Block(
                    block_id=bid,
                    episode=episode,
                    scene=cur_scene["scene"],
                    seq=seq_in_episode,
                    summary=text[:60],
                    text=text,
                    characters=_mentioned(text, cur_scene["characters"]),
                )
            )
            cur_scene["block_ids"].append(bid)
            continue
        m = LINE_RE.match(line)
        if m and cur_scene is not None:
            seq_in_episode += 1
            who = (m.group("who") or "").strip()
            text = m.group("text").strip()
            bid = block_id(episode, seq_in_episode)
            chars = [who] if who else _mentioned(text, cur_scene["characters"])
            blocks.append(
                Block(
                    block_id=bid,
                    episode=episode,
                    scene=cur_scene["scene"],
                    seq=seq_in_episode,
                    summary=f"{who}: {text}"[:60],
                    text=text,
                    characters=chars,
                )
            )
            cur_scene["block_ids"].append(bid)
            continue

        # 兼容匹配 ALT_LINE_RE 或 BARE_LINE_RE
        m = ALT_LINE_RE.match(line) or BARE_LINE_RE.match(line)
        if m:
            if cur_scene is None:
                scene_no += 1
                cur_scene = {
                    "scene": scene_no,
                    "location": "默认场景",
                    "time_of_day": "日",
                    "anchor": "默认场景，日",
                    "characters": [],
                    "block_ids": [],
                }
            who = (m.group("who") or "").strip()
            act = (m.group("act") or "").strip()
            text = (m.group("text") or "").strip()
            if who and who not in cur_scene["characters"]:
                cur_scene["characters"].append(who)
            if act:
                seq_in_episode += 1
                act_bid = block_id(episode, seq_in_episode)
                blocks.append(
                    Block(
                        block_id=act_bid,
                        episode=episode,
                        scene=cur_scene["scene"],
                        seq=seq_in_episode,
                        summary=f"{who} {act}"[:60],
                        text=act,
                        characters=[who],
                    )
                )
                cur_scene["block_ids"].append(act_bid)
            if text:
                seq_in_episode += 1
                line_bid = block_id(episode, seq_in_episode)
                blocks.append(
                    Block(
                        block_id=line_bid,
                        episode=episode,
                        scene=cur_scene["scene"],
                        seq=seq_in_episode,
                        summary=f"{who}: {text}"[:60],
                        text=text,
                        characters=[who],
                    )
                )
                cur_scene["block_ids"].append(line_bid)
            continue
    flush_scene()
    return blocks, scenes


def _mentioned(text: str, cast: list[str]) -> list[str]:
    """返回文本里提到的场景人物 id（按 id 或名字片段命中）。无副作用。"""
    out: list[str] = []
    for cid in cast:
        if cid and cid in text:
            out.append(cid)
    return out


def count_episodes(md_texts: list[str]) -> int:
    """从多集 md 的标题行统计集数（用于 episode_count_range 的 block 视角）。无副作用。"""
    n = 0
    for text in md_texts:
        if EPISODE_RE.search((text or "").strip().splitlines()[0] if text.strip() else ""):
            n += 1
    return n
