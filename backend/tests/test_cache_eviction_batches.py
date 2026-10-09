from app.infra import cache
from app.features.file import service


def test_pattern_eviction_deletes_before_consuming_whole_scan(monkeypatch):
    batches = []
    class Store:
        def scan_iter(self, **kwargs):
            assert kwargs['match'] == 'bounded:*'
            for index in range(1001):
                if index == 128:
                    assert batches and len(batches[0]) == 128
                yield f'bounded:{index}'
        def delete(self, *keys):
            assert len(keys) <= 128
            batches.append(keys)
            return len(keys)
    monkeypatch.setattr(cache, 'redis_client', Store())
    assert cache.evict_cache_pattern('bounded') == 1001
    assert len(batches) == 8 and sum(map(len, batches)) == 1001


def test_failed_later_batch_preserves_completed_eviction_count(monkeypatch):
    batches = []
    class Store:
        def scan_iter(self, **kwargs):
            yield from (f'partial:{index}' for index in range(300))
        def delete(self, *keys):
            if batches:
                raise ConnectionError('private-cache-detail')
            batches.append(keys)
            return len(keys)
    monkeypatch.setattr(cache, 'redis_client', Store())
    assert cache.evict_cache_pattern('partial') == 128
    assert len(batches) == 1


def test_file_search_cache_eviction_is_workspace_scoped(monkeypatch):
    calls = []
    monkeypatch.setattr(service, 'evict_cache_pattern', calls.append)
    service._clear_search_cache(1)
    service._clear_search_cache(12)
    service._clear_search_cache(None)
    assert calls == ['search:fuzzy:1', 'search:fuzzy:12']
