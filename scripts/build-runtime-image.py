"""构建经过校验和锁定的 glibc 运行时环境; 可选保存其不可变本地镜像 ID."""
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
    lock = json.loads((ROOT / 'backend/runtime_image/lock.json').read_text())
    architecture = subprocess.check_output(['docker', 'info', '--format', '{{.Architecture}}'], text=True).strip()
    architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(architecture, architecture)
    if architecture not in lock['artifacts']:
        raise RuntimeError('Only arm64 and amd64 runtime builds are supported')
    artifact = lock['artifacts'][architecture]
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    with tempfile.TemporaryDirectory(prefix='skycloud-runtime-build-') as temporary:
        folder = Path(temporary); archive = folder / 'release.tar.gz'
        url = f"https://github.com/anomalyco/opencode/releases/download/v{lock['version']}/{artifact['name']}"
        digest = hashlib.sha256(); size = 0
        with urllib.request.urlopen(url, context=context, timeout=60) as response, archive.open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise RuntimeError('Runtime release archive exceeds the download limit')
                digest.update(chunk); stream.write(chunk)
        if digest.hexdigest() != artifact['sha256']:
            raise RuntimeError('Runtime release checksum mismatch; build refused')
        with tarfile.open(archive) as source:
            binaries = [member for member in source.getmembers() if member.isfile() and Path(member.name).name == 'opencode']
            if len(binaries) != 1 or binaries[0].size > 512 * 1024 * 1024:
                raise RuntimeError('Unexpected runtime release layout')
            with source.extractfile(binaries[0]) as binary, (folder / 'opencode').open('wb') as destination:
                shutil.copyfileobj(binary, destination)
        archive.unlink()
        shutil.copyfile(ROOT / 'backend/runtime_image/Dockerfile', folder / 'Dockerfile')
        tag = 'skycloud-opencode:' + lock['version']
        subprocess.run(['docker', 'build', '--tag', tag,
            '--build-arg', 'RUNTIME_BASE=' + lock['base'],
            '--build-arg', 'DEBIAN_SNAPSHOT=' + lock['debian_snapshot'],
            '--build-arg', 'OPENCODE_VERSION=' + lock['version'], str(folder)], check=True)
        return subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()


def save_reference(path, reference):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Refusing to replace a symlink configuration')
    lines = []
    if path.is_file():
        content = path.read_text(encoding='utf-8')
        lines = [line for line in content.splitlines() if not line.startswith('OPENCODE_IMAGE=')]
    lines.append('OPENCODE_IMAGE=' + reference)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.runtime-env-', dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write('\n'.join(lines) + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=None, help='Target configuration file to update (e.g. .skycloud/generated.env)')
    args = parser.parse_args()
    if args.env_file and (args.env_file.is_symlink() or (args.env_file.exists() and not args.env_file.is_file())):
        parser.error('Configuration must be a regular file')
    image = build()
    target_file = args.env_file
    if target_file is None and (ROOT / '.skycloud/generated.env').is_file():
        target_file = ROOT / '.skycloud/generated.env'
    if target_file:
        save_reference(target_file, image)
    print('OPENCODE_IMAGE=' + image)
