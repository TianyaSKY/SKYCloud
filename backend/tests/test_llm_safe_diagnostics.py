"""故障日志不得序列化提供商响应、异常文本或 SQL 绑定值测试."""

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from loguru import logger

PRIVATE: str = "private-prompt-provider-key-sentinel"


@pytest.fixture
def logs() -> Generator[list[tuple[str, Any]], None, None]:
    records: list[tuple[str, Any]] = []

    def log_sink(message: Any) -> None:
        records.append((str(message), message.record["exception"]))

    sink = logger.add(log_sink, format="{message}", level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink)


def assert_safe(logs: list[tuple[str, Any]], diagnostic: str) -> None:
    rendered = "".join(row[0] for row in logs)
    assert diagnostic in rendered
    assert PRIVATE not in rendered
    assert all(exception is None for _, exception in logs)


def test_accounting_sql_exception_cannot_log_bound_prompt(
    monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    from sqlalchemy.exc import StatementError
    from app.features.token_usage import service

    session = Mock()
    session.execute.side_effect = StatementError(
        PRIVATE,
        "INSERT INTO usage VALUES (:prompt)",
        {"prompt": PRIVATE},
        ValueError(PRIVATE),
    )
    monkeypatch.setattr(service, "SessionLocal", lambda: session)
    service.record_usage(
        service.RecordUsageParams(
            user_id=1,
            action="chat",
            query_summary=PRIVATE,
            extra_info=PRIVATE,
        )
    )
    session.rollback.assert_called_once()
    session.close.assert_called_once()
    assert_safe(logs, "error_type=StatementError")


def test_best_effort_accounting_does_not_log_action_or_exception(
    monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    from app.features.token_usage import service
    from app.infra.llm.usage import TokenUsage, safe_record

    monkeypatch.setattr(
        service, "record_usage", Mock(side_effect=RuntimeError(PRIVATE))
    )
    safe_record(
        TokenUsage(
            user_id=1,
            action=PRIVATE,
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=3,
            query_summary=PRIVATE,
        )
    )
    assert_safe(logs, "error_type=RuntimeError")


def test_shutdown_failure_still_closes_remaining_clients_without_private_detail(
    monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    from app.infra.llm import sync_client

    failed = Mock()
    failed.close.side_effect = RuntimeError(PRIVATE)
    other = Mock()
    monkeypatch.setattr(
        sync_client,
        "_client_cache",
        {("api", "private"): failed, ("api", "other"): other},
    )
    sync_client.close_sync_clients()
    failed.close.assert_called_once()
    other.close.assert_called_once()
    assert sync_client._client_cache == {}  # pyright: ignore[reportPrivateUsage]
    assert_safe(logs, "error_type=RuntimeError")


async def test_rerank_failure_returns_original_docs_and_keeps_exception_private(
    monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    from app.features.chat import rerank

    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, text=PRIVATE)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        monkeypatch.setattr(rerank, "_get_rerank_client", lambda: client)
        monkeypatch.setattr(
            rerank,
            "get_rerank_model_config",
            lambda: {"api": "http://rerank.test", "key": PRIVATE, "model": "model"},
        )
        monkeypatch.setattr(rerank, "get_rerank_top_k", lambda: 2)
        docs: list[Any] = [MagicMock(page_content=PRIVATE), MagicMock(page_content="other")]
        assert await rerank.rerank_documents(PRIVATE, docs) is docs
    assert_safe(logs, "error_type=HTTPStatusError")


async def test_chat_stream_failure_keeps_prompt_out_of_logs_and_error_event(
    monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    from app.features.chat import service
    from app.infra.llm import client

    chain = MagicMock()
    chain.__or__.return_value = chain
    chain.__ror__.return_value = chain
    chain.with_config.return_value = chain
    chain.with_structured_output.return_value = chain

    async def events(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(PRIVATE)
        yield  # pyright: ignore[reportUnreachable]

    chain.astream_events = events

    def make_chain(*_a: object, **_kw: object) -> MagicMock:
        return chain

    monkeypatch.setattr(service, "get_chat_model", lambda: chain)
    monkeypatch.setattr(service.ChatPromptTemplate, "from_template", make_chain)
    for name in ("RunnableParallel", "RunnableLambda", "StrOutputParser"):
        monkeypatch.setattr(service, name, make_chain)
    recorder = Mock()
    monkeypatch.setattr(client, "record_llm_usage", recorder)
    event_list = [
        event async for event in service.generate_chat_events(1, 2, PRIVATE, [])
    ]
    assert len(event_list) == 1 and event_list[0].type == "error"
    assert (
        "对话请求失败" in event_list[0].payload["message"]
        and PRIVATE not in str(event_list[0].payload)
    )
    recorder.assert_called_once()
    assert_safe(logs, "error_type=RuntimeError")
