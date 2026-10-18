"""Smoke-test a built image with stand-in agents only; never contacts model/dev services."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(base, path, body=None):
    payload = None if body is None else json.dumps(body).encode()
    req = Request(base + path, data=payload, headers={'Content-Type': 'application/json'})
    try:
        # Translation can include cold parser/context initialization under QEMU.
        # Allow the API's 75-second request deadline to return a verdict or error.
        timeout = 90 if path == '/nl-to-sparql' else 5
        with urlopen(req, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        return error.code, error.headers, error.read()


@contextmanager
def running(image, platform, public):
    command = ['docker', 'run', '--detach', '--platform', platform,
               '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
               '--publish', '127.0.0.1::8080',
               '--env', 'NL2SPARQL_BACKEND=stub',
               '--env', f'DEMO_PUBLIC_MODE={str(public).lower()}',
               '--env', f'DEMO_MIN_GROUP_SIZE={5 if public else "disabled"}',
               '--env', 'NL2SPARQL_LOG_PATH=/tmp/requests.jsonl',
               '--env', 'DEMO_SHARE_DB_PATH=/tmp/shares.sqlite3', image]
    container = subprocess.check_output(command, text=True).strip()
    try:
        mapping = json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))
        port = mapping[0]['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
        base = 'http://127.0.0.1:' + port
        for _ in range(60):
            try:
                if request(base, '/health')[0] == 200:
                    break
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(1)
        else:
            raise AssertionError('Container did not become healthy.')
        yield base, container
    finally:
        subprocess.run(['docker', 'rm', '--force', container], check=True, stdout=subprocess.DEVNULL)


def smoke(image: str, platform: str):
    user = subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Config.User}}'], text=True).strip()
    assert user and user.split(':')[0] not in {'0', 'root'}, 'Container must not run as root.'
    for public in (False, True):
        with running(image, platform, public) as (base, container):
            status, _, body = request(base, '/health')
            assert status == 200 and json.loads(body) == {'status': 'ok', 'backend': 'stub'}
            status, _, body = request(base, '/demo/config')
            assert status == 200 and json.loads(body) == {'demo_min_group_size': 5 if public else None, 'row_limit': 200, 'policy_version': 1}
            status, headers, body = request(base, '/console/cli/')
            assert status == 200 and b'id="prompt-form"' in body
            assert headers['Cache-Control'] == 'no-store'
            assert request(base, '/console/js/result.js')[0] == 200
            assert request(base, '/console/cli/assets/sib-emblem.png')[0] == 200
            if public:
                for path in ('/agents', '/agents/config', '/openapi.json', '/docs'):
                    assert request(base, path)[0] == 404
                assert request(base, '/nl-to-sparql', {'text': 'How many patients have cholera?'})[0] == 404
            else:
                status, _, body = request(base, '/nl-to-sparql', {'text': 'How many patients have cholera, by sex?'})
                answer = json.loads(body)
                assert status == 200 and answer['is_compliant'] is True
                assert isinstance(answer['sparql_query'], str) and answer['tokens_consumed'] == 0
                status, _, body = request(base, '/nl-to-sparql', {'text': 'How many patients have cholera, and which ones are they?'})
                answer = json.loads(body)
                assert status == 200 and answer['is_compliant'] is False and answer['compliance_reason']
            # Inspect only filenames/booleans. Neither runtime mode executes a database query.
            check = ("from pathlib import Path; p=Path('/app'); "
                     "assert not (p/'.env').exists(); assert not (p/'.git').exists(); "
                     "assert not (p/'patients').exists(); assert not (p/'ontologies').exists(); "
                     "assert not any((p/'assets').iterdir()); "
                     "assert (p/'data/schema_cards.json').is_file()")
            subprocess.run(['docker', 'exec', container, 'python', '-c', check], check=True)
    print('Container smoke checks passed: developer/public modes, static console, API contract, non-root and private-file boundary.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--platform', required=True, choices=['linux/amd64', 'linux/arm64'])
    args = parser.parse_args()
    smoke(args.image, args.platform)


if __name__ == '__main__':
    main()
