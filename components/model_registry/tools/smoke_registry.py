#!/usr/bin/env python3
"""Live API smoke using a temporary session created by CVAT's own login API.

Creates two demonstration models, tests update/rollback/delete, and cleans them up.
No CVAT annotation is changed. Model tombstones and retained weights remain for audit.
The password is read interactively, never from a command-line argument.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import http.cookiejar
import json
import math
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a password or cookie to a redirected origin.
        return None


def assert_polygon(result: dict) -> None:
    assert result['type'] == 'polygon' and 'mask' not in result, result
    points = result['points']
    assert len(points) >= 6 and len(points) % 2 == 0, result
    vertices = list(zip(points[0::2], points[1::2]))
    assert all(math.isfinite(x) and math.isfinite(y) and 12 <= x <= 43 and 8 <= y <= 31 for x, y in vertices), result
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    assert all(math.dist(a, b) >= 2 - 1e-8 for a, b in edges), result
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) / 2
    assert area >= 10, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='CVAT browser URL ending in /model-registry/')
    parser.add_argument('--username', default=os.environ.get('MR_CVAT_USERNAME', ''), help='CVAT username (also MR_CVAT_USERNAME)')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        parser.error('This creates and deletes demonstration models. Pass --run to execute.')
    public = urlsplit(args.url)
    if (public.scheme not in ('http', 'https') or not public.hostname
            or public.username is not None or public.password is not None or public.query or public.fragment
            or public.path not in ('/model-registry', '/model-registry/')
            or any(char.isspace() for char in args.url)):
        parser.error('--url must be the CVAT browser origin followed by /model-registry/')
    try:
        port = public.port
    except ValueError as exc:
        parser.error(str(exc))
    host = '[' + public.hostname + ']' if ':' in public.hostname else public.hostname
    port_suffix = f':{port}' if port and port != {'http': 80, 'https': 443}[public.scheme] else ''
    origin = f'{public.scheme}://{host}{port_suffix}'
    base = args.url.rstrip('/')
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies), NoRedirect())
    created = []

    def request(method, path, body=None, content_type='application/json', *, cvat=False):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        headers = {'Content-Type': content_type, 'Origin': origin}
        if cvat:
            csrf = next((cookie.value for cookie in cookies if cookie.name == 'csrftoken'), '')
            if csrf:
                headers['X-CSRFToken'] = csrf
        else:
            headers['X-Registry-Request'] = '1'
        req = urllib.request.Request((origin if cvat else base) + path, data=body, method=method, headers=headers)
        try:
            with opener.open(req, timeout=210) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4000).decode(errors='replace')
            raise RuntimeError(f'{method} {path}: HTTP {exc.code} {detail}') from exc

    username = args.username or input('CVAT username: ').strip()
    password = getpass.getpass('CVAT password: ')
    try:
        # This goes directly to the CVAT origin, never through the registry.
        # The cookie jar keeps only this CLI session; no browser session is read.
        request('POST', '/api/auth/login', {'username': username, 'password': password}, cvat=True)
    finally:
        password = ''

    def upload(kind, model=None, expected=None):
        boundary = '----registry-smoke-' + secrets.token_hex(12)
        parts = []
        directory = ROOT / 'examples' / kind
        for name, value in [('model_id', model), ('expected_revision', expected),
                            ('manifest', (directory / 'manifest.json').read_text(encoding='utf-8'))]:
            if value:
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for field, filename, content_type in [('code', 'model.py', 'text/plain'),
                                               ('weights', 'model.onnx', 'application/octet-stream'),
                                               ('sample', 'sample.png', 'image/png')]:
            parts.extend([
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode(),
                (directory / filename).read_bytes(), b'\r\n',
            ])
        parts.append(f'--{boundary}--\r\n'.encode())
        operation = request('POST', '/api/upload', b''.join(parts), 'multipart/form-data; boundary=' + boundary)
        if not model:
            created.append(operation['model_id'])
        end = time.monotonic() + 210
        while time.monotonic() < end:
            operation = request('GET', '/api/operations/' + operation['id'])
            if operation['status'] == 'succeeded':
                return operation
            if operation['status'] == 'failed':
                raise RuntimeError('Registration failed: ' + operation['detail'])
            time.sleep(1)
        raise TimeoutError('Registration timed out; inspect operation ' + operation['id'])

    try:
        request('GET', '/api/me')
        for kind in ('segmentation', 'detection'):
            operation = upload(kind)
            model_id, old = operation['model_id'], operation['revision']
            image = base64.b64encode((ROOT / 'examples' / kind / 'sample.png').read_bytes()).decode()
            result = request('POST', f'/api/models/{model_id}/test', {'image': image})
            assert len(result['results']) == 1, result
            if kind == 'detection':
                assert result['results'][0]['type'] == 'rectangle', result
                assert result['results'][0]['points'] == [12., 8., 44., 32.], result
            else:
                assert_polygon(result['results'][0])
            new = upload(kind, model_id, old)
            result = request('POST', f'/api/models/{model_id}/test?revision={old}', {'image': image})
            assert result['revision'] == old
            request('POST', f'/api/models/{model_id}/rollback', {'expected_revision': new['revision'], 'revision': old})
            assert request('GET', f'/api/models/{model_id}/logs')
            print(f'PASS: {kind}: registration, inference, version pinning, update, rollback, logs')
    finally:
        cleanup_errors = []
        for model_id in created:
            try:
                model = request('GET', f'/api/models/{model_id}')
                request('DELETE', f'/api/models/{model_id}', {'expected_revision': model['active_revision']})
                assert request('GET', f'/api/models/{model_id}')['deleted']
                print('Disabled demo model ' + model_id)
            except Exception as exc:
                cleanup_errors.append((model_id, str(exc)))
        try:
            request('POST', '/api/auth/logout', cvat=True)
        except Exception as exc:
            print(f'Could not close the smoke-test CVAT session: {exc}')
        finally:
            cookies.clear()
        if cleanup_errors:
            raise RuntimeError(f'Cleanup failed; disable these demo IDs manually: {cleanup_errors}')


if __name__ == '__main__':
    main()
