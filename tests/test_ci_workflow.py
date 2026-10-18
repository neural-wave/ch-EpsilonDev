"""Protect the workflow's release gates without adding a YAML runtime dependency."""
import re
import subprocess
from pathlib import Path

import pytest


WORKFLOW = (Path(__file__).resolve().parents[1] / '.github/workflows/tests.yml').read_text()


def job(name):
    match = re.search(rf'^  {name}:\n(.*?)(?=^  \w+:\n|\Z)', WORKFLOW, re.M | re.S)
    assert match, f'Missing CI job: {name}'
    return match.group(1)


def test_required_gate_includes_tests_and_container():
    assert 'needs: [tests, container]' in job('ci')
    assert "if: ${{ always() }}" in job('ci')
    assert 'test "$TEST_RESULT" = success' in job('ci')
    assert 'test "$CONTAINER_RESULT" = success' in job('ci')


def test_dependencies_are_installed_after_python_setup():
    steps = job('tests')
    assert steps.index('actions/setup-python@') < steps.index('python -m pip install')
    assert "python-version: '3.12'" in steps
    assert 'python -m pytest -q' in steps
    assert 'node --test tests/frontend/*.test.cjs' in steps
    assert 'python scripts/ci/check_release_files.py' in steps


def test_publication_uses_the_smoke_tested_image_on_main_pushes_only():
    container = job('container')
    assert 'needs: tests' in container
    assert "if: ${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}" in container
    assert container.index('docker build ') < container.index('smoke_container.py') < container.index('docker push ')
    assert 'docker tag nl2sparql:ci' in container
    assert '--platform linux/amd64' in container
    assert 'setup-qemu' not in WORKFLOW
    assert 'packages: write' not in job('tests')


def test_actions_are_pinned_and_checkout_does_not_persist_credentials():
    actions = re.findall(r'uses: ([^\s]+)', WORKFLOW)
    assert actions
    assert all(re.fullmatch(r'[^@]+@[0-9a-f]{40}', action) for action in actions)
    for step in WORKFLOW.split('      - '):
        if step.startswith('uses: actions/checkout@'):
            assert 'persist-credentials: false' in step


@pytest.mark.parametrize('script', ['infra/virtuoso/dev.sh', 'infra/virtuoso/perf.sh', 'scripts/runbox.sh'])
def test_operations_scripts_parse_without_executing_services(script):
    path = Path(__file__).resolve().parents[1] / script
    subprocess.run(['bash', '-n', str(path)], check=True, capture_output=True, text=True)
