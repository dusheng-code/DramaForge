"""职责：M1 卡片库初始化脚本：把 ≥30 张参照卡写进 MySQL `reference_card` 与 Milvus `drama_card`（双路索引）。
输入：`--limit N`（默认 40）、`--drop`（重建 collection）、`--no-milvus`。
输出：写入行数统计与一次 hybrid 检索自检结果。

副作用：写 MySQL、写 Milvus（可能长时间建索引）、惰性加载 BGE-M3（不可用时用确定性 hash 向量并 warn）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from typing import Any

from loguru import logger

from ..contracts.enums import PAYOFF_TAGS, SLOT_KEYS
from ..core.config import Settings, get_settings
from ..retrieval.sql_repo import ReferenceCardRepo

# 30+ 张参照卡的内置语料（结构/情绪/语感/合规/立意五类，覆盖两个平台与真实榜单口径）
CARDS: list[dict[str, Any]] = [
    # ── premise ──
    dict(slot_key="premise", assertion="爆款短剧前三秒必须出现一次明确的冲突或身份置疑，否则完播率断崖", numeric_value=3.0, numeric_unit="秒", platform="douyin", title="闪婚后她成了首富", heat_score=93.2, tags=["悬念钩", "身份揭穿"]),
    dict(slot_key="premise", assertion="主角必须在第一集内被明确剥夺某样东西（婚姻/工作/尊严），观众才愿意看翻身", platform="douyin", title="退婚后我继承家业", heat_score=91.5, tags=["逆袭", "打脸"]),
    dict(slot_key="premise", assertion="对立面要具体到人（婆婆/前夫/上司），不要用抽象的『命运』做对手", platform="kuaishou", title="婆婆的隐藏身份", heat_score=88.0, tags=["护短"]),
    dict(slot_key="premise", assertion="题材越垂直越好，单剧只服务一个情绪承诺（爽/虐/甜），混搭会让推荐不精准", platform="douyin", title="三十岁的离婚协议", heat_score=84.3, tags=["误会解除"]),
    dict(slot_key="premise", assertion="主角要有可识别的职业身份，方便在每一集用工作场景制造新冲突", platform="kuaishou", title="我在豪门当保姆", heat_score=82.7, tags=["逆袭"]),
    dict(slot_key="premise", assertion="全剧只保留两条人物关系（主角—施害者、主角—帮手），第三条关系会稀释注意力", platform="douyin", title="离婚当天我签了大单", heat_score=80.1, tags=["打脸"]),
    # ── structure ──
    dict(slot_key="structure", assertion="单集镜头数控制在 6 到 12 个之间，超出会让单镜时长不足 3 秒、信息来不及落地", numeric_value=12.0, numeric_unit="镜", platform="douyin", title="闪婚后她成了首富", heat_score=93.2, tags=["悬念钩"]),
    dict(slot_key="structure", assertion="单集时长以 60-90 秒为锚（竖屏滑走成本极低），超过 90 秒完播率显著下滑", numeric_value=90.0, numeric_unit="秒", platform="douyin", title="退婚后我继承家业", heat_score=91.5, tags=["悬念钩"]),
    dict(slot_key="structure", assertion="第一集必须在 8 秒内进入第一次对抗，任何铺垫都不允许放在开场", numeric_value=8.0, numeric_unit="秒", platform="douyin", title="婆婆的隐藏身份", heat_score=88.0, tags=["悬念钩"]),
    dict(slot_key="structure", assertion="反转必须落在第 1 到第 2 集之间，越晚观众越觉得被骗", numeric_value=2.0, numeric_unit="集", platform="kuaishou", title="三十岁的离婚协议", heat_score=84.3, tags=["反杀"]),
    dict(slot_key="structure", assertion="每集结尾必须留一个未解决的强钩子（人物忽然出现/信息忽然暴露），不允许收束", platform="douyin", title="我在豪门当保姆", heat_score=82.7, tags=["悬念钩"]),
    dict(slot_key="structure", assertion="换装/衣着变化要与集号绑定，跨集随意换装会造成『不是同一个人』的错觉", platform="kuaishou", title="离婚当天我签了大单", heat_score=80.1, tags=["误会解除"]),
    dict(slot_key="structure", assertion="一集内只允许一次情绪转向（爽→虐 或 虐→爽），两次转向会让情绪失真", platform="douyin", title="她来自十年后", heat_score=79.4, tags=["悬念钩"]),
    # ── emotion ──
    dict(slot_key="emotion", assertion="爽点密度参考值：每集至少一次明确的『打脸/反杀』命中，间隔不超过 20 秒", numeric_value=20.0, numeric_unit="秒", platform="douyin", title="闪婚后她成了首富", heat_score=93.2, tags=["打脸", "反杀"]),
    dict(slot_key="emotion", assertion="打脸必须由主角**亲自**完成，借他人之手会让观众爽感打折", platform="douyin", title="退婚后我继承家业", heat_score=91.5, tags=["打脸"]),
    dict(slot_key="emotion", assertion="虐点只能点到为止：一句羞辱 + 一个低头即可，超过两句观众会弃剧", platform="kuaishou", title="婆婆的隐藏身份", heat_score=88.0, tags=["误会解除"]),
    dict(slot_key="emotion", assertion="单镜动作只做一件事，观众在 3 秒内无法解析两个并行动作", numeric_value=1.0, numeric_unit="动作", platform="douyin", title="三十岁的离婚协议", heat_score=84.3, tags=["喜剧缓和"]),
    dict(slot_key="emotion", assertion="每 3 集安排一次喜剧缓和（误会/错位），用于拉平情绪曲线避免疲劳", numeric_value=3.0, numeric_unit="集", platform="kuaishou", title="我在豪门当保姆", heat_score=82.7, tags=["喜剧缓和"]),
    dict(slot_key="emotion", assertion="身份揭穿要分两次：第一次只揭一半（观众知道、对手不知道），第二次才公开", platform="douyin", title="离婚当天我签了大单", heat_score=80.1, tags=["身份揭穿"]),
    dict(slot_key="emotion", assertion="亮底牌的镜头要给足停留（≥2 秒），一闪而过的底牌等于没亮", numeric_value=2.0, numeric_unit="秒", platform="douyin", title="她来自十年后", heat_score=79.4, tags=["亮底牌"]),
    # ── platform_voice ──
    dict(slot_key="platform_voice", assertion="单句台词不超过 18 个汉字，超过必须拆句（竖屏字幕一屏放不下）", numeric_value=18.0, numeric_unit="字", platform="douyin", title="闪婚后她成了首富", heat_score=93.2, tags=["打脸"]),
    dict(slot_key="platform_voice", assertion="台词与画面同步率：按每秒 4.5 字配音，台词字数不能超过镜头时长能容纳的量", numeric_value=4.5, numeric_unit="字/秒", platform="douyin", title="退婚后我继承家业", heat_score=91.5, tags=["悬念钩"]),
    dict(slot_key="platform_voice", assertion="每个角色给一句可复现的口头禅，跨角色不得重复，用于听感识别", platform="kuaishou", title="婆婆的隐藏身份", heat_score=88.0, tags=["护短"]),
    dict(slot_key="platform_voice", assertion="平台语感偏直给：不要用文学化比喻，观众在手机上不做二次解码", platform="douyin", title="三十岁的离婚协议", heat_score=84.3, tags=["打脸"]),
    dict(slot_key="platform_voice", assertion="人物称谓必须前后一致（『姐』『嫂子』不能混用），否则观众分不清关系", platform="kuaishou", title="我在豪门当保姆", heat_score=82.7, tags=["误会解除"]),
    dict(slot_key="platform_voice", assertion="提示词必须显式写明运镜与光线，否则模型会默认回到平光中景", platform="douyin", title="离婚当天我签了大单", heat_score=80.1, tags=["亮底牌"]),
    # ── risk ──
    dict(slot_key="risk", assertion="禁止出现真实品牌贬损与真实机构名称，违者整条视频下架", platform="douyin", title="平台合规口径", heat_score=70.0, tags=["悬念钩"]),
    dict(slot_key="risk", assertion="禁止未成年人参与情感/暴力情节，涉及学生设定必须年满 18 岁", platform="douyin", title="平台合规口径", heat_score=70.0, tags=["悬念钩"]),
    dict(slot_key="risk", assertion="禁止展示具体医疗操作与药物名称，只能出现『吃药』这类模糊表述", platform="kuaishou", title="平台合规口径", heat_score=68.0, tags=["悬念钩"]),
    dict(slot_key="risk", assertion="家暴/囚禁等敏感情节必须以法律介入收尾，不能以『原谅』收尾", platform="douyin", title="平台合规口径", heat_score=68.0, tags=["反杀"]),
    dict(slot_key="risk", assertion="不得出现具体的金融产品收益率与投资建议，涉及金钱只谈情节不谈产品", platform="kuaishou", title="平台合规口径", heat_score=66.0, tags=["亮底牌"]),
    dict(slot_key="risk", assertion="与已有爆款相似度需要披露并给出差异点，同质化会被判重复内容", platform="douyin", title="平台合规口径", heat_score=66.0, tags=["悬念钩"]),
    dict(slot_key="premise", assertion="主角的动机要在第一集写明（报复/守护/证明自己），模糊动机会让观众找不到代入点", platform="kuaishou", title="她的第二人生", heat_score=78.0, tags=["复仇推进"]),
    dict(slot_key="structure", assertion="每集第一个镜头必须是人物近景，不要用空镜开场（空镜留不住手指）", platform="douyin", title="她的第二人生", heat_score=78.0, tags=["悬念钩"]),
    dict(slot_key="emotion", assertion="复仇推进要一集一小步：每集解决一个具体的小障碍，而不是一次性解决所有", platform="kuaishou", title="她的第二人生", heat_score=78.0, tags=["复仇推进"]),
    dict(slot_key="platform_voice", assertion="旁白总量不超过每集两句，且必须承担信息推进，不能用来抒情", platform="douyin", title="她的第二人生", heat_score=78.0, tags=["悬念钩"]),
]


def _card_id(card: dict, index: int) -> str:
    """生成稳定 card_id（slot + 序号），便于幂等 upsert。无副作用。"""
    return f"c_{card['slot_key']}_{index:03d}"


def _rows(limit: int) -> list[dict]:
    """把内置语料转成 reference_card 行（补齐 card_id/tags/source_url）。无副作用。"""
    cards = CARDS[:limit] if limit > 0 else CARDS
    rows: list[dict] = []
    per_slot: dict[str, int] = {k: 0 for k in SLOT_KEYS}
    for card in cards:
        per_slot[card["slot_key"]] = per_slot.get(card["slot_key"], 0) + 1
        cid = _card_id(card, per_slot[card["slot_key"]])
        rows.append(
            {
                "card_id": cid,
                "slot_key": card["slot_key"],
                "assertion": card["assertion"],
                "numeric_value": card.get("numeric_value"),
                "numeric_unit": card.get("numeric_unit"),
                "platform": card.get("platform", "douyin"),
                "title": card.get("title"),
                "heat_score": card.get("heat_score"),
                "source_url": f"https://example.com/ref/{cid}",
                "payload": {"tags": card.get("tags", []), "title": card.get("title")},
            }
        )
    return rows


class HashEmbedder:
    """确定性 hash 向量（无 FlagEmbedding 时的降级方案）：同文本必然同向量，仅保证链路可跑。"""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = int(dim)
        self.model_name = f"hash-{self.dim}"

    @property
    def available(self) -> bool:
        """永远可用（不依赖第三方模型）。无副作用。"""
        return True

    def _vec(self, text: str) -> list[float]:
        """单文本 → dim 维 L2 归一化向量。无副作用。"""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "big")
        vec: list[float] = []
        counter = 0
        while len(vec) < self.dim:
            block = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).digest()
            for i in range(0, len(block), 2):
                if len(vec) >= self.dim:
                    break
                vec.append(int.from_bytes(block[i : i + 2], "big") / 32768.0 - 1.0)
            counter += 1
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        """查询向量。无副作用。"""
        return [self._vec(t) for t in texts]

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        """文档向量。无副作用。"""
        return [self._vec(t) for t in texts]

    def encode_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """词袋稀疏向量（token hash → 权重），供 hybrid 的 sparse 路。无副作用。"""
        out: list[dict[int, float]] = []
        for text in texts:
            bucket: dict[int, float] = {}
            for token in text.split() or list(text):
                idx = int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:4], "big") % 100_000
                bucket[idx] = bucket.get(idx, 0.0) + 1.0
            total = sum(bucket.values()) or 1.0
            out.append({k: v / total for k, v in bucket.items()})
        return out


async def _embedder(settings: Settings):
    """优先 BGE-M3（需 `uv sync --extra embed`），不可用时降级 hash 向量并 warn。副作用：可能加载模型。"""
    try:
        from ..retrieval.milvus_repo import BgeM3Embedder

        emb = BgeM3Embedder(model_name=settings.embed_model, dim=int(settings.embed_dim))
        if not emb.available:
            raise RuntimeError("FlagEmbedding not installed")
        emb.encode_queries(["自检"])  # 触发惰性加载，失败立刻暴露
        logger.info("embedder = BGE-M3 ({} dim)", settings.embed_dim)
        return emb
    except Exception as exc:  # noqa: BLE001 —— 未安装/加载失败则降级，不阻断 M1 链路验证
        logger.warning("BGE-M3 不可用（{}），改用确定性 hash 向量：检索链路真实运行，语义质量待 M9 回填", type(exc).__name__)
        return HashEmbedder(int(settings.embed_dim))


async def run(args: argparse.Namespace) -> int:
    """写入卡片并做一次 hybrid 检索自检。副作用：见模块头。"""
    settings = get_settings()
    rows = _rows(int(args.limit))
    repo = ReferenceCardRepo.from_settings(settings)
    try:
        n_sql = await repo.upsert_cards(rows)
        print(f"[mysql] upsert {n_sql} rows -> reference_card (total={len(await repo.all_cards())})")
        rank = await repo.rank_stats()
        print(f"[mysql] rank_stats total={rank.get('total')} platforms={rank.get('platforms')}")

        if args.no_milvus:
            print("[milvus] skipped (--no-milvus)")
            return 0
        from ..retrieval.milvus_repo import CardDoc, MilvusCardRepo

        embedder = await _embedder(settings)
        milvus = MilvusCardRepo(settings, embedder=embedder)
        if args.drop:
            milvus.ensure_collection(drop=True)
        else:
            milvus.ensure_collection()
        docs = [
            CardDoc(card_id=row["card_id"], slot_key=row["slot_key"], text=row["assertion"]) for row in rows
        ]
        n_mv = await milvus.upsert_cards(docs)
        print(f"[milvus] upsert {n_mv} docs -> {settings.milvus_uri}/{settings.milvus_collection} (embedder={embedder.model_name})")
        queries = ["前三秒冲突", "镜头数预算"]
        hits_all = []
        for q in queries:
            hits = await milvus.hybrid_search(q, slot_keys=["premise", "structure"], top_k=3)
            print(f"[milvus] hybrid_search({q}) top{len(hits)}:")
            for h in hits:
                print(f"   score={getattr(h, 'score', 0):.6f} slot={getattr(h, 'slot_key', '?')} id={getattr(h, 'card_id', '?')}")
            assert hits, f"hybrid_search({q}) 无命中，链路异常"
            assert len(hits) <= 3
            hits_all.extend(hits)
        print("[ok] M1 卡片库就绪")
        return 0
    finally:
        await repo.close()


def main(argv: list[str] | None = None) -> int:
    """命令行入口。副作用：建连接、写库、加载模型。"""
    p = argparse.ArgumentParser(prog="build_cards", description="写入参照卡（MySQL + Milvus）")
    p.add_argument("--limit", type=int, default=40, help="写入前 N 张（0 表示全部）")
    p.add_argument("--drop", action="store_true", help="重建 Milvus collection")
    p.add_argument("--no-milvus", action="store_true", help="只写 MySQL")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
