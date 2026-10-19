"""职责：建表脚本（§5.3 六张表的 DDL 唯一存放处）与库/集合初始化；离线进程执行，不进 uvicorn。
输入：TASK_STATE_DB（mysql+aiomysql DSN）、Milvus URI。
输出：建好的 6 张 MySQL 表；可选建好 Milvus collection `drama_card`。

副作用：DDL 与 CREATE DATABASE（幂等，IF NOT EXISTS）；--drop 时删除表。禁止在此写业务数据。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ..core.config import Settings, get_settings

# §5.3 原文 DDL（仅补 IF NOT EXISTS 以保证幂等）
DDL: dict[str, str] = {
    "task": """
CREATE TABLE IF NOT EXISTS task (
  task_id VARCHAR(32) PRIMARY KEY, topic VARCHAR(512) NOT NULL,
  platform VARCHAR(32), episode_count INT, episode_duration_target INT,
  strict_mode TINYINT(1) DEFAULT 0, status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
  cost_cap_cny DECIMAL(8,2), params_json JSON, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    "stage_state": """
CREATE TABLE IF NOT EXISTS stage_state (
  task_id VARCHAR(32) NOT NULL,
  slot_key VARCHAR(16) NOT NULL,
  stage VARCHAR(24) NOT NULL,
  revision INT NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
  fail_reason VARCHAR(1024), output_path VARCHAR(512),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (task_id, slot_key, stage, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    "asset_state": """
CREATE TABLE IF NOT EXISTS asset_state (
  task_id VARCHAR(32) NOT NULL, shot_id VARCHAR(64) NOT NULL, kind VARCHAR(16) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
  attempt INT NOT NULL DEFAULT 0, episode INT NOT NULL,
  tier VARCHAR(8), model VARCHAR(48), resolution VARCHAR(8),
  provider VARCHAR(24), provider_task_id VARCHAR(128),
  revision INT NOT NULL DEFAULT 0,
  cost_cny DECIMAL(8,3) NOT NULL DEFAULT 0,
  file_path VARCHAR(512), est_cost_cny DECIMAL(8,3),
  reject_reason VARCHAR(512),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (task_id, shot_id, kind, revision),
  KEY idx_status (task_id, status), KEY idx_ep (task_id, episode, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    "reference_card": """
CREATE TABLE IF NOT EXISTS reference_card (
  card_id VARCHAR(64) NOT NULL, slot_key VARCHAR(16) NOT NULL,
  title VARCHAR(128), platform VARCHAR(32),
  assertion VARCHAR(512) NOT NULL,
  numeric_value DECIMAL(10,2), numeric_unit VARCHAR(16),
  payload JSON NOT NULL, annotator VARCHAR(32), source_url VARCHAR(512),
  annotated_at DATE,
  PRIMARY KEY (card_id, slot_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    "platform_rank_snapshot": """
CREATE TABLE IF NOT EXISTS platform_rank_snapshot (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, snapshot_date DATE NOT NULL, platform VARCHAR(32),
  title VARCHAR(128), genre_tags VARCHAR(256), episode_count INT,
  avg_duration_sec DECIMAL(7,2), heat_score DECIMAL(12,2),
  heat_source VARCHAR(64), url VARCHAR(512),
  KEY idx_dp (snapshot_date, platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    "resolution": """
CREATE TABLE IF NOT EXISTS resolution (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, task_id VARCHAR(32) NOT NULL,
  target_type VARCHAR(16), target_id VARCHAR(64), action VARCHAR(32),
  operator VARCHAR(32), comment VARCHAR(1024),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
}

# 6 张表按依赖顺序创建（无 FK，顺序仅便于阅读）
TABLE_ORDER: tuple[str, ...] = (
    "task",
    "stage_state",
    "asset_state",
    "reference_card",
    "platform_rank_snapshot",
    "resolution",
)


async def create_schema(engine: AsyncEngine, drop: bool = False) -> None:
    """执行建表（幂等）。副作用：DDL。"""
    async with engine.begin() as conn:
        if drop:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for name in reversed(TABLE_ORDER):
                await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
                logger.warning("dropped table {}", name)
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        for name in TABLE_ORDER:
            await conn.execute(text(DDL[name]))
            logger.info("ensured table {}", name)


def split_dsn(dsn: str) -> tuple[str, str]:
    """把 DSN 拆成 (无库 DSN, 库名)。无副作用。"""
    head, _, tail = dsn.rpartition("/")
    db_name = tail.split("?", 1)[0]
    return f"{head}/", db_name


async def create_database(dsn: str) -> None:
    """若库不存在则创建（utf8mb4）。副作用：CREATE DATABASE。"""
    server_dsn, db_name = split_dsn(dsn)
    engine = create_async_engine(server_dsn + "?charset=utf8mb4", poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        logger.info("ensured database {}", db_name)
    finally:
        await engine.dispose()


async def init_all(settings: Settings | None = None, drop: bool = False, milvus: bool = False) -> None:
    """建库 + 建表 +（可选）建 Milvus collection。副作用：DDL/远程调用。"""
    s = settings or get_settings()
    await create_database(s.task_state_db)
    engine = create_async_engine(s.task_state_db, poolclass=NullPool)
    try:
        await create_schema(engine, drop=drop)
    finally:
        await engine.dispose()
    if milvus:
        from ..retrieval.milvus_repo import ensure_collection

        ensure_collection(s, drop=drop)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：`uv run python -m dramaforge.scripts.init_db [--drop] [--milvus]`。副作用：建库建表。"""
    parser = argparse.ArgumentParser(description="DramaForge 建库建表（§5.3）")
    parser.add_argument("--drop", action="store_true", help="先删表再建（危险，仅本地）")
    parser.add_argument("--milvus", action="store_true", help="同时创建 Milvus collection drama_card")
    args = parser.parse_args(argv)
    asyncio.run(init_all(drop=args.drop, milvus=args.milvus))
    logger.info("init_db done")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
