"""Reconnect a directly opened local browser without exposing the API bearer.

Enabled only by the checkout server. A top-level loopback navigation issues a
short-lived challenge; a same-origin POST consumes it and sets a signed,
persistent HttpOnly cookie. API clients still need their existing bearer.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from threading import Lock

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

COOKIE = 'autocut_browser_session'
CHALLENGE = 'autocut_browser_connect'
MAX_AGE = 30 * 24 * 60 * 60
CHALLENGE_AGE = 60

LANDING = '''<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Creative Factory</title><style>
body{margin:0;background:#f7f8fa;color:#252a34;font:15px -apple-system,BlinkMacSystemFont,sans-serif;min-height:100vh;display:grid;place-items:center}
main{width:min(420px,calc(100% - 64px));padding:32px;background:white;border:1px solid #e4e8ef;border-radius:14px}
h1{font-size:22px;margin:0 0 18px}p{line-height:1.6;color:#778297}button{border:0;border-radius:8px;padding:12px 18px;background:#4277ff;color:white;font:inherit;cursor:pointer}
</style></head><body><main><h1>Creative Factory</h1>
<p id="message" role="status">正在恢复本机连接…</p>
<button id="retry" hidden>重新连接</button><noscript>请启用浏览器 JavaScript 后刷新页面。</noscript>
</main><script src="/session/connect.js"></script></body></html>'''


def local_host(host):
    match = re.fullmatch(r'(?:127\.0\.0\.1|localhost):([0-9]{1,5})', host)
    return bool(match and 1 <= int(match[1]) <= 65535)


class BrowserSession:
    def __init__(self, token):
        self.key = token.encode()
        self.challenges = {}
        self.lock = Lock()

    @staticmethod
    def cookie_name(request, name=COOKIE):
        # Cookies have no port scope. Keep two local development servers from
        # replacing each other's browser credentials in the same profile.
        return f"{name}_{request.url.port}"

    @staticmethod
    def local_browser(request, site, mode, dest):
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            return False
        headers = request.headers
        return (local and headers.get('sec-fetch-site') in site
                and headers.get('sec-fetch-mode') in mode
                and headers.get('sec-fetch-dest') == dest)

    def navigation(self, request):
        # Chromium JSON viewers give a manual reload an extension origin and
        # therefore label it cross-site. Accept only a user-activated, direct
        # document navigation with no referring website in that case.
        sites = {'none', 'same-origin'}
        if request.headers.get('sec-fetch-user') == '?1' and not request.headers.get('referer'):
            sites.update({'cross-site', 'same-site'})
        return (request.method == 'GET' and request.url.path == '/'
                and 'text/html' in request.headers.get('accept', '')
                and self.local_browser(request, sites, {'navigate'}, 'document'))

    def signature(self, host, payload):
        return hmac.new(self.key, (host + '\n' + payload).encode(), hashlib.sha256).hexdigest()

    def valid(self, request):
        value = request.cookies.get(self.cookie_name(request), '')
        if not re.fullmatch(r'v1\.[0-9]{10}\.[A-Za-z0-9_-]{24}\.[a-f0-9]{64}', value):
            return False
        version, expiry, nonce, signature = value.split('.')
        if not time.time() < int(expiry) <= time.time() + MAX_AGE + 5:
            return False
        return hmac.compare_digest(signature, self.signature(request.headers['host'], '.'.join([version, expiry, nonce])))

    def remember(self, request, response):
        payload = f'v1.{int(time.time()) + MAX_AGE}.{secrets.token_urlsafe(18)}'
        response.set_cookie(self.cookie_name(request), payload + '.' + self.signature(request.headers['host'], payload),
                            max_age=MAX_AGE, httponly=True, samesite='strict', path='/')

    def landing(self, request):
        nonce = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.challenges = {k: v for k, v in self.challenges.items() if v[1] > now}
            if len(self.challenges) >= 128:
                self.challenges.pop(next(iter(self.challenges)))
            self.challenges[nonce] = (request.headers['host'], now + CHALLENGE_AGE)
        response = HTMLResponse(LANDING)
        response.set_cookie(self.cookie_name(request, CHALLENGE), nonce, max_age=CHALLENGE_AGE,
                            httponly=True, samesite='strict', path='/session')
        return response

    def script(self, request, web):
        if request.method != 'GET' or not self.local_browser(request, {'same-origin'}, {'no-cors', 'same-origin'}, 'script'):
            return JSONResponse({'error': 'AUTH_REQUIRED'}, 401)
        return FileResponse(web / 'connect.js')

    def connect(self, request):
        if (request.method != 'POST'
                or request.headers.get('origin') != 'http://' + request.headers['host']
                or request.headers.get('content-type', '').split(';')[0] != 'application/json'
                or not self.local_browser(request, {'same-origin'}, {'cors', 'same-origin'}, 'empty')):
            return JSONResponse({'error': 'ORIGIN_REJECTED'}, 403)
        with self.lock:
            challenge = self.challenges.pop(request.cookies.get(self.cookie_name(request, CHALLENGE), ''), None)
        if not self.valid(request) and (not challenge or challenge[0] != request.headers['host'] or challenge[1] <= time.time()):
            return JSONResponse({'error': 'AUTH_REQUIRED'}, 401)
        response = JSONResponse({'status': 'CONNECTED'})
        self.remember(request, response)
        response.delete_cookie(self.cookie_name(request, CHALLENGE), path='/session', httponly=True, samesite='strict')
        return response
