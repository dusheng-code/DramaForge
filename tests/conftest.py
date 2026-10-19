"""职责：pytest 公共夹具：真实 MySQL 测试库、Settings 覆盖、Fake LLM / Fake provider 装配点。
输入：环境变量 DRAMAFORGE_TEST_DB（可选）或 .env 里的 TASK_STATE_DB。
输出：settings / store / assets / artifacts_root / clean_db 等夹具。

副作用：创建并清空测试库（dramaforge_test），在系统临时目录写产物。禁止触碰生产库。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from dramaforge.core.asset_store import AssetStore
from dramaforge.core.bus import EventBus
from dramaforge.core.config import Settings
from dramaforge.core.events import Events
from dramaforge.core.state_store import StateStore
from dramaforge.scripts.init_db import TABLE_ORDER, create_database, create_schema, split_dsn

TEST_DB_ENV = "DRAMAFORGE_TEST_DB"


def _test_dsn(base: Settings) -> str:
    """从生产 DSN 派生测试库 DSN（库名加 _test 后缀）。无副作用。"""
    override = os.getenv(TEST_DB_ENV)
    if override:
        return override
    prefix, db_name = split_dsn(base.task_state_db)
    return f"{prefix}{db_name}_test?charset=utf8mb4"


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """测试用 Settings：独立 runtime 目录 + 独立测试库。无副作用。"""
    base = Settings()
    runtime = tmp_path_factory.mktemp("var")
    return base.model_copy(update={"task_state_db": _test_dsn(base), "runtime_dir": str(runtime)})


@pytest.fixture(scope="session")
def runtime_dir(settings: Settings) -> Path:
    """产物根目录（本会话内唯一）。无副作用。"""
    path = Path(settings.runtime_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _db(settings: Settings):
    """建库建表（会话级），结束后保留表结构便于复跑。副作用：DDL。"""
    await create_database(settings.task_state_db)
    engine = create_async_engine(settings.task_state_db, poolclass=None)
    await create_schema(engine)
    await engine.dispose()
    yield settings.task_state_db
    try:
        engine = create_async_engine(settings.task_state_db)
        for name in reversed(TABLE_ORDER):
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await engine.dispose()
    except Exception:  # noqa: BLE001 —— 清理失败不影响测试结论
        pass


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def store(_db: str) -> StateStore:
    """真实 MySQL StateStore（会话级连接池）。副作用：建连接。"""
    s = StateStore.connect(_db)
    yield s
    await s.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def assets(_db: str) -> AssetStore:
    """真实 MySQL AssetStore。副作用：建连接。"""
    a = AssetStore.connect(_db, max_attempt=3)
    yield a
    await a.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_db(_db: str):
    """每个用例前清空业务表，保证相互独立。副作用：TRUNCATE。"""
    engine = create_async_engine(_db)
    try:
        async with engine.begin() as conn:
            for name in ("stage_state", "asset_state", "resolution", "reference_card", "platform_rank_snapshot"):
                await conn.execute(text(f"TRUNCATE TABLE {name}"))
            await conn.execute(text("DELETE FROM task"))
    finally:
        await engine.dispose()
    yield


@pytest.fixture()
def bus() -> EventBus:
    """进程内事件总线。无副作用。"""
    return EventBus()


@pytest.fixture()
def events(bus: EventBus) -> Events:
    """事件发布器。无副作用。"""
    return Events(bus)


@pytest.fixture()
def task_root(runtime_dir: Path):
    """每个用例独立的产物目录工厂，测试结束删除。副作用：写磁盘。"""
    created: list[Path] = []

    def _make(task_id: str) -> Path:
        path = runtime_dir / task_id
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        created.append(path)
        return path

    yield _make
    for path in created:
        shutil.rmtree(path, ignore_errors=True)
