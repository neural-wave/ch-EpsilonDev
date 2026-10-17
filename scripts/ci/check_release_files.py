"""Reject tracked private/generated paths without reading or printing their contents."""
from __future__ import annotations

from pathlib import PurePosixPath
import subprocess

LOGOS = {'frontend/cli/assets/sib-emblem.png', 'frontend/cli/assets/sphn-logo.png'}


def restricted(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    parts = PurePosixPath(path).parts
    if path == '.env.example' or path in LOGOS:
        return False
    return (
        name == '.env' or name.startswith('.env.')
        or name.endswith(('.ttl', '.zst', '.zip', '.sqlite', '.sqlite3', '.db', '.pem', '.key'))
        or any(part in {'assets', 'patients', 'ontologies', 'logs', 'node_modules', '__pycache__'}
               or part.startswith('.venv') for part in parts)
        or path.startswith(('eval/reports/', 'tests/browser/artifacts/', 'build/', 'dist/'))
    )


def main() -> int:
    paths = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    failures = sum(restricted(path) for path in paths if path)
    if failures:
        print(f'Release blocked: {failures} restricted/generated path(s) are tracked. '
              'Inspect git ls-files locally; do not print private contents into CI logs.')
        return 1
    print('Tracked-file release boundary passed (filename checks, not a content/secrets audit).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
