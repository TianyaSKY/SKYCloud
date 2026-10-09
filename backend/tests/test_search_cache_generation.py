import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from app.infra import cache
from app.features.file import service, search_service


def test_completed_invalidation_cannot_be_undone_by_an_older_search(monkeypatch):
    values = {}
    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    class Store:
        def get(self, key):
            with lock:
                return values.get(key)
        def set(self, key, value, nx=False):
            with lock:
                if nx and key in values:
                    return False
                values[key] = value
                return True
        def setex(self, key, ttl, value):
            return self.set(key, value)
        def scan_iter(self, match, **kwargs):
            with lock:
                keys = [key for key in values if key.startswith(match[:-1])]
            yield from keys
        def delete(self, *keys):
            with lock:
                return sum(values.pop(key, None) is not None for key in keys)
    monkeypatch.setattr(cache, 'redis_client', Store())
    source = ['old-name']
    reads = []
    def query(*args):
        snapshot = source[0]
        reads.append(snapshot)
        if len(reads) == 1:
            entered.set()
            assert release.wait(3)
        return SimpleNamespace(items=[SimpleNamespace(to_dict=lambda: {'name': snapshot})], total=1)
    monkeypatch.setattr(search_service.repository, 'search_names', query)
    monkeypatch.setattr(search_service, '_search_revision', lambda *args: 0)
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(search_service._search_files_fuzzy, None, 1, 'name', 1, 10)
        try:
            assert entered.wait(1)
            source[0] = 'new-name'
            service._clear_search_cache(1)
        finally:
            release.set()
        assert old.result()['items'][0]['name'] == 'old-name'
    result = search_service._search_files_fuzzy(None, 1, 'name', 1, 10)
    assert result['items'][0]['name'] == 'new-name'
    assert reads == ['old-name', 'new-name']


def test_missing_generation_always_chooses_a_new_namespace(monkeypatch):
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get.return_value = None
    store.set.return_value = True
    monkeypatch.setattr(cache, 'redis_client', store)
    first = cache._generation_cache_key('cache-key', 'scope')
    second = cache._generation_cache_key('cache-key', 'scope')
    assert first != second
    assert all(call.kwargs == {'nx': True} for call in store.set.call_args_list)


def test_failed_generation_read_bypasses_both_cache_read_and_write(monkeypatch):
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get.side_effect = ConnectionError('private-generation-detail')
    monkeypatch.setattr(cache, 'redis_client', store)
    @cache.cacheable('data', generation_key=lambda: 'scope')
    def query():
        return {'current': True}
    assert query() == {'current': True}
    store.get.assert_called_once_with('scope')
    store.setex.assert_not_called()
