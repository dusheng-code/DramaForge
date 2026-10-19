"""职责：M1/M9 离线批量向量化脚本，调用 BGE-M3 产出 dense + sparse 向量并批量同步回 Milvus。
输入：`--batch-size N`，`--limit N`，`--collection name`。
输出：离线计算完成条数与耗时统计。

副作用：加载 BGE-M3 模型（重内存约 2.2GB），读写 Milvus。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from loguru import logger

from ..core.config import Settings, get_settings
from ..retrieval.milvus_repo import CardDoc, MilvusCardRepo, get_embedder
from ..retrieval.sql_repo import ReferenceCardRepo


async def run_embed_offline(args: argparse.Namespace) -> int:
    """离线批量向量化并更新 Milvus。"""
    settings = get_settings()
    collection_name = args.collection or settings.milvus_collection
    batch_size = max(1, int(args.batch_size))
    limit = int(args.limit)

    embedder = get_embedder(settings)
    if not embedder.available:
        logger.error(
            "FlagEmbedding 未安装，无法执行真实离线批量向量化！"
            "请运行: uv sync --extra embed 并安装 CUDA/Torch 依赖。"
        )
        return 2

    logger.info("初始化 SQL 与 Milvus 连接 (collection={})...", collection_name)
    sql_repo = ReferenceCardRepo.from_settings(settings)
    milvus_repo = MilvusCardRepo(settings, embedder=embedder)
    milvus_repo.ensure_collection(drop=False)

    try:
        cards = await sql_repo.all_cards()
        if limit > 0:
            cards = cards[:limit]
        total = len(cards)
        logger.info("从 MySQL 读取到 {} 张参照卡待离线向量化", total)
        if not total:
            logger.warning("未检索到任何参照卡，请先执行 build_cards.py")
            return 0

        t0 = time.time()
        synced = 0
        for i in range(0, total, batch_size):
            chunk = cards[i : i + batch_size]
            docs = [
                CardDoc(
                    card_id=str(c.get("card_id")),
                    slot_key=str(c.get("slot_key")),
                    text=str(c.get("assertion") or ""),
                )
                for c in chunk
            ]
            n = await milvus_repo.upsert_cards(docs)
            synced += n
            logger.info("进度: {}/{} 完成", synced, total)

        elapsed = time.time() - t0
        logger.info("离线向量化完成: 成功同步 {} 条，耗时 {:.2f}s", synced, elapsed)
        return 0
    finally:
        await sql_repo.close()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(prog="embed_offline", description="离线批量向量化参照卡并回填 Milvus")
    parser.add_argument("--batch-size", type=int, default=32, help="单批向量化大小")
    parser.add_argument("--limit", type=int, default=0, help="处理前 N 条 (0为全量)")
    parser.add_argument("--collection", type=str, default="", help="目标集合名")
    args = parser.parse_args(argv)
    return asyncio.run(run_embed_offline(args))


if __name__ == "__main__":
    sys.exit(main())
