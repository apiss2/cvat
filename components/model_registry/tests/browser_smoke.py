#!/usr/bin/env python3
"""Browser integration test with a native CVAT session mock and fake inference.

Runs the real registry API and static UI under /model-registry/. The browser
first signs into native CVAT and opens the registry from its tasks page. The
mock CVAT issues its own root-path sessionid cookie. No Docker, production
credentials or production CVAT data is used.
Optional dependency: playwright plus Chromium.
"""
import argparse
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from conftest import FakeRuntime, SESSIONS, TOKENS
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from registry.config import Settings
from registry.manager import create_app
import uvicorn
from playwright.sync_api import sync_playwright, expect


@contextmanager
def serve(app, sock=None):
    sock = sock or socket.socket()
    if not sock.getsockname()[1]:
        sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium')
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    cvat = FastAPI()
    auth_calls = []
    identity_failure = {'status': 0}
    valid_sessions = set(SESSIONS.values())
    model_names = {'segmentation': '表面欠陥のセグメンテーション', 'detection': '部品の検出'}

    @cvat.get('/auth/login')
    async def native_login_page():
        return HTMLResponse('''<!doctype html><html lang="en"><title>CVAT login mock</title>
        <h1>CVAT login mock</h1><form action="/api/auth/login" method="post">
        <label>Username<input id="native-username" name="username" autocomplete="username"></label>
        <label>Password<input id="native-password" type="password" name="password" autocomplete="current-password"></label>
        <button id="native-signin" type="submit">Log in to CVAT</button></form></html>''')

    @cvat.post('/api/auth/login')
    async def native_login(request: Request):
        is_json = request.headers.get('content-type', '').startswith('application/json')
        data = await request.json() if is_json else await request.form()
        if data.get('username') not in ('alice', 'bob') or data.get('password') != 'browser-test-only':
            return JSONResponse({'detail': 'invalid'}, status_code=400)
        session = SESSIONS[data['username']]
        valid_sessions.add(session)
        response = JSONResponse({'key': 'unused-cvat-response-token'}) if is_json else RedirectResponse('/tasks', status_code=303)
        response.set_cookie('sessionid', session, path='/', httponly=True, samesite='lax')
        return response

    @cvat.post('/api/auth/logout')
    async def native_logout(request: Request):
        valid_sessions.discard(request.cookies.get('sessionid'))
        response = JSONResponse({'detail': 'Successfully logged out'})
        response.delete_cookie('sessionid', path='/')
        return response

    @cvat.get('/')
    async def native_root(request: Request):
        return RedirectResponse('/tasks' if request.cookies.get('sessionid') in valid_sessions else '/auth/login')

    @cvat.get('/tasks')
    async def native_tasks(request: Request):
        if request.cookies.get('sessionid') not in valid_sessions:
            return RedirectResponse('/auth/login')
        return HTMLResponse('<h1>CVAT tasks mock</h1><a id="open-registry" href="/model-registry/#models" target="_blank" rel="noopener">Model registry</a>')

    @cvat.get('/api/users/self')
    async def identity(request: Request):
        # The manager forwards only CVAT's session cookie; no personal API token.
        assert 'authorization' not in request.headers
        assert request.headers.get('host') == origin.removeprefix('http://')
        if identity_failure['status']:
            return JSONResponse({'detail': 'CVAT connection test failure'}, status_code=identity_failure['status'])
        supplied = request.cookies.get('sessionid')
        for index, name in enumerate(('alice', 'bob'), 1):
            if supplied == SESSIONS[name] and supplied in valid_sessions:
                auth_calls.append(name)
                return {'id': index, 'username': name, 'is_active': True, 'is_staff': False, 'is_superuser': False, 'groups': ['user']}
        return JSONResponse({'detail': 'invalid'}, status_code=401)

    def enter_native_credentials(tab, name):
        tab.locator('#native-username').fill(name)
        tab.locator('#native-password').fill('browser-test-only')
        tab.locator('#native-signin').click()
        expect(tab.locator('h1')).to_have_text('CVAT tasks mock')

    def return_to_registry(page):
        page.bring_to_front()
        # Chromium headless does not consistently dispatch the window focus
        # event on bring_to_front, so also send the same event explicitly.
        page.evaluate("window.dispatchEvent(new Event('focus'))")

    def screenshot(page, suffix=''):
        if args.screenshot:
            path = args.screenshot.with_name(args.screenshot.stem + suffix + args.screenshot.suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)

    with tempfile.TemporaryDirectory(prefix='registry-browser-') as directory, serve(cvat) as cvat_url:
        root = Path(directory)
        secret = root / 'service_token'
        secret.write_text(TOKENS['service'])
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        app = create_app(Settings(data_dir=root / 'data', host_data_dir=str(root / 'data'),
                                  service_token_file=secret, cvat_url=cvat_url,
                                  public_url=origin + '/model-registry/'), FakeRuntime())

        async def proxy(scope, receive, send):
            # The real deployment proxy strips only the registry prefix.
            if scope['type'] == 'http':
                if not scope['path'].startswith('/model-registry/'):
                    return await cvat(scope, receive, send)
                scope = dict(scope)
                scope['path'] = scope['path'][len('/model-registry'):]
                scope['raw_path'] = scope['path'].encode()
            await app(scope, receive, send)

        with serve(proxy, sock), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium or None, headless=True,
                                                  args=['--no-sandbox', '--disable-dev-shm-usage'])
            context = browser.new_context(viewport={'width': 1440, 'height': 1050}, locale='ja-JP')
            # The normal entry point is a CVAT page that is already signed in.
            native_tab = context.new_page()
            native_tab.goto(origin + '/auth/login')
            enter_native_credentials(native_tab, 'alice')
            with context.expect_page() as registry_tab:
                native_tab.locator('#open-registry').click()
            page = registry_tab.value
            errors = []
            registry_login_requests = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: registry_login_requests.append(request.url)
                    if '/model-registry/api/auth/' in request.url else None)
            expect(page.locator('#workspace')).to_be_visible()
            expect(page.locator('#catalog')).to_be_visible()
            expect(page.locator('#access-panel')).to_be_hidden()
            assert not page.locator('input[type=password], #token, #username, #logout, #cvat-login, a[href="/auth/login"]').count()
            assert page.locator('#return-cvat').get_attribute('href') == '/'
            assert page.locator('#return-cvat').get_attribute('target') is None
            assert page.evaluate("getComputedStyle(document.querySelector('.app-header')).backgroundColor") == 'rgb(19, 36, 60)'
            expect(page.locator('#registration')).to_be_hidden()
            expect(page.locator('#identity')).to_have_text('alice')
            page.reload()
            expect(page.locator('#workspace')).to_be_visible()
            cookies = context.cookies()
            session = next(cookie for cookie in cookies if cookie['name'] == 'sessionid')
            assert session['httpOnly'] and session['path'] == '/'
            assert not any(cookie['name'].startswith('cvat_model_registry') for cookie in cookies)

            # A CVAT connection error must not send a signed-in user back to a
            # login screen. It shows the connection problem and supports retry.
            for cvat_status, registry_status, title in [
                (400, 502, 'CVATとの連携に失敗しました'),
                (503, 503, 'CVATとの接続を確認できません'),
                (403, 403, 'CVATのアカウントを利用できません'),
            ]:
                identity_failure['status'] = cvat_status
                with page.expect_response(lambda response: response.url.endswith('/api/me')) as failed_identity:
                    page.reload()
                assert failed_identity.value.status == registry_status
                expect(page.locator('#access-title')).to_have_text(title)
                expect(page.locator('#workspace')).to_be_hidden()
                expect(page.locator('#access-actions')).to_be_visible()
                assert 'ログイン' not in page.locator('#access-panel').inner_text()
                if cvat_status == 400:
                    screenshot(page, '-connection-error')
                identity_failure['status'] = 0
                page.locator('#retry-access').click()
                expect(page.locator('#workspace')).to_be_visible()
                expect(page.locator('#identity')).to_have_text('alice')

            # An expired bookmark returns to the native CVAT page in the same
            # tab; registry never presents another credential form.
            guest = browser.new_context()
            guest_page = guest.new_page()
            guest_page.goto(origin + '/model-registry/#models')
            expect(guest_page.locator('#access-title')).to_have_text('CVATのセッションを確認できません')
            expect(guest_page.locator('#workspace')).to_be_hidden()
            guest_page.locator('#return-cvat').click()
            expect(guest_page.locator('#native-signin')).to_be_visible()
            assert len(guest.pages) == 1
            guest.close()
            return_to_registry(page)

            for kind in ('segmentation', 'detection'):
                page.locator('#register-nav').click()
                expect(page.locator('#catalog')).to_be_hidden()
                expect(page.locator('#operation')).to_have_text('')
                expect(page.locator('#notice')).to_have_text('')
                page.locator('#name').fill(model_names[kind])
                page.locator('#description').fill('製品画像から対象領域を抽出します。撮影条件と推論結果を確認してから使用してください。')
                page.locator('#author-contact').fill('alice@example.internal')
                page.locator('#model-kind').select_option('polygon' if kind == 'segmentation' else 'rectangle')
                page.locator('[data-key=name]').fill('bright_object')
                directory = ROOT / 'examples' / kind
                for element, file in [('code', 'model.py'), ('weights', 'model.onnx'), ('sample', 'sample.png')]:
                    page.locator('#' + element).set_input_files(str(directory / file))
                if kind == 'segmentation':
                    screenshot(page, '-registration')
                page.locator('#submit-model').click()
                expect(page.locator('#detail')).to_be_visible(timeout=15000)
                expect(page.locator('#operation')).to_contain_text('succeeded')
                page.locator('#test-image').set_input_files(str(directory / 'sample.png'))
                page.locator('#test-button').click()
                expect(page.locator('#test-result')).to_contain_text('request_id')
                expect(page.locator('#preview')).to_be_visible()
                expect(page.locator('#test-result')).to_contain_text('polygon' if kind == 'segmentation' else 'rectangle')
                expect(page.locator('#logs')).to_contain_text('publish')
                assert not page.locator('#threshold').count()
                if kind == 'segmentation':
                    screenshot(page, '-detail')

            page.locator('#models-nav').click()
            expect(page.locator('#models')).to_contain_text(model_names['segmentation'])
            expect(page.locator('#models')).to_contain_text('alice@example.internal')
            expect(page.locator('#model-count')).to_have_text('2')
            expect(page.locator('#published-count')).to_have_text('2')
            expect(page.locator('#my-model-count')).to_have_text('2')
            page.locator('#search').fill('存在しないモデル')
            expect(page.locator('#models')).to_contain_text('一致するモデルがありません')
            expect(page.locator('#catalog-count')).to_have_text('2件中 0件を表示')
            page.locator('#search').fill('')
            page.locator('#search').blur()
            expect(page.locator('#operation')).to_be_hidden()
            screenshot(page)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("Array.from(document.querySelectorAll('*')).filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right]).slice(0,30)")
            screenshot(page, '-mobile')
            page.locator('#register-nav').click()
            expect(page.locator('#registration')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile form overflow'
            screenshot(page, '-registration-mobile')
            page.set_viewport_size({'width': 1440, 'height': 1050})
            page.locator('#models-nav').click()

            # A browser already logged into CVAT goes directly to the catalog.
            bob = browser.new_context(viewport={'width': 1440, 'height': 1050}, locale='ja-JP')
            native_tab = bob.new_page()
            native_tab.goto(origin + '/auth/login')
            enter_native_credentials(native_tab, 'bob')
            native_tab.goto(origin + '/model-registry/#models')
            other = native_tab
            expect(other.locator('#models')).to_contain_text(model_names['segmentation'])
            expect(other.locator('#identity')).to_have_text('bob')
            expect(other.locator('#my-model-count')).to_have_text('0')
            other.locator('#models button').first.click()
            expect(other.locator('#detail')).to_be_visible()
            expect(other.locator('#update-model')).to_be_hidden()
            expect(other.locator('#delete-model')).to_be_hidden()
            expect(other.locator('#owner-detail')).to_be_hidden()
            expect(other.locator('#detail-contact')).to_contain_text('alice@example.internal')
            model_id = other.url.rsplit('/', 1)[-1]
            status = other.evaluate("""async id => (await fetch('./api/models/' + id, {method:'DELETE', headers:{'Content-Type':'application/json'}, body:'{}'})).status""", model_id)
            assert status == 403, status
            bob.clear_cookies()
            return_to_registry(other)
            expect(other.locator('#access-actions')).to_be_visible()
            expect(other.locator('#workspace')).to_be_hidden()
            expect(other.locator('#detail-contact')).to_have_text('')
            bob.close()
            return_to_registry(page)
            page.get_by_role('button', name=model_names['segmentation'] + 'の詳細を見る').click()
            expect(page.locator('#detail')).to_be_visible()
            page.on('dialog', lambda dialog: dialog.accept())
            page.locator('#delete-model').click()
            expect(page.locator('#detail-info')).to_contain_text('削除済み')

            # Old requests may finish after CVAT has changed accounts in another tab.
            page.locator('#models-nav').click()
            for old_status, next_user in [(200, 'bob'), (401, 'alice')]:
                expect(page.locator('#catalog')).to_be_visible()
                page.evaluate("""(status) => {
                    const original = window.fetch;
                    window.oldReadHeld = false;
                    window.fetch = (url, options) => {
                        if (String(url).endsWith('/api/models') && !window.oldReadHeld) {
                            window.oldReadHeld = true;
                            return new Promise(resolve => {
                                window.releaseOldRead = () => {
                                    window.fetch = original;
                                    const payload = status === 200 ? [{id:'aaaaaaaaaaaaaaaaaaaa',owner:'private-old-user',manifest:{name:'PRIVATE_STALE_RECORD'},can_manage:true,operations:[]}] : {detail:'old session expired'};
                                    resolve(new Response(JSON.stringify(payload), {status, headers:{'Content-Type':'application/json'}}));
                                };
                            });
                        }
                        return original(url, options);
                    };
                }""", old_status)
                page.locator('#refresh').click()
                for _ in range(100):
                    if page.evaluate('window.oldReadHeld'):
                        break
                    page.wait_for_timeout(50)
                assert page.evaluate('window.oldReadHeld')
                native_tab = context.new_page()
                native_tab.goto(origin + '/auth/login')
                enter_native_credentials(native_tab, next_user)
                native_tab.close()
                return_to_registry(page)
                expect(page.locator('#identity')).to_have_text(next_user)
                expect(page.locator('#models')).to_contain_text(model_names['detection'])
                page.evaluate('window.releaseOldRead()')
                page.wait_for_timeout(100)
                expect(page.locator('#workspace')).to_be_visible()
                expect(page.locator('#identity')).to_have_text(next_user)
                expect(page.locator('#models')).not_to_contain_text('PRIVATE_STALE_RECORD')
                page.locator('#refresh').click()
                expect(page.locator('#models')).to_contain_text(model_names['detection'])
            # Capture a form while signed in as Alice, then switch CVAT's cookie
            # before the XHR is sent. The server must refuse the old identity.
            page.locator('#register-nav').click()
            page.locator('#name').fill('MUST_NOT_BE_PUBLISHED_AS_BOB')
            for element, file in [('code', 'model.py'), ('weights', 'model.onnx'), ('sample', 'sample.png')]:
                page.locator('#' + element).set_input_files(str(ROOT / 'examples' / 'segmentation' / file))
            page.evaluate("""() => {
                const original = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.send = function(body) {
                    const xhr = this;
                    XMLHttpRequest.prototype.send = original;
                    window.continueUpload = () => original.call(xhr, body);
                };
            }""")
            page.locator('#submit-model').click()
            context.add_cookies([{'name': 'sessionid', 'value': SESSIONS['bob'], 'url': origin, 'httpOnly': True, 'sameSite': 'Lax'}])
            with page.expect_response(lambda response: response.url.endswith('/api/upload')) as changed_upload:
                page.evaluate('window.continueUpload()')
            assert changed_upload.value.status == 409
            assert changed_upload.value.headers.get('x-registry-account-changed') == '1'
            expect(page.locator('#identity')).to_have_text('bob')
            expect(page.locator('#notice')).to_contain_text('CVATのアカウントが変わりました')
            expect(page.locator('#name')).to_have_value('')
            assert page.locator('#code').input_value() == ''
            page.locator('#models-nav').click()
            expect(page.locator('#models')).not_to_contain_text('MUST_NOT_BE_PUBLISHED_AS_BOB')
            # Switch back through native CVAT login before testing revocation.
            native_tab = context.new_page()
            native_tab.goto(origin + '/auth/login')
            enter_native_credentials(native_tab, 'alice')
            native_tab.close()
            return_to_registry(page)
            expect(page.locator('#identity')).to_have_text('alice')
            # Revoking the real CVAT session expires registry access on its next read.
            valid_sessions.discard(SESSIONS['alice'])
            page.locator('#refresh').click()
            expect(page.locator('#access-actions')).to_be_visible()
            expect(page.locator('#workspace')).to_be_hidden()
            expect(page.locator('#identity')).to_have_text('')
            expect(page.locator('#logs')).to_have_text('')
            assert 'alice' in auth_calls and 'bob' in auth_calls
            assert not registry_login_requests, registry_login_requests
            assert not errors, errors
            browser.close()
            print('PASS: Chromium native CVAT login before opening the registry, direct catalog access, no registry credentials or login link, CVAT 400/403/503 error distinction and retry, same-tab return to CVAT on session expiry, no registry session cookie, subpath assets/API, styled desktop/mobile layouts, separate catalog/registration/details, search, segmentation/detection upload and previews, shared author catalog, ownership controls, CSRF, CVAT account switch, stale-response rejection, upload identity mismatch rejection, session expiry and deletion. CVAT and inference use fakes.')


if __name__ == '__main__':
    main()
