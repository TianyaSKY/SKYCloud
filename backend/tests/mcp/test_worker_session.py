"""清理故障必须保留业务结果且不记录 SQL 秘密信息."""

from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest
from loguru import logger
from sqlalchemy.exc import StatementError

from app.mcp import server
from app.mcp.unit_of_work import worker_session


@pytest.mark.parametrize("business_failure", [False, True])
@pytest.mark.parametrize("invalidation_failure", [False, True])
async def test_cleanup_fault_preserves_outcome(
    monkeypatch: pytest.MonkeyPatch,
    business_failure: bool,
    invalidation_failure: bool,
) -> None:
    private = "private-sql-bind-sentinel"
    session = MagicMock()
    session.close.side_effect = StatementError(
        private,
        "SELECT :password",
        {"password": private},
        ValueError(private),
    )
    if invalidation_failure:
        session.invalidate.side_effect = ValueError(private)
    monkeypatch.setattr(server, "SessionLocal", lambda: session)
    records: list[dict[str, Any]] = []

    def log_sink(message: Any) -> None:
        records.append(dict(message.record))

    sink = logger.add(log_sink)
    original = RuntimeError("business error")

    def operation(db: Any) -> dict[str, bool]:
        assert db is session
        if business_failure:
            raise original
        return {"committed": True}

    try:
        if business_failure:
            with pytest.raises(RuntimeError) as caught:
                await server._run_sync(operation)  # pyright: ignore[reportPrivateUsage]
            assert caught.value is original
        else:
            result = await server._run_sync(operation)  # pyright: ignore[reportPrivateUsage]
            assert result == {"committed": True}
    finally:
        logger.remove(sink)
    session.close.assert_called_once_with()
    session.invalidate.assert_called_once_with()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    assert len(records) == (2 if invalidation_failure else 1)
    assert all(
        private not in str(record["message"]) and record["exception"] is None
        for record in records
    )


def test_normal_cleanup_retains_caller_transaction_control() -> None:
    session = MagicMock()
    with worker_session(lambda: session) as db:
        assert db is session
    session.close.assert_called_once_with()
    session.invalidate.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_construction_failure_preserves_original_exception() -> None:
    original = RuntimeError("factory failure")

    def factory() -> NoReturn:
        raise original

    with pytest.raises(RuntimeError) as caught:
        with worker_session(factory):
            pytest.fail("No session was created")
    assert caught.value is original
