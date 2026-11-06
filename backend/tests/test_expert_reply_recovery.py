"""恢复机制必须将已完成输出绑定至新提交的用户消息测试."""
from typing import Any
import pytest
from app.features.assistant.engines.expert_engine import _current_reply  # pyright: ignore[reportPrivateUsage]


def user(identity: str = 'new-user', query: str = 'question') -> dict[str, Any]:
    return {'info': {'id': identity, 'role': 'user'}, 'parts': [{'type': 'text', 'text': query}]}


def answer(identity: str | None = 'new-answer', parent: str = 'new-user', text: str = 'current', **changes: Any) -> dict[str, Any]:
    info: dict[str, Any] = {'id': identity, 'role': 'assistant', 'parentID': parent, 'finish': 'stop', 'time': {'completed': 123}, 'tokens': {'output': 7}}
    info.update(changes)
    return {'info': info, 'parts': [{'type': 'text', 'text': text}]}


def test_repeated_query_does_not_recover_prior_round_or_its_usage() -> None:
    prior: list[Any] = [user('old-user'), answer('old-answer', 'old-user', 'old')]
    ids: set[str] = {'old-user', 'old-answer'}
    assert _current_reply(prior, ids, 'question') == ('', None, {}, None)
    assert _current_reply(prior + [user(), answer()], ids, 'question') == ('current', 'new-answer', {'output': 7}, None)


@pytest.mark.parametrize('changes', [{'id': None}, {'parentID': 'old-user'}, {'parentID': 'foreign'}, {'time': {}}, {'finish': 'tool-calls'}, {'finish': None}, {'error': {'message': 'failed'}}])
def test_incomplete_failed_or_unrelated_answer_is_not_success(changes: dict[str, Any]) -> None:
    messages: list[Any] = [user(), answer(**changes)]
    assert _current_reply(messages, set(), 'question') == ('', None, {}, None)


def test_different_query_and_malformed_user_parts_do_not_bind() -> None:
    messages: list[Any] = [user(query='other'), answer()]
    assert _current_reply(messages, set(), 'question') == ('', None, {}, None)
    malformed: dict[str, Any] = user()
    malformed['parts'] = None
    assert _current_reply([malformed, answer()], set(), 'question') == ('', None, {}, None)
