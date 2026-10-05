"""Opt-in source-built MinIO persistence and cached legacy-volume compatibility."""
from io import BytesIO
import json
import os
from pathlib import Path
import time
from urllib.request import urlopen
import uuid

from minio import Minio
import pytest

from tests.integration.test_runtime_security import docker

pytestmark = pytest.mark.skipif(os.getenv('RUN_MINIO_IMAGE_TESTS') != '1', reason='Opt-in isolated MinIO image integration')


def start(name, image, volume, access, secret):
    docker('run', '-d', '--pull', 'never', '--name', name, '-e', 'MINIO_ROOT_USER=' + access,
           '-e', 'MINIO_ROOT_PASSWORD=' + secret, '-p', '127.0.0.1::9000',
           '-v', volume + ':/data', image, 'server', '/data')
    port = docker('port', name, '9000/tcp').stdout.strip().rsplit(':', 1)[1]
    endpoint = '127.0.0.1:' + port
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen('http://' + endpoint + '/minio/health/cluster', timeout=1) as response:
                if response.status == 200:
                    return Minio(endpoint, access_key=access, secret_key=secret, secure=False)
        except Exception:
            time.sleep(.1)
    raise AssertionError('Isolated MinIO did not start: ' + docker('logs', name, check=False).stderr)


def read(client, bucket, key):
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


@pytest.mark.parametrize('legacy', [False, True], ids=['source-restart', 'legacy-volume-upgrade'])
def test_storage_image_preserves_objects_across_restart_and_upgrade(legacy):
    image = os.getenv('SKYCLOUD_TEST_MINIO_IMAGE')
    assert image and image.startswith('sha256:'), 'Set SKYCLOUD_TEST_MINIO_IMAGE to the built immutable local image ID'
    initial_image = image
    if legacy:
        initial_image = os.getenv('SKYCLOUD_TEST_LEGACY_MINIO_IMAGE')
        if not initial_image:
            pytest.skip('Set SKYCLOUD_TEST_LEGACY_MINIO_IMAGE to a cached immutable legacy image ID')
        assert initial_image.startswith('sha256:')
        docker('image', 'inspect', initial_image)
    name = 'skycloud-minio-image-test-' + uuid.uuid4().hex[:12]
    volume = name + '-data'
    access, secret = uuid.uuid4().hex, uuid.uuid4().hex + uuid.uuid4().hex
    bucket, body = 'persistence-test', b'preserve-existing-object\x00\xff'
    try:
        docker('volume', 'create', volume)
        client = start(name, initial_image, volume, access, secret)
        client.make_bucket(bucket)
        client.put_object(bucket, 'original', BytesIO(body), len(body))
        docker('stop', name)
        docker('rm', name)
        client = start(name, image, volume, access, secret)
        assert read(client, bucket, 'original') == body
        client.put_object(bucket, 'new', BytesIO(b'new object'), 10)
        lock_path = Path(__file__).resolve().parents[2] / 'minio_image/lock.json'
        lock = json.loads(lock_path.read_text())
        version = docker('exec', name, 'minio', '--version').stdout
        assert 'DEVELOPMENT.' + lock['version'].replace(':', '-') in version
        assert lock['commit'] in version
        assert json.loads(docker('exec', name, 'cat', '/usr/share/minio/lock.json').stdout) == lock
        docker('exec', name, 'python', '-c',
               "import hashlib,json; p='/usr/share/minio/'; lock=json.load(open(p+'lock.json')); "
               "assert hashlib.file_digest(open(p+'source.tar.gz','rb'),'sha256').hexdigest()==lock['source_sha256']")
        docker('exec', name, 'python', '-c',
               "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:9000/minio/health/cluster',timeout=2).status==200")
        docker('stop', name)
        docker('rm', name)
        client = start(name, image, volume, access, secret)
        assert read(client, bucket, 'original') == body
        assert read(client, bucket, 'new') == b'new object'
        client.remove_object(bucket, 'new')
        assert [item.object_name for item in client.list_objects(bucket)] == ['original']
    finally:
        docker('rm', '-f', name, check=False)
        docker('volume', 'rm', volume, check=False)
