"""Browser reconnect regressions with isolated storage and no provider calls."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from autocut.browser_session import CHALLENGE as CHALLENGE_BASE, COOKIE as COOKIE_BASE, MAX_AGE
from autocut.server import create_app
from autocut.storage import Store

NAVIGATION = {'Accept': 'text/html', 'Sec-Fetch-Site': 'none',
              'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
CONNECT = {'Origin': 'http://127.0.0.1:9999', 'Content-Type': 'application/json',
           'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}
SCRIPT = {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'script'}
TOKEN = 'private-fixture-bearer-never-in-html-or-javascript'
COOKIE = COOKIE_BASE + '_9999'
CHALLENGE = CHALLENGE_BASE + '_9999'


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'data')
        self.manager = MagicMock()
        self.keys = MagicMock()
        self.keys.get.side_effect = AssertionError('Reconnect must not access provider credentials')
        self.web = Path(__file__).resolve().parents[1] / 'web'
        self.client = self.client_for()

    def client_for(self, token=TOKEN, enabled=True, peer='127.0.0.1', port=9999, cookies=None):
        app = create_app(self.store, self.keys, MagicMock(), self.manager, token,
                         web=self.web, recover_on_startup=False, browser_entry=enabled)
        client = TestClient(app, base_url=f'http://127.0.0.1:{port}',
                            client=(peer, 12345), cookies=cookies)
        self.addCleanup(client.close)
        return client

    def connect(self, client=None):
        client = client or self.client
        root = client.get('/', headers=NAVIGATION)
        self.assertEqual(root.status_code, 200, root.text)
        self.assertIn('正在恢复本机连接', root.text)
        result = client.post('/session/connect', headers=CONNECT, json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {'status': 'CONNECTED'})
        return result

    def test_direct_navigation_connects_without_exposing_bearer(self):
        root = self.client.get('/', headers=NAVIGATION)
        self.assertEqual(root.status_code, 200)
        self.assertNotIn(TOKEN, root.text + str(root.headers))
        self.assertNotIn('新建剪辑', root.text)  # no app data before completing the challenge
        self.assertIn('HttpOnly', root.headers['set-cookie'])
        self.assertIn('SameSite=strict', root.headers['set-cookie'])
        self.assertEqual(root.headers['cache-control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", root.headers['content-security-policy'])
        script = self.client.get('/session/connect.js', headers=SCRIPT)
        self.assertEqual(script.status_code, 200)
        self.assertNotIn(TOKEN, script.text)
        self.assertEqual(self.client.get('/jobs').status_code, 401)
        result = self.client.post('/session/connect', headers=CONNECT, json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn(TOKEN, result.text + str(result.headers))
        self.assertEqual(self.client.get('/session/check').status_code, 200)
        self.assertEqual(self.client.get('/jobs').json(), [])
        self.assertNotIn(CHALLENGE, self.client.cookies)
        self.manager.enqueue.assert_not_called()

    def test_cookie_survives_browser_reload_and_server_restart(self):
        result = self.connect()
        cookie = next(c for c in self.client.cookies.jar if c.name == COOKIE)
        self.assertGreater(cookie.expires, time.time() + MAX_AGE - 10)
        self.assertIn(f'Max-Age={MAX_AGE}', result.headers['set-cookie'])
        self.assertIn('HttpOnly', result.headers['set-cookie'])
        self.assertIn('SameSite=strict', result.headers['set-cookie'])
        refreshed = self.client.get('/', headers=NAVIGATION)
        self.assertIn('id="new-form"', self.client.get('/static/app.js').text)
        self.assertIn('id="main"', refreshed.text)
        self.assertNotIn('正在恢复本机连接', refreshed.text)
        restarted = self.client_for(cookies=self.client.cookies)
        self.assertEqual(restarted.get('/jobs').status_code, 200)
        self.assertIn('id="main"', restarted.get('/', headers=NAVIGATION).text)

    def test_rotated_server_token_recovers_on_navigation(self):
        self.connect()
        restarted = self.client_for(token='new-server-bearer', cookies=self.client.cookies)
        self.assertEqual(restarted.get('/jobs').status_code, 401)
        self.connect(restarted)
        self.assertEqual(restarted.get('/jobs').status_code, 200)

    def test_api_requests_and_embedded_pages_cannot_bootstrap(self):
        for headers, status in [({}, 401), ({**NAVIGATION, 'Sec-Fetch-Dest': 'iframe'}, 401),
                                ({**NAVIGATION, 'Sec-Fetch-Mode': 'no-cors'}, 401),
                                ({**NAVIGATION, 'Sec-Fetch-Site': 'cross-site'}, 403),
                                ({**NAVIGATION, 'Sec-Fetch-Site': 'same-site'}, 403)]:
            with self.subTest(headers=headers):
                result = self.client.get('/', headers=headers)
                self.assertEqual(result.status_code, status)
                self.assertNotIn('set-cookie', result.headers)
        self.client.get('/', headers=NAVIGATION)
        self.assertEqual(self.client.get('/static/app.js').status_code, 401)
        self.assertEqual(self.client.post('/native/grant', json={'path': self.temp.name}).status_code, 401)

    def test_manual_reload_from_chromium_json_viewer_recovers(self):
        viewer_reload = {**NAVIGATION, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-User': '?1'}
        root = self.client.get('/', headers=viewer_reload)
        self.assertEqual(root.status_code, 200)
        self.assertIn('正在恢复本机连接', root.text)
        self.assertEqual(self.client.post('/session/connect', headers=CONNECT, json={}).status_code, 200)
        # The exception is for direct document reloads, never a website's
        # subrequests, embedded frame, or form submission.
        for headers in [{**viewer_reload, 'Referer': 'https://evil.example/'},
                        {**viewer_reload, 'Sec-Fetch-Dest': 'iframe'},
                        {**viewer_reload, 'Sec-Fetch-Mode': 'no-cors'}]:
            self.assertEqual(self.client.get('/', headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/jobs', headers=viewer_reload).status_code, 403)

    def test_unauthorized_connect_and_host_or_origin_forgery_rejected(self):
        self.assertEqual(self.client.post('/session/connect', headers=CONNECT, json={}).status_code, 401)
        for headers, status in [({'Host': 'evil.example:9999'}, 403),
                                ({'Host': '127.0.0.1:9999.evil.example'}, 403),
                                ({'Host': 'localhost:99999'}, 403),
                                ({'Origin': 'http://evil.example'}, 403)]:
            with self.subTest(headers=headers):
                result = self.client.get('/', headers={**NAVIGATION, **headers})
                self.assertEqual(result.status_code, status)
                self.assertNotIn('set-cookie', result.headers)
        self.client.get('/', headers=NAVIGATION)
        for headers in [{**CONNECT, 'Origin': 'http://evil.example'},
                        {**CONNECT, 'Origin': 'http://127.0.0.1:9998'},
                        {k: v for k, v in CONNECT.items() if k != 'Origin'},
                        {**CONNECT, 'Sec-Fetch-Site': 'same-site'},
                        {**CONNECT, 'Content-Type': 'text/plain'}]:
            self.assertEqual(self.client.post('/session/connect', headers=headers, content=b'{}').status_code, 403)
        self.assertEqual(self.client.post('/session/connect', headers=CONNECT, json={}).status_code, 200)
        self.assertEqual(self.client.get('/jobs', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)

    def test_challenge_expires_and_cannot_be_replayed_by_another_client(self):
        self.client.get('/', headers=NAVIGATION)
        nonce = self.client.cookies.get(CHALLENGE)
        replay = self.client_for(cookies={CHALLENGE: nonce})
        # Use the same app so this checks nonce consumption rather than a new nonce store.
        other = TestClient(self.client.app, base_url='http://127.0.0.1:9999',
                           client=('127.0.0.1', 12345), cookies={CHALLENGE: nonce})
        self.addCleanup(other.close)
        self.assertEqual(self.client.post('/session/connect', headers=CONNECT, json={}).status_code, 200)
        self.assertEqual(other.post('/session/connect', headers=CONNECT, json={}).status_code, 401)
        self.assertEqual(replay.post('/session/connect', headers=CONNECT, json={}).status_code, 401)
        self.client.cookies.clear()
        self.client.get('/', headers=NAVIGATION)
        with patch('autocut.browser_session.time.time', return_value=time.time() + 61):
            self.assertEqual(self.client.post('/session/connect', headers=CONNECT, json={}).status_code, 401)

    def test_signed_cookie_rejects_tampering_expiry_and_other_server_ports(self):
        self.connect()
        value = self.client.cookies.get(COOKIE)
        forged = self.client_for(cookies={COOKIE: value[:-1] + ('0' if value[-1] != '0' else '1')})
        self.assertEqual(forged.get('/jobs').status_code, 401)
        other_port = self.client_for(port=9998, cookies={COOKIE_BASE + '_9998': value})
        self.assertEqual(other_port.get('/jobs').status_code, 401)
        with patch('autocut.browser_session.time.time', return_value=time.time() + MAX_AGE + 1):
            self.assertEqual(self.client.get('/jobs').status_code, 401)

    def test_only_explicitly_enabled_loopback_server_offers_reconnect(self):
        disabled = self.client_for(enabled=False)
        self.assertEqual(disabled.get('/', headers=NAVIGATION).status_code, 401)
        remote = self.client_for(peer='192.0.2.10')
        self.assertEqual(remote.get('/', headers=NAVIGATION).status_code, 401)
        self.assertEqual(remote.post('/session/connect', headers=CONNECT, json={}).status_code, 403)

    def test_existing_native_cookie_and_bearer_clients_remain_compatible(self):
        self.assertEqual(self.client.get('/jobs', headers={'Authorization': 'Bearer ' + TOKEN}).status_code, 200)
        native = self.client_for(enabled=False, cookies={'autocut_session': TOKEN})
        self.assertEqual(native.get('/jobs').status_code, 200)
        self.assertEqual(native.get('/jobs', headers={'Origin': 'http://evil.example'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
