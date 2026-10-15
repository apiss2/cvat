import base64
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from registry.codec import decode_image, encode_results
from registry.config import Settings
from registry.manager import create_app
from registry.schema import Manifest
from registry.sdk import Box
from registry.runtime import RuntimeFailure

TOKENS = {'service': 's'*48}
SESSIONS = {'alice': 'a'*32, 'bob': 'b'*32, 'admin': 'd'*32}
ORIGIN = 'http://testserver'

class FakeRuntime:
    """Contract fake: never imports uploaded code and never uses ONNX or Docker."""
    def __init__(self):
        self.calls = []
        self.revoked = set()
        self.fail_next = False
        self.block = None
        self.closed = False
    def reconcile(self): pass
    def call(self, model_id, revision, directory, payload, request_id):
        self.calls.append((model_id, revision, payload, request_id))
        if self.block:
            self.block.wait(timeout=5)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeFailure('TEST load/predict error')
        manifest = Manifest.model_validate_json((directory/'manifest.json').read_text())
        image = decode_image(payload['image'])
        bitmap = image.mean(axis=2) > 128
        if manifest.labels[0].type == 'polygon':
            output = np.stack([bitmap for _ in manifest.labels])
        else:
            output = [Box(label.id, .9, (12, 8, 44, 32)) for label in manifest.labels]
        return encode_results(output, manifest, image.shape)
    def discard(self, *args): pass
    def revoke(self, model_id): self.revoked.add(model_id)
    def close(self): self.closed = True

@pytest.fixture
def ctx(tmp_path, monkeypatch):
    secret = tmp_path/'service_token'
    secret.write_text(TOKENS['service'])
    settings = Settings(data_dir=tmp_path/'data', host_data_dir=str(tmp_path/'data'),
                        service_token_file=secret, public_url=ORIGIN+'/model-registry/')
    users = {name: {'id': index, 'username': name, 'is_active': True,
                    'groups': ['admin'] if name == 'admin' else ['user']}
             for index, name in enumerate(SESSIONS, 1)}

    def cvat_response(request):
        assert request.method == 'GET' and request.url == 'http://cvat_server:8080/api/users/self'
        assert request.headers['accept'] == 'application/vnd.cvat+json'
        assert 'authorization' not in request.headers
        supplied = request.headers.get('cookie')
        for name, session in SESSIONS.items():
            if supplied == 'sessionid=' + session:
                return httpx.Response(200, json=users[name])
        return httpx.Response(401)

    actual_client = httpx.AsyncClient

    def transport_client(**kwargs):
        return actual_client(transport=httpx.MockTransport(cvat_response), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', transport_client)
    runtime = FakeRuntime()
    app = create_app(settings, runtime)
    with TestClient(app, base_url=ORIGIN) as client:
        yield {'client': client, 'runtime': runtime, 'service': app.state.service,
               'settings': settings, 'root': ROOT, 'cvat_users': users}


def headers(user='alice'):
    if user == 'service':
        return {'Authorization': 'Bearer ' + TOKENS['service']}
    return {'Cookie': 'sessionid=' + SESSIONS[user], 'Origin': ORIGIN,
            'X-Registry-Request': '1'}

def upload(ctx, kind='segmentation', *, user='alice', model_id=None, expected=None):
    data = {}
    if model_id: data.update(model_id=model_id, expected_revision=expected or '')
    c = ctx['client']
    r = c.post('/api/models', headers=headers(user), data=data, files={'package': ('model.zip', (ROOT/'examples'/f'{kind}-demo.zip').read_bytes(), 'application/zip')})
    assert r.status_code == 202, r.text
    op = r.json()
    deadline = time.monotonic()+8
    while time.monotonic()<deadline:
        op = c.get('/api/operations/'+op['id'], headers=headers(user)).json()
        if op['status'] in ('succeeded','failed'): return op
        time.sleep(.01)
    raise AssertionError(f'operation did not finish: {op}')

@pytest.fixture
def published(ctx):
    op = upload(ctx)
    assert op['status']=='succeeded', op
    return op

@pytest.fixture
def sample():
    return base64.b64encode((ROOT/'examples/segmentation/sample.png').read_bytes()).decode()
