"""Object storage calls must be bounded: no unbounded connect/read and no silent retry storm."""
import urllib3

from app.infra import storage

SECRET = "explicit-storage-secret-value-32-chars"


def build(monkeypatch, **overrides):
    class FakePool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    captured = {}

    def pool(**kwargs):
        captured['pool'] = FakePool(**kwargs)
        return captured['pool']

    class Client:
        def __init__(self, endpoint, **kwargs):
            captured.update(kwargs)

        def bucket_exists(self, bucket):
            return True

    monkeypatch.setattr(storage.urllib3, 'PoolManager', pool)
    monkeypatch.setattr(storage, 'Minio', Client)
    for key, value in overrides.items():
        monkeypatch.setattr(storage, key, value)
    storage.MinIOStorageClient(endpoint='minio:9000', access_key='storage-user', secret_key=SECRET, bucket='bucket')
    return captured


def test_minio_client_bounds_connect_read_and_retries(monkeypatch):
    captured = build(monkeypatch)
    timeout = captured['pool'].kwargs['timeout']
    assert isinstance(timeout, urllib3.Timeout)
    assert timeout.connect_timeout == storage.MINIO_CONNECT_TIMEOUT > 0
    assert timeout.read_timeout == storage.MINIO_READ_TIMEOUT > 0
    retries = captured['pool'].kwargs['retries']
    assert isinstance(retries, urllib3.Retry)
    assert retries.total == storage.MINIO_RETRIES
    assert retries.status_forcelist == [500, 502, 503, 504]
    assert captured['secure'] is False


def test_minio_bounds_follow_configuration(monkeypatch):
    captured = build(monkeypatch, MINIO_CONNECT_TIMEOUT=1.5, MINIO_READ_TIMEOUT=7.0, MINIO_RETRIES=0)
    timeout = captured['pool'].kwargs['timeout']
    assert (timeout.connect_timeout, timeout.read_timeout) == (1.5, 7.0)
    assert captured['pool'].kwargs['retries'].total == 0
