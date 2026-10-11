"""Regenerate hashed Python 3.11+ dependency locks with uv 0.12.1."""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upgrade', action='store_true', help='Resolve newer allowed versions before auditing/testing')
    args = parser.parse_args()
    version = subprocess.check_output(['uv', '--version'], text=True).split()[1]
    if version != '0.12.1':
        raise RuntimeError('Use uv 0.12.1 so lock generation uses the documented resolver')
    for source, target, constraints in [
        ('backend/requirements.txt', 'backend/requirements.lock', []),
        ('backend/requirements-test.txt', 'backend/requirements-test.lock', ['-c', 'backend/requirements.lock']),
        ('backend/requirements-quality.txt', 'backend/requirements-quality.lock', ['-c', 'backend/requirements.lock']),
        ('backend/runtime_controller/requirements.txt', 'backend/runtime_controller/requirements.lock', []),
    ]:
        command = ['uv', 'pip', 'compile', source, '--python-version', '3.11', '--universal',
                   '--generate-hashes', '--index-url', 'https://pypi.org/simple', '--quiet',
                   '--custom-compile-command', 'python scripts/lock-python.py', '--output-file', target,
                   *constraints]
        if args.upgrade:
            command.append('--upgrade')
        subprocess.run(command, cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
