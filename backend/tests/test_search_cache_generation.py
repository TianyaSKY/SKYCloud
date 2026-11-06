from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.features.file import deletion, search_service
from app.infra import cache


def test_completed_invalidation_cannot_be_undone_by_an_older_search(monkeypatch: pytest.MonkeyPatch) -> None:
    values: dict[str, Any] = {}
    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    class Store:
        def get(self, key: str) -> Any:
            with lock:
                return values.get(key)
        def set(self, key: str, value: Any, nx: bool = False) -> bool:
            with lock:
                if nx and key in values:
                    return False
                values[key] = value
                return True
        def setex(self, key: str, ttl: int, value: Any) -> bool:
            return self.set(key, value)
        def scan_iter(self, match: str, **kwargs: Any) -> Generator[str, None, None]:
            with lock:
                keys = [key for key in values if key.startswith(match[:-1])]
            yield from keys
        def delete(self, *keys: str) -> int:
            with lock:
                return sum(values.pop(key, None) is not None for key in keys)
    monkeypatch.setattr(cache, 'redis_client', Store())
    source = ['old-name']
    reads: list[str] = []
    def query(*args: Any) -> SimpleNamespace:
        snapshot = source[0]
        reads.append(snapshot)
        if len(reads) == 1:
            entered.set()
            assert release.wait(3)
        return SimpleNamespace(items=[SimpleNamespace(to_dict=lambda: {'name': snapshot})], total=1)
    def _ret_zero(*args: object) -> int:
        return 0
    def _ret_true(*args: object, **kwargs: object) -> bool:
        return True
    monkeypatch.setattr(search_service.repository, 'search_names', query)
    monkeypatch.setattr(search_service, '_search_revision', _ret_zero)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(search_service, '_search_cache_allowed', _ret_true)  # pyright: ignore[reportPrivateUsage]
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(search_service._search_files_fuzzy, cast(Any, None), 1, 'name', 1, 10)  # pyright: ignore[reportPrivateUsage]
        try:
            assert entered.wait(1)
            source[0] = 'new-name'
            deletion.clear_search_cache(1)
        finally:
            release.set()
        res = cast(dict[str, Any], old.result())
        assert res['items'][0]['name'] == 'old-name'
    result = search_service._search_files_fuzzy(cast(Any, None), 1, 'name', 1, 10)  # pyright: ignore[reportPrivateUsage]
    assert cast(dict[str, Any], result)['items'][0]['name'] == 'new-name'
    assert reads == ['old-name', 'new-name']


def test_missing_generation_always_chooses_a_new_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get.return_value = None
    store.set.return_value = True
    monkeypatch.setattr(cache, 'redis_client', store)
    first = cache._generation_cache_key('cache-key', 'scope')  # pyright: ignore[reportPrivateUsage]
    second = cache._generation_cache_key('cache-key', 'scope')  # pyright: ignore[reportPrivateUsage]
    assert first != second
    assert all(call.kwargs == {'nx': True} for call in store.set.call_args_list)


def test_failed_generation_read_bypasses_both_cache_read_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get.side_effect = ConnectionError('private-generation-detail')
    monkeypatch.setattr(cache, 'redis_client', store)
    @cache.cacheable(
        cache.CacheableParams(prefix='data'), generation_key=lambda: 'scope',
    )
    def query() -> dict[str, bool]:
        return {'current': True}
    assert query() == {'current': True}
    store.get.assert_called_once_with('scope')
    store.setex.assert_not_called()


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stage', ['before', 'after'])
async def test_cache_policy_skips_shared_effects_when_transaction_becomes_unsafe(monkeypatch: pytest.MonkeyPatch, asynchronous: bool, stage: str) -> None:
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get.return_value = None
    monkeypatch.setattr(cache, 'redis_client', store)
    allowed: list[bool] = [stage != 'before']
    def policy() -> bool:
        return allowed[0]
    if asynchronous:
        @cache.cacheable(
            cache.CacheableParams(prefix='policy'), cache_allowed=policy,
        )
        async def async_query() -> dict[str, bool]:
            allowed[0] = False
            return {'own-transaction': True}
        result = await async_query()
    else:
        @cache.cacheable(
            cache.CacheableParams(prefix='policy'), cache_allowed=policy,
        )
        def sync_query() -> dict[str, bool]:
            allowed[0] = False
            return {'own-transaction': True}
        result = sync_query()
    assert result == {'own-transaction': True}
    if stage == 'before':
        store.get.assert_not_called()
    else:
        store.get.assert_called_once_with('policy')
    store.setex.assert_not_called()
