"""Durable generation identities and retirement fences in Controller-owned state."""

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
    """A generation belongs to another identity or is malformed."""


def _generation_of(container: Mapping[str, Any]) -> str | None:
    """Return the generation label of one visible container, if valid."""
    labels: Mapping[str, Any] = container.get("Labels") or {}
    generation = labels.get("skycloud.generation")
    if not isinstance(generation, str) or not re.fullmatch(
        GENERATION_PATTERN, generation,
    ):
        return None
    return generation


class GenerationJournal:
    """Durable intent, retirement and identity records for runtime generations."""

    def __init__(self, directory: str | Path) -> None:
        """Bind the journal to the Controller-owned state directory."""
        self.directory = Path(directory)

    def connect(self) -> sqlite3.Connection:
        """Open the journal database with its fixed durability settings."""
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
        """Return retired identities found by scanning visible containers."""
        # Scan visible resources, not unbounded retirement history. A persisted
        # cursor advances even over protected/mismatched identities.
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
        """Read retired records around the persisted cursor and advance it."""
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
        """Hold the generation shard lock and record the create intent."""
        if not re.fullmatch(GENERATION_PATTERN, generation):
            message = "Invalid runtime generation"
            raise JournalIdentityError(message)
        self.directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        # Bounded lock files; shard collisions only serialize unrelated IDs.
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
                db.commit()  # Intent precedes every Docker create, including a crash.
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
    """One locked generation row that owns its journal connection."""

    def __init__(self, db: sqlite3.Connection, generation: str) -> None:
        """Bind the record to its locked generation."""
        self.db, self.generation = db, generation

    def read(self) -> sqlite3.Row:
        """Return the journal row; the shard lock guarantees it exists."""
        row = self.db.execute(
            "SELECT * FROM generations WHERE generation=?", (self.generation,),
        ).fetchone()
        # locked() inserts this row before yielding, and journal rows are never
        # deleted. sqlite3.Row is the configured connection row factory.
        return cast("sqlite3.Row", row)

    def ready(self, container_id: str) -> None:
        """Record the created container identity for this generation."""
        self.db.execute(
            "UPDATE generations SET state='ready',container_id=? WHERE generation=?",
            (container_id, self.generation),
        )
        self.db.commit()

    def retire(self) -> None:
        """Commit the retirement fence before any container removal."""
        # Commit the fence before removal. Failure must leave it retired so a
        # delayed prepare can never recreate a deleted resource.
        self.db.execute(
            "UPDATE generations SET state='retired' WHERE generation=?",
            (self.generation,),
        )
        self.db.commit()
