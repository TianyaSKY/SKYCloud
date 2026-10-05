"""Build MinIO from checksum-locked official source and save its local image ID."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def build():
    lock_path = ROOT / 'backend/minio_image/lock.json'
    lock = json.loads(lock_path.read_text())
    architecture = subprocess.check_output(['docker', 'info', '--format', '{{.Architecture}}'], text=True).strip()
    architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(architecture, architecture)
    if architecture not in lock['architectures']:
        raise RuntimeError('Only arm64 and amd64 MinIO builds are supported')
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    with tempfile.TemporaryDirectory(prefix='skycloud-minio-build-') as temporary:
        folder = Path(temporary)
        archive = folder / 'source.tar.gz'
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(lock['source_url'], context=context, timeout=60) as response, archive.open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 64 * 1024 * 1024:
                    raise RuntimeError('MinIO source exceeds the download limit')
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != lock['source_sha256']:
            raise RuntimeError('MinIO source checksum mismatch; build refused')
        source = folder / 'source'
        source.mkdir()
        prefix = 'minio-' + lock['commit'] + '/'
        with tarfile.open(archive) as tar:
            total = 0
            for member in tar:
                if member.isdir():
                    continue
                relative = Path(member.name.removeprefix(prefix))
                if not member.name.startswith(prefix) or not member.isfile() or relative.is_absolute() or '..' in relative.parts:
                    raise RuntimeError('Unexpected MinIO source archive layout')
                total += member.size
                if total > 256 * 1024 * 1024:
                    raise RuntimeError('MinIO extracted source exceeds the size limit')
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as incoming, destination.open('wb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
        shutil.copyfile(ROOT / 'backend/minio_image/Dockerfile', folder / 'Dockerfile')
        shutil.copyfile(lock_path, folder / 'lock.json')
        tag = 'skycloud-minio:' + lock['commit'][:12]
        arguments = {
            'MINIO_BUILDER': lock['builder'], 'MINIO_BASE': lock['base'],
            'MINIO_VERSION': lock['version'], 'MINIO_COMMIT': lock['commit'],
            'MINIO_RELEASE_TAG': 'DEVELOPMENT.' + lock['version'].replace(':', '-'),
            'MINIO_COPYRIGHT_YEAR': lock['version'][:4],
        }
        command = ['docker', 'build', '--tag', tag]
        for name, value in arguments.items():
            command.extend(['--build-arg', name + '=' + value])
        subprocess.run(command + [str(folder)], check=True)
        return subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()


def save_reference(path, reference):
    if path.is_symlink():
        raise RuntimeError('Refusing to replace a symlink configuration')
    content = path.read_text()
    lines = [line for line in content.splitlines() if not line.startswith('MINIO_IMAGE=')]
    lines.append('MINIO_IMAGE=' + reference)
    fd, temporary = tempfile.mkstemp(prefix='.minio-env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(lines) + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Existing private configuration; other credentials are preserved')
    args = parser.parse_args()
    if args.env_file and (args.env_file.is_symlink() or not args.env_file.is_file()):
        parser.error('Configuration must be an existing regular file')
    image = build()
    if args.env_file:
        save_reference(args.env_file, image)
    print('MINIO_IMAGE=' + image)
