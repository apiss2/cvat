"""Existing CVAT session reuse, CSRF boundaries, ownership identity and revocation."""
import asyncio
import json
import ssl
from dataclasses import replace
from pathlib import Path

import certifi
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from conftest import FakeRuntime, ROOT, SESSIONS, TOKENS
from registry.auth import Auth
from registry.config import Settings
from registry.manager import create_app

ORIGIN = "https://cvat.example"
PREFIX = "/model-registry"


class PrefixProxy:
    """Match Traefik StripPrefix while retaining the external browser URL."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            if not scope['path'].startswith(PREFIX + '/'):
                return await JSONResponse({}, status_code=404)(scope, receive, send)
            scope = {**scope, 'path': scope['path'][len(PREFIX):],
                     'raw_path': scope['raw_path'][len(PREFIX):]}
        return await self.app(scope, receive, send)


class CVATServer:
    def __init__(self):
        self.calls = []
        self.user = {'id': 7, 'username': 'alice', 'is_active': True,
                     'groups': ['user'], 'is_superuser': False, 'is_staff': False,
                     'email': 'private@example.test'}
        self.status = 200
        self.cookie_name = 'sessionid'
        self.session = SESSIONS['alice']
        self.malformed = None
        self.failure = None
        self.public_host = 'cvat.example'
        self.public_scheme = 'https'

    def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == 'cvat_server'
        assert request.url.path == '/api/users/self'
        assert request.method == 'GET' and not request.content
        assert request.headers['accept'] == 'application/vnd.cvat+json'
        assert 'authorization' not in request.headers
        assert 'x-forwarded-host' not in request.headers
        assert 'x-forwarded-for' not in request.headers
        assert 'origin' not in request.headers
        assert request.headers['host'] == self.public_host
        assert request.headers['x-forwarded-proto'] == self.public_scheme
        assert request.headers['cookie'] == self.cookie_name + '=' + self.session
        if self.failure:
            raise self.failure
        if self.status != 200:
            return httpx.Response(self.status, text='private upstream diagnostic',
                                  headers={'Location': 'https://attacker.example/collect'})
        if self.malformed is not None:
            return httpx.Response(200, content=self.malformed)
        return httpx.Response(200, json=self.user,
                              headers={'Set-Cookie': 'upstream-private-cookie=secret; Path=/'})


@pytest.fixture
def cvat_ctx(tmp_path, monkeypatch):
    secret = tmp_path / 'service_token'
    secret.write_text(TOKENS['service'])
    settings = Settings(data_dir=tmp_path/'data', host_data_dir=str(tmp_path/'data'),
                        service_token_file=secret, public_url=ORIGIN+PREFIX+'/',
                        cvat_url='http://cvat_server:8080')
    fake = CVATServer()
    actual_client = httpx.AsyncClient

    def transport_client(**kwargs):
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        assert kwargs['verify'] is True
        return actual_client(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', transport_client)
    app = create_app(settings, FakeRuntime())
    with TestClient(PrefixProxy(app), base_url=ORIGIN) as client:
        yield {'client': client, 'app': app, 'auth': app.state.auth, 'settings': settings, 'cvat': fake}


def cvat_login(ctx):
    # Represents the cookie CVAT's own login already set in the browser.
    ctx['client'].cookies.set(ctx['settings'].cvat_session_cookie, ctx['cvat'].session, path='/')


def mutation_headers():
    return {'Origin': ORIGIN, 'X-Registry-Request': '1'}


def test_existing_cvat_session_opens_registry_without_separate_login(cvat_ctx):
    ctx = cvat_ctx
    c = ctx['client']
    assert c.get(PREFIX+'/api/me').status_code == 401
    assert ctx['cvat'].calls == []
    cvat_login(ctx)
    response = c.get(PREFIX+'/api/me')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data == {'id': 7, 'name': 'alice', 'admin': False, 'auth_mode': 'cvat'}
    assert 'set-cookie' not in response.headers
    assert ctx['cvat'].session not in response.text
    assert 'private@example.test' not in response.text
    assert 'no-store' in response.headers['cache-control']
    assert c.get(PREFIX+'/api/models').json() == []
    assert len(ctx['cvat'].calls) == 2  # Each protected request revalidates with CVAT.
    assert not c.cookies.get('upstream-private-cookie')


def test_only_configured_cvat_cookie_reaches_trusted_server(cvat_ctx):
    ctx = cvat_ctx
    cvat_login(ctx)
    ctx['client'].cookies.set('cvat_model_registry_session', 'old-registry-cookie', path='/')
    ctx['client'].cookies.set('csrftoken', 'csrf-secret', path='/')
    ctx['client'].cookies.set('unrelated', 'private-third-party-value', path='/')
    response = ctx['client'].get(PREFIX+'/api/me', headers={
        'Authorization': 'Bearer ignored-private-token', 'X-Forwarded-Host': 'attacker.example',
        'X-Forwarded-For': '10.0.0.1', 'Host': 'attacker.example', 'X-Forwarded-Proto': 'http',
        'Accept': 'text/html',
    })
    assert response.status_code == 200
    sent = ctx['cvat'].calls[-1]
    assert sent.headers['cookie'] == 'sessionid='+ctx['cvat'].session
    assert all(value not in str(sent.headers) for value in (
        'old-registry-cookie', 'csrf-secret', 'private-third-party-value', 'ignored-private-token',
    ))


@pytest.mark.parametrize('path', ['login', 'logout', 'register'])
def test_registry_has_no_credential_or_session_management_endpoints(cvat_ctx, path):
    c = cvat_ctx['client']
    for authenticated in (False, True):
        if authenticated:
            cvat_login(cvat_ctx)
        response = c.post(PREFIX+'/api/auth/'+path, headers=mutation_headers(),
                          json={'username': 'alice', 'password': 'never-echo-this-password'})
        assert response.status_code == 404
        assert 'never-echo-this-password' not in response.text
        assert 'set-cookie' not in response.headers
    assert cvat_ctx['cvat'].calls == []


@pytest.mark.parametrize('headers', [
    {}, {'Origin': ORIGIN}, {'X-Registry-Request': '1'},
    {'Origin': 'null', 'X-Registry-Request': '1'},
    {'Origin': 'https://attacker.example', 'X-Registry-Request': '1'},
    {'Origin': 'https://cvat.example.attacker.test', 'X-Registry-Request': '1'},
    {'Origin': ORIGIN, 'X-Registry-Request': 'wrong'},
    {'Origin': ORIGIN, 'X-Registry-Request': '1', 'Sec-Fetch-Site': 'cross-site'},
])
def test_mutations_reject_csrf_before_parsing_upload_or_forwarding_cookie(cvat_ctx, headers):
    ctx = cvat_ctx
    cvat_login(ctx)
    response = ctx['client'].post(PREFIX+'/api/models',
                                  headers={**headers, 'Content-Length': str(3*1024**3)}, content=b'x')
    assert response.status_code == 403
    assert ctx['cvat'].calls == []


def test_authenticated_same_origin_upload_is_limited_after_authentication(cvat_ctx):
    ctx = cvat_ctx
    cvat_login(ctx)
    response = ctx['client'].post(PREFIX+'/api/models',
                                  headers={**mutation_headers(), 'Content-Length': str(3*1024**3)}, content=b'x')
    assert response.status_code == 413
    assert len(ctx['cvat'].calls) == 1


@pytest.mark.parametrize('expected_id', [None, '7'])
def test_upload_accepts_current_identity_and_clients_without_identity_hint(cvat_ctx, expected_id):
    ctx = cvat_ctx
    cvat_login(ctx)
    supplied = mutation_headers()
    if expected_id is not None:
        supplied['X-Registry-User-ID'] = expected_id
    response = ctx['client'].post(PREFIX+'/api/models', headers=supplied, files={
        'package': ('model.zip', (ROOT/'examples/segmentation-demo.zip').read_bytes(), 'application/zip'),
    })
    assert response.status_code == 202, response.text
    assert response.json()['owner'] == 'alice'
    assert len(ctx['cvat'].calls) == 1


@pytest.mark.parametrize('name,user_id,expected_id,chunked', [
    ('bob', 19, '7', False), ('bob', 19, '7', True),
    ('alice', 7, '07', False), ('alice', 7, '', True),
])
def test_stale_or_nonexact_identity_rejected_before_reading_upload_body(
        cvat_ctx, name, user_id, expected_id, chunked):
    ctx = cvat_ctx
    ctx['cvat'].session = SESSIONS[name]
    ctx['cvat'].user.update(id=user_id, username=name)
    headers = {
        **mutation_headers(), 'Cookie': 'sessionid='+ctx['cvat'].session,
        'X-Registry-User-ID': expected_id,
        'Content-Type': 'multipart/form-data; boundary=sample-boundary',
    }
    if chunked:
        headers['Transfer-Encoding'] = 'chunked'
    else:
        headers['Content-Length'] = '1048576'
    responses = []

    async def receive():
        raise AssertionError('The upload body must not be consumed for a stale account')

    async def send(message):
        responses.append(message)

    scope = {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'POST', 'scheme': 'https', 'path': '/api/models',
        'raw_path': b'/api/models', 'query_string': b'',
        'headers': [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        'client': ('127.0.0.1', 1), 'server': ('cvat.example', 443),
    }
    asyncio.run(ctx['app'](scope, receive, send))
    response = next(item for item in responses if item['type'] == 'http.response.start')
    assert response['status'] == 409
    assert dict(response['headers'])[b'x-registry-account-changed'] == b'1'
    body = b''.join(item.get('body', b'') for item in responses if item['type'] == 'http.response.body')
    assert json.loads(body) == {'detail': 'CVAT account changed; refresh the registry before continuing'}
    assert len(ctx['cvat'].calls) == 1
    assert ctx['app'].state.service.store.models() == []


def test_cross_origin_preflight_never_grants_browser_access(cvat_ctx):
    response = cvat_ctx['client'].options(PREFIX+'/api/models', headers={
        'Origin': 'https://attacker.example', 'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'X-Registry-Request',
    })
    assert 'access-control-allow-origin' not in response.headers
    assert 'access-control-allow-credentials' not in response.headers


def test_roles_are_refreshed_and_staff_alone_is_not_registry_admin(cvat_ctx):
    ctx = cvat_ctx
    c = ctx['client']
    cvat_login(ctx)
    ctx['cvat'].user['is_staff'] = True
    assert c.get(PREFIX+'/api/me').json()['admin'] is False
    ctx['cvat'].user['groups'] = ['user', 'admin']
    assert c.get(PREFIX+'/api/me').json()['admin'] is True
    ctx['cvat'].user['groups'] = ['user']
    assert c.get(PREFIX+'/api/me').json()['admin'] is False
    ctx['cvat'].user['is_superuser'] = True
    assert c.get(PREFIX+'/api/me').json()['admin'] is True


@pytest.mark.parametrize('change', ['disabled', 'deleted', 'session_expired', 'logged_out'])
def test_cvat_session_revocation_applies_to_registry_immediately(cvat_ctx, change):
    ctx = cvat_ctx
    c = ctx['client']
    cvat_login(ctx)
    assert c.get(PREFIX+'/api/me').status_code == 200
    if change == 'disabled':
        ctx['cvat'].user['is_active'] = False
    elif change == 'logged_out':
        c.cookies.clear()
    else:
        ctx['cvat'].status = 401
    response = c.get(PREFIX+'/api/me')
    assert response.status_code == (403 if change == 'disabled' else 401)


def test_changed_cvat_session_returns_current_identity_without_registry_cache(cvat_ctx):
    ctx = cvat_ctx
    cvat_login(ctx)
    assert ctx['client'].get(PREFIX+'/api/me').json()['name'] == 'alice'
    ctx['cvat'].session = SESSIONS['bob']
    ctx['cvat'].user.update(id=19, username='bob')
    cvat_login(ctx)
    response = ctx['client'].get(PREFIX+'/api/me', headers={'X-Registry-User-ID': '7'})
    assert response.json() == {'id': 19, 'name': 'bob', 'admin': False, 'auth_mode': 'cvat'}
    assert 'set-cookie' not in response.headers


@pytest.mark.parametrize('status', [302, 404, 500, 503])
def test_cvat_unavailable_fails_closed_without_leaking_or_redirecting(cvat_ctx, status):
    ctx = cvat_ctx
    cvat_login(ctx)
    ctx['cvat'].status = status
    response = ctx['client'].get(PREFIX+'/api/models')
    assert response.status_code == 503
    assert 'private upstream diagnostic' not in response.text
    assert len(ctx['cvat'].calls) == 1
    assert 'location' not in response.headers


@pytest.mark.parametrize('upstream_status,status', [(400, 502), (401, 401), (403, 403), (406, 502)])
def test_cvat_connection_and_permission_errors_are_not_misreported_as_login_required(
        cvat_ctx, upstream_status, status):
    cvat_login(cvat_ctx)
    cvat_ctx['cvat'].status = upstream_status
    response = cvat_ctx['client'].get(PREFIX+'/api/me')
    assert response.status_code == status
    assert 'private upstream diagnostic' not in response.text
    assert 'location' not in response.headers


def test_cvat_unsupported_response_format_is_reported_without_retry(cvat_ctx):
    cvat_login(cvat_ctx)
    cvat_ctx['cvat'].status = 406
    response = cvat_ctx['client'].get(PREFIX+'/api/me')
    assert response.status_code == 502
    assert 'HTTP 406' in response.json()['detail']
    assert 'application/vnd.cvat+json' in response.json()['detail']
    assert 'ALLOWED_HOSTS' not in response.text
    assert 'private upstream diagnostic' not in response.text
    assert len(cvat_ctx['cvat'].calls) == 1
    assert 'set-cookie' not in response.headers


@pytest.mark.parametrize('public_url,host,scheme', [
    ('http://cvat.example:8080/model-registry/', 'cvat.example:8080', 'http'),
    ('https://cvat.example:443/model-registry/', 'cvat.example', 'https'),
    ('https://[2001:db8::1]:8443/model-registry/', '[2001:db8::1]:8443', 'https'),
])
def test_internal_request_uses_configured_public_host_and_scheme(cvat_ctx, public_url, host, scheme):
    cvat_login(cvat_ctx)
    cvat_ctx['auth'].settings = replace(cvat_ctx['settings'], public_url=public_url)
    cvat_ctx['cvat'].public_host = host
    cvat_ctx['cvat'].public_scheme = scheme
    assert cvat_ctx['client'].get(PREFIX+'/api/me').status_code == 200


def test_cvat_timeout_fails_closed(cvat_ctx):
    cvat_login(cvat_ctx)
    cvat_ctx['cvat'].failure = httpx.ReadTimeout('private upstream detail')
    response = cvat_ctx['client'].get(PREFIX+'/api/models')
    assert response.status_code == 503 and 'private upstream detail' not in response.text


def test_cvat_rate_limit_does_not_turn_into_login_error(cvat_ctx):
    cvat_login(cvat_ctx)
    cvat_ctx['cvat'].status = 429
    response = cvat_ctx['client'].get(PREFIX+'/api/models')
    assert response.status_code == 429 and response.headers['retry-after'] == '60'


@pytest.mark.parametrize('malformed', [b'not-json containing upstream secrets', b'[]'])
def test_invalid_cvat_payload_is_rejected(cvat_ctx, malformed):
    cvat_login(cvat_ctx)
    cvat_ctx['cvat'].malformed = malformed
    response = cvat_ctx['client'].get(PREFIX+'/api/me')
    assert response.status_code == 503 and 'upstream secrets' not in response.text


@pytest.mark.parametrize('changes', [
    {'id': True}, {'id': 0}, {'username': ''}, {'username': ' alice'},
    {'username': 'alice\nadmin'}, {'groups': 'admin'}, {'groups': [True]},
    {'is_active': None}, {'is_active': 'true'}, {'is_active': 1},
])
def test_invalid_cvat_identity_is_rejected(cvat_ctx, changes):
    cvat_login(cvat_ctx)
    cvat_ctx['cvat'].user.update(changes)
    assert cvat_ctx['client'].get(PREFIX+'/api/me').status_code == 503


@pytest.mark.parametrize('value', ['', 'a'*4097, 'a b', 'a,b', 'a\\b'])
def test_invalid_cvat_cookie_is_never_forwarded(cvat_ctx, value):
    cvat_ctx['client'].cookies.set('sessionid', value, path='/')
    assert cvat_ctx['client'].get(PREFIX+'/api/me').status_code == 401
    assert cvat_ctx['cvat'].calls == []


def test_old_registry_cookie_and_personal_tokens_do_not_authenticate(cvat_ctx):
    c = cvat_ctx['client']
    c.cookies.set('cvat_model_registry_session', 'old-registry-session', path=PREFIX+'/')
    for scheme in ('Bearer', 'Token'):
        response = c.get(PREFIX+'/api/me', headers={'Authorization': scheme+' '+SESSIONS['alice']})
        assert response.status_code == 401
    assert cvat_ctx['cvat'].calls == []
    assert c.get(PREFIX+'/internal/functions', headers={'Authorization': 'Bearer '+TOKENS['service']}).status_code == 200
    assert c.get(PREFIX+'/internal/functions', headers={'Authorization': 'Bearer '+SESSIONS['alice']}).status_code == 401


def test_custom_cvat_cookie_name_is_supported(cvat_ctx):
    ctx = cvat_ctx
    ctx['auth'].settings = replace(ctx['settings'], cvat_session_cookie='team_cvat_session')
    ctx['cvat'].cookie_name = 'team_cvat_session'
    ctx['client'].cookies.set('sessionid', 'wrong-cookie', path='/')
    assert ctx['client'].get(PREFIX+'/api/me').status_code == 401
    ctx['client'].cookies.set('team_cvat_session', ctx['cvat'].session, path='/')
    assert ctx['client'].get(PREFIX+'/api/me').status_code == 200


@pytest.mark.parametrize('fields', [
    {'auth_mode': 'other'}, {'auth_mode': 'token'}, {'public_url': ''},
    {'public_url': 'https://cvat.example/model-registry'},
    {'public_url': 'https://cvat.example/model-registry/?x=1'},
    {'public_url': 'https://u:p@cvat.example/model-registry/'},
    {'public_url': 'https://cvat.example/../'}, {'cvat_url': 'http://cvat_server/api'},
    {'cvat_url': 'https://user:password@cvat.example'},
    {'cvat_session_cookie': ''}, {'cvat_session_cookie': 'a;b'}, {'cvat_session_cookie': 'a'*129},
])
def test_invalid_auth_configuration_is_rejected(tmp_path, fields):
    defaults = {'data_dir': tmp_path, 'host_data_dir': str(tmp_path),
                'public_url': ORIGIN+PREFIX+'/'}
    defaults.update(fields)
    with pytest.raises(ValueError):
        Settings(**defaults).validate()


def test_authentication_cannot_be_bypassed_by_an_asgi_root_path(cvat_ctx):
    client = TestClient(cvat_ctx['app'], root_path=PREFIX, base_url=ORIGIN)
    try:
        assert client.get(PREFIX+'/api/me').status_code == 401
        assert client.get(PREFIX+'/internal/functions').status_code == 401
        response = client.get(PREFIX+'/internal/functions', headers={'Authorization': 'Bearer '+TOKENS['service']})
        assert response.status_code == 200 and response.json() == {}
    finally:
        client.close()


def test_custom_cvat_ca_bundle_reaches_https_client_with_verification_enabled(tmp_path, monkeypatch):
    source = Path(certifi.where()).read_text()
    pem = '-----BEGIN CERTIFICATE-----' + source.split('-----BEGIN CERTIFICATE-----', 1)[1].split('-----END CERTIFICATE-----', 1)[0] + '-----END CERTIFICATE-----\n'
    ca_file = tmp_path/'cvat-ca.pem'
    ca_file.write_text(pem)
    settings = Settings(data_dir=tmp_path, host_data_dir=str(tmp_path),
                        public_url=ORIGIN+PREFIX+'/', cvat_url='https://cvat_server:8080',
                        cvat_ca_file=str(ca_file))
    settings.validate()
    auth = Auth(settings)
    actual_client = httpx.AsyncClient
    fake = CVATServer()
    contexts = []

    def transport_client(**kwargs):
        contexts.append(kwargs['verify'])
        return actual_client(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', transport_client)
    user = asyncio.run(auth._identity(fake.session))
    assert user.name == 'alice'
    context = contexts[0]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert context.get_ca_certs(binary_form=True) == [ssl.PEM_cert_to_DER_cert(pem)]


@pytest.mark.parametrize('kind', ['missing', 'invalid_pem', 'relative'])
def test_bad_custom_cvat_ca_fails_startup_explicitly(tmp_path, kind):
    path = tmp_path/'cvat-ca.pem'
    if kind == 'invalid_pem':
        path.write_text('This is not a CA certificate')
    value = 'relative.pem' if kind == 'relative' else str(path)
    settings = Settings(data_dir=tmp_path, host_data_dir=str(tmp_path),
                        public_url=ORIGIN+PREFIX+'/', cvat_ca_file=value)
    with pytest.raises(ValueError, match='MR_CVAT_CA_FILE'):
        settings.validate()
