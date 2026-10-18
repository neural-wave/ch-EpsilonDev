"""Release boundaries and offline application checks used by the container workflow."""
from contextlib import contextmanager
import subprocess

import pytest
from fastapi.testclient import TestClient

from nl2sparql.api import create_app
from nl2sparql.config import Settings
from scripts.ci.check_release_files import restricted
from scripts.ci import smoke_container


@pytest.mark.parametrize('path', [
    '.env', '.env.production', 'nested/.env.local', 'assets/terminology_index.jsonl',
    'patients/example.csv', 'ontologies/schema.ttl', 'logs/shared_results.sqlite3',
    'eval/reports/private.json', 'tests/browser/artifacts/trace.zip', '.venv311/bin/python',
    'node_modules/package/index.js', 'private.key', 'dist/result.whl',
])
def test_release_boundary_rejects_private_or_generated_paths(path):
    assert restricted(path)


@pytest.mark.parametrize('path', [
    '.env.example', 'frontend/cli/assets/sib-emblem.png', 'frontend/cli/assets/sphn-logo.png',
    'frontend/js/result.js', 'data/schema_cards.json', 'docs/FRONTEND.md', '.github/workflows/tests.yml',
])
def test_release_boundary_allows_public_source_and_approved_logos(path):
    assert not restricted(path)


def test_release_boundary_allows_all_currently_tracked_files():
    tracked_paths = subprocess.check_output(['git', 'ls-files', '-z'], text=True).split('\0')
    assert not [path for path in tracked_paths if path and restricted(path)]


def test_container_application_checks_work_without_model_database_or_assets(monkeypatch, tmp_path):
    calls = []

    @contextmanager
    def running(image, platform, public):
        assert platform == 'linux/amd64'
        settings = Settings(backend='stub', log_path=None, assets_dir=tmp_path/'absent-assets',
                            demo_public_mode=public, demo_min_group_size=5 if public else None)
        with TestClient(create_app(settings=settings)) as client:
            yield client, 'synthetic-container'

    def request(client, path, body=None):
        response = client.get(path) if body is None else client.post(path, json=body)
        return response.status_code, response.headers, response.content

    def check_output(command, **kwargs):
        assert command[:3] == ['docker', 'image', 'inspect']
        return '10001:10001\n'

    def run(command, **kwargs):
        calls.append(command)
        assert command[:4] == ['docker', 'exec', 'synthetic-container', 'python']

    async def forbidden_execution(*args, **kwargs):
        raise AssertionError('Container smoke checks must never execute a database query.')

    monkeypatch.setattr(smoke_container, 'running', running)
    monkeypatch.setattr(smoke_container, 'request', request)
    monkeypatch.setattr(smoke_container.subprocess, 'check_output', check_output)
    monkeypatch.setattr(smoke_container.subprocess, 'run', run)
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select', forbidden_execution)
    smoke_container.smoke('synthetic-image', 'linux/amd64')
    assert len(calls) == 2


def test_container_check_rejects_a_root_image(monkeypatch):
    monkeypatch.setattr(smoke_container.subprocess, 'check_output', lambda *args, **kwargs: '0:0\n')
    with pytest.raises(AssertionError, match='root'):
        smoke_container.smoke('synthetic-image', 'linux/amd64')
