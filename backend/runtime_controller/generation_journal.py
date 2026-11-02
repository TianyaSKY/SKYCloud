"""Controller 持有状态中的持久化世代标识与退役栅栏."""

import fcntl
import os
import re
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, cast

GENERATION_PATTERN: Final = r"[0-9a-f]{32}"
CONTAINER_ID_PATTERN: Final = r"[0-9a-f]{64}"
DATABASE_NAME: Final = "generations.sqlite"
DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600
CONNECT_TIMEOUT_SECONDS: Final = 30
DEFAULT_SCAN_LIMIT: Final = 20
LOCK_SHARD_LENGTH: Final = 2
IDENTITY_COLUMNS: Final = ("workspace_id", "user_id", "runtime_id")


class JournalIdentityError(ValueError):
    """世代属于其他标识或格式畸形异常."""


def _generation_of(container: Mapping[str, Any]) -> str | None:
    """返回单个可见容器的世代标签, 若无效则返回 None."""
    labels: Mapping[str, Any] = container.get("Labels") or {}
    generation = labels.get("skycloud.generation")
    if not isinstance(generation, str) or not re.fullmatch(
        GENERATION_PATTERN, generation,
    ):
        return None
    return generation


class GenerationJournal:
    """运行时世代的持久化意图、退役状态与标识记录."""

    def __init__(self, directory: str | Path) -> None:
        """将日志绑定至 Controller 持有的状态目录."""
        self.directory = Path(directory)

    def connect(self) -> sqlite3.Connection:
        """以固定的持久化配置打开日志数据库."""
        self.directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        path = self.directory / DATABASE_NAME
        db = sqlite3.connect(path, timeout=CONNECT_TIMEOUT_SECONDS)
        try:
            path.chmod(FILE_MODE)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS generations (
                generation TEXT PRIMARY KEY, workspace_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, runtime_id INTEGER NOT NULL,
                state TEXT NOT NULL, container_id TEXT)""",
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reconciliation_cursor ("
                "id INTEGER PRIMARY KEY CHECK(id=1), container_id TEXT NOT NULL)",
            )
        except BaseException:
            db.close()
            raise
        return db

    def retired_candidates(
        self,
        containers: Sequence[Mapping[str, Any]],
        limit: int = DEFAULT_SCAN_LIMIT,
    ) -> list[tuple[int, int, int, str]]:
        """扫描可见容器返回已退役的实例标识列表."""
        # 扫描可见资源, 而非无界的退役历史。持久化游标
        # 即使跨越受保护/不匹配标识也会向前推进。
        rows = sorted(
            (
                row
                for row in containers
                if re.fullmatch(CONTAINER_ID_PATTERN, row.get("Id", ""))
            ),
            key=lambda row: row["Id"],
        )
        db = self.connect()
        try:
            retired = self._scan_retired(db, rows, limit)
        finally:
            db.close()
        return retired

    def _scan_retired(
        self,
        db: sqlite3.Connection,
        rows: list[Mapping[str, Any]],
        limit: int,
    ) -> list[tuple[int, int, int, str]]:
        """围绕持久化游标读取已退役记录并推进游标."""
        cursor = db.execute(
            "SELECT container_id FROM reconciliation_cursor WHERE id=1",
        ).fetchone()
        position = cursor[0] if cursor else ""
        candidates = (
            [row for row in rows if row["Id"] > position]
            + [row for row in rows if row["Id"] <= position]
        )[:limit]
        retired: list[tuple[int, int, int, str]] = []
        for container in candidates:
            generation = _generation_of(container)
            if generation is None:
                continue
            record = db.execute(
                "SELECT * FROM generations WHERE generation=? AND state='retired'",
                (generation,),
            ).fetchone()
            if record is not None:
                retired.append(
                    (
                        record["workspace_id"],
                        record["user_id"],
                        record["runtime_id"],
                        record["generation"],
                    ),
                )
        if candidates:
            db.execute(
                "INSERT INTO reconciliation_cursor VALUES (1,?) "
                "ON CONFLICT(id) DO UPDATE SET container_id=excluded.container_id",
                (candidates[-1]["Id"],),
            )
            db.commit()
        return retired

    @contextmanager
    def locked(
        self,
        generation: str,
        identity: tuple[int, int, int],
        *,
        nonblocking: bool = False,
    ) -> Generator["GenerationRecord", None, None]:
        """持有世代分片锁并记录创建意图."""
        if not re.fullmatch(GENERATION_PATTERN, generation):
            message = "Invalid runtime generation"
            raise JournalIdentityError(message)
        self.directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        # 有界锁文件; 分片碰撞仅会串行化无关的 ID。
        lock_name = "generation-" + generation[:LOCK_SHARD_LENGTH] + ".lock"
        fd = os.open(
            self.directory / lock_name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            FILE_MODE,
        )
        db = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            db = self.connect()
            row = db.execute(
                "SELECT * FROM generations WHERE generation=?", (generation,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO generations VALUES (?,?,?,?,'preparing',NULL)",
                    (generation, *identity),
                )
                db.commit()  # 意图优先于任何 Docker 创建操作, 包括崩溃情况。
            elif tuple(row[key] for key in IDENTITY_COLUMNS) != identity:
                message = "Generation belongs to another identity"
                raise JournalIdentityError(message)
            yield GenerationRecord(db, generation)
        finally:
            if db is not None:
                db.close()
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class GenerationRecord:
    """持有其日志数据库连接的单个已锁定世代记录."""

    def __init__(self, db: sqlite3.Connection, generation: str) -> None:
        """将记录绑定至其锁定的世代标识."""
        self.db, self.generation = db, generation

    def read(self) -> sqlite3.Row:
        """返回日志数据库行; 分片锁保证该行必定存在."""
        row = self.db.execute(
            "SELECT * FROM generations WHERE generation=?", (self.generation,),
        ).fetchone()
        # locked() 在 yield 前插入此行, 且日志行从不删除。
        # sqlite3.Row 是配置的连接行工厂。
        return cast("sqlite3.Row", row)

    def ready(self, container_id: str) -> None:
        """为该世代记录已创建的容器标识."""
        self.db.execute(
            "UPDATE generations SET state='ready',container_id=? WHERE generation=?",
            (container_id, self.generation),
        )
        self.db.commit()

    def retire(self) -> None:
        """在移除任何容器前提交退役栅栏."""
        # 在移除前提交栅栏。即使失败也须保持退役状态,
        # 以防延迟的 prepare 重建已删除资源。
        self.db.execute(
            "UPDATE generations SET state='retired' WHERE generation=?",
            (self.generation,),
        )
        self.db.commit()
