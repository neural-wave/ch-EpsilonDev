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


def test_required_gate_includes_every_validation_job():
    checks = {'repository', 'pytest', 'frontend', 'browser', 'container'}
    needs = re.search(r'^    needs: \[(.*?)\]', job('ci'), re.M)
    assert needs
    assert set(needs.group(1).replace(' ', '').split(',')) == checks
    assert "if: ${{ always() }}" in job('ci')
    assert "item['result'] != 'success'" in job('ci')


def test_python_matrix_uses_setup_python_before_dependency_installation():
    steps = job('pytest').split('      - ')
    checkout = next(step for step in steps if step.startswith('uses: actions/checkout@'))
    setup = next(step for step in steps if step.startswith('uses: actions/setup-python@'))
    install = next(step for step in steps if step.startswith('run: python -m pip install'))
    assert 'python-version:' not in checkout
    assert 'python-version: ${{ matrix.python }}' in setup
    assert steps.index(checkout) < steps.index(setup) < steps.index(install)


def test_publication_promotes_tested_images_only_after_all_checks():
    publish = job('publish')
    assert 'needs: [ci, container]' in publish
    assert "github.event_name != 'pull_request'" in publish
    assert "github.ref == 'refs/heads/main'" in publish
    assert "startsWith(github.ref, 'refs/tags/v')" in publish
    assert "github.event_name != 'workflow_dispatch' || inputs.publish" in publish
    assert 'docker load --input' in publish
    assert 'docker/build-push-action@' not in publish
    assert 'packages: write' not in WORKFLOW.split('  publish:\n')[0]
    assert 'push: false' in job('container')
    assert job('container').index('smoke_container.py') < job('container').index('docker save')


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
