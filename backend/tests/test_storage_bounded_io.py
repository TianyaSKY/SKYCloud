"""对象存储调用必须有界: 无无界连接/读取且无静默重试风暴测试."""
from typing import Any

import pytest
import urllib3

from app.infra import storage

SECRET = "explicit-storage-secret-value-32-chars"


def build(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
    class FakePool:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs: dict[str, Any] = kwargs

    captured: dict[str, Any] = {}

    def pool(**kwargs: Any) -> FakePool:
        captured['pool'] = FakePool(**kwargs)
        return captured['pool']

    class Client:
        def __init__(self, endpoint: str, **kwargs: Any) -> None:
            captured.update(kwargs)

        def bucket_exists(self, bucket: str) -> bool:
            return True

    monkeypatch.setattr(storage.urllib3, 'PoolManager', pool)
    monkeypatch.setattr(storage, 'Minio', Client)
    for key, value in overrides.items():
        monkeypatch.setattr(storage, key, value)
    storage.MinIOStorageClient(endpoint='minio:9000', access_key='storage-user', secret_key=SECRET, bucket='bucket')
    return captured


def test_minio_client_bounds_connect_read_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = build(monkeypatch)
    pool_obj: Any = captured['pool']
    timeout: Any = pool_obj.kwargs['timeout']
    assert isinstance(timeout, urllib3.Timeout)
    assert timeout.connect_timeout == storage.MINIO_CONNECT_TIMEOUT > 0
    assert timeout.read_timeout == storage.MINIO_READ_TIMEOUT > 0
    retries: Any = pool_obj.kwargs['retries']
    assert isinstance(retries, urllib3.Retry)
    assert retries.total == storage.MINIO_RETRIES
    assert retries.status_forcelist == [500, 502, 503, 504]
    assert captured['secure'] is False


def test_minio_bounds_follow_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = build(monkeypatch, MINIO_CONNECT_TIMEOUT=1.5, MINIO_READ_TIMEOUT=7.0, MINIO_RETRIES=0)
    pool_obj: Any = captured['pool']
    timeout: Any = pool_obj.kwargs['timeout']
    assert (timeout.connect_timeout, timeout.read_timeout) == (1.5, 7.0)
    retries: Any = pool_obj.kwargs['retries']
    assert retries.total == 0

